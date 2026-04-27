#!/usr/bin/env python3
"""Phase 4 — Re-index Hub content into a fresh Qdrant collection.

Idempotent: running twice produces the same end state. Reads file content
from the existing SQLite content store and re-submits it to /batch so the
new embed-service backend computes vectors against the new collection.

Usage:
    .venv/bin/python scripts/reindex.py --collection code_v2_jina
    .venv/bin/python scripts/reindex.py --collection code_v2_jina --dry-run
    .venv/bin/python scripts/reindex.py --collection code_v2_jina --clean
    .venv/bin/python scripts/reindex.py --collection code_v2_jina \\
        --skip-machine-ids test-machine-01,m1
    .venv/bin/python scripts/reindex.py --collection code_v2_jina --no-validate

The cutover protocol (see docs/runbooks/reindex.md) wraps this script with watcher
quiesce + Hub healthcheck poll + collection switch.

Exit codes:
    0  success
    1  one or more /batch calls failed
    2  content DB missing
    3  Hub healthcheck timeout
    4  Hub QDRANT_COLLECTION mismatch (validation failure)
    5  --clean failed to drop the target collection
"""

import argparse
import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent

DEFAULT_HUB = os.getenv("HUB_URL", "http://localhost:9000")
DEFAULT_TOKEN = os.getenv("HUB_AUTH_TOKEN", "changeme")
DEFAULT_DB = os.getenv("CONTENT_DB_PATH", str(REPO / "data" / "hub-content.db"))
DEFAULT_COLLECTION = os.getenv("QDRANT_COLLECTION", "code_v2_jina")
DEFAULT_QDRANT_URL = os.getenv("QDRANT_URL") or (
    f"http://{os.getenv('QDRANT_HOST', 'localhost')}:{os.getenv('QDRANT_PORT', '6333')}"
)


def fetch_files_from_content_store(
    db_path: str, skip_machine_ids: set[str] | None = None,
) -> list[tuple[str, str, str, str]]:
    if not Path(db_path).exists():
        print(f"ERROR: content DB not found at {db_path}", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT machine_id, project, file_path, content FROM files "
            "ORDER BY machine_id, project, file_path"
        ).fetchall()
    finally:
        conn.close()
    if skip_machine_ids:
        rows = [r for r in rows if r[0] not in skip_machine_ids]
    return rows


def wait_for_healthy(http: httpx.Client, hub: str, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = http.get(f"{hub}/health", timeout=5.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def validate_active_collection(
    http: httpx.Client, hub: str, token: str, expected: str,
) -> tuple[bool, str | None]:
    """Verify that Hub /stats reports the expected QDRANT_COLLECTION as active.

    Returns (ok, actual_or_error). Prevents the silent footgun where Hub points
    at a different collection than the --collection arg, so the script would
    re-emit into the wrong store.
    """
    try:
        r = http.get(
            f"{hub}/stats",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        r.raise_for_status()
        body = r.json()
    except Exception as exc:
        return False, f"hub_stats_unreachable: {exc}"
    qdrant_block = body.get("qdrant") or {}
    actual = qdrant_block.get("collection")
    if not actual:
        return False, "qdrant.collection field missing from Hub /stats response"
    return (actual == expected), actual


def drop_collection(qdrant_url: str, collection: str) -> tuple[bool, str | None]:
    """DELETE the collection via Qdrant REST. Hub _ensure_collection recreates
    it on the next /batch call, so this is the entire 'clean' step."""
    try:
        with httpx.Client() as http:
            r = http.delete(f"{qdrant_url}/collections/{collection}", timeout=30.0)
        if r.status_code in (200, 404):
            return True, None
        return False, f"qdrant DELETE returned {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return False, f"qdrant DELETE failed: {exc}"


def post_batch(
    http: httpx.Client,
    hub: str,
    token: str,
    machine_id: str,
    project: str,
    file_path: str,
    content: str,
) -> int:
    body = {
        "machine_id": machine_id,
        "project": project,
        "events": [
            {
                "type": "MODIFY",
                "path": file_path,
                "machine_id": machine_id,
                "project": project,
                "timestamp": int(time.time()),
                "content": content,
                # Use real content hash so Hub-side dedup works on retry.
                # `reindex-{ts}` would defeat any future content_hash short-circuit.
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "entities": [],
                "relations": [],
            }
        ],
    }
    r = http.post(
        f"{hub}/batch",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    return r.status_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", default=DEFAULT_HUB)
    ap.add_argument("--token", default=DEFAULT_TOKEN)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--collection", default=DEFAULT_COLLECTION,
                    help="Target collection (Hub must be configured to use it via QDRANT_COLLECTION)")
    ap.add_argument("--dry-run", action="store_true",
                    help="List files that would be re-indexed without sending")
    ap.add_argument("--quiesce-timeout", type=int, default=30,
                    help="Seconds to wait for Hub /health before aborting")
    ap.add_argument("--clean", action="store_true",
                    help="DELETE the target collection before re-emitting (Hub recreates on next /batch)")
    ap.add_argument("--skip-machine-ids", default="",
                    help="Comma-separated list of machine_ids to exclude from re-emit")
    ap.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL,
                    help="Qdrant REST URL for --clean DELETE")
    ap.add_argument("--validate", dest="validate", action="store_true", default=True,
                    help="Verify Hub /stats reports the same active collection (default)")
    ap.add_argument("--no-validate", dest="validate", action="store_false",
                    help="Skip the Hub /stats collection-mismatch check")
    return ap.parse_args(argv)


def main() -> int:
    args = parse_args()
    skip = {m.strip() for m in args.skip_machine_ids.split(",") if m.strip()}

    files = fetch_files_from_content_store(args.db, skip_machine_ids=skip)
    if skip:
        print(f"Found {len(files)} files in content store at {args.db} "
              f"(skipped machine_ids: {sorted(skip)})")
    else:
        print(f"Found {len(files)} files in content store at {args.db}")

    if args.dry_run:
        for mid, proj, path, content in files[:10]:
            print(f"  [{mid}/{proj}] {path}  ({len(content)} chars)")
        if len(files) > 10:
            print(f"  ... and {len(files) - 10} more")
        return 0

    if args.clean:
        ok, err = drop_collection(args.qdrant_url, args.collection)
        if not ok:
            print(f"ERROR: --clean failed: {err}", file=sys.stderr)
            return 5
        print(f"Dropped collection {args.collection} (will be recreated on first /batch)")

    with httpx.Client() as http:
        if not wait_for_healthy(http, args.hub, args.quiesce_timeout):
            print(f"ERROR: Hub unhealthy after {args.quiesce_timeout}s", file=sys.stderr)
            return 3

        if args.validate:
            matches, actual = validate_active_collection(
                http, args.hub, args.token, args.collection,
            )
            if not matches:
                print(
                    f"ERROR: Hub QDRANT_COLLECTION mismatch — expected '{args.collection}', "
                    f"Hub reports '{actual}'. Set QDRANT_COLLECTION before running, "
                    f"or pass --no-validate to bypass.",
                    file=sys.stderr,
                )
                return 4

        ok = 0
        failed = []
        for mid, proj, path, content in files:
            try:
                code = post_batch(http, args.hub, args.token, mid, proj, path, content)
                if 200 <= code < 300:
                    ok += 1
                else:
                    failed.append((path, code))
            except Exception as exc:
                failed.append((path, str(exc)))

        print(f"Re-indexed {ok}/{len(files)} files into {args.collection}")
        if failed:
            print(f"FAILED ({len(failed)}):")
            for path, reason in failed[:20]:
                print(f"  {path}: {reason}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
