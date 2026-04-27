#!/usr/bin/env python3
"""Phase 4 — Re-index Hub content into a fresh Qdrant collection.

Idempotent: running twice produces the same end state. Reads file content
from the existing SQLite content store and re-submits it to /batch so the
new embed-service backend computes vectors against the new collection.

Usage:
    .venv/bin/python scripts/reindex.py --collection code_v2_jina
    .venv/bin/python scripts/reindex.py --collection code_v2_jina --dry-run
    .venv/bin/python scripts/reindex.py --quiesce-timeout 30 --collection code_v2_jina

The cutover protocol (see docs/reindex.md) wraps this script with watcher
quiesce + Hub healthcheck poll + collection switch.
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


def fetch_files_from_content_store(db_path: str) -> list[tuple[str, str, str, str]]:
    if not Path(db_path).exists():
        print(f"ERROR: content DB not found at {db_path}", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT machine_id, project, file_path, content FROM files ORDER BY machine_id, project, file_path"
        ).fetchall()
    finally:
        conn.close()
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


def main() -> int:
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
    args = ap.parse_args()

    files = fetch_files_from_content_store(args.db)
    print(f"Found {len(files)} files in content store at {args.db}")

    if args.dry_run:
        for mid, proj, path, content in files[:10]:
            print(f"  [{mid}/{proj}] {path}  ({len(content)} chars)")
        if len(files) > 10:
            print(f"  ... and {len(files) - 10} more")
        return 0

    with httpx.Client() as http:
        if not wait_for_healthy(http, args.hub, args.quiesce_timeout):
            print(f"ERROR: Hub unhealthy after {args.quiesce_timeout}s", file=sys.stderr)
            return 3

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
