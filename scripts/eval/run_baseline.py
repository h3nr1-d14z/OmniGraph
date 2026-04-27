#!/usr/bin/env python3
"""Phase 0.5 — Baseline eval harness.

Embeds each query in eval/queries.json via the running embed-service,
runs Qdrant search with exact=True for deterministic ordering, and writes
top-10 results to the chosen output JSON.

Usage:
    .venv/bin/python scripts/eval/run_baseline.py
        [--queries eval/queries.json]
        [--output eval/baseline.json]
        [--collection omnigraph_code]
        [--top-k 10]
        [--exact / --no-exact]
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent.parent
DEFAULT_QUERIES = REPO / "eval" / "queries.json"
DEFAULT_OUTPUT = REPO / "eval" / "baseline.json"

DEFAULT_EMBED_URL = os.getenv("EMBED_SERVICE_URL", "http://localhost:8000")
DEFAULT_QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
DEFAULT_QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))


def embed_query(http: httpx.Client, embed_url: str, text: str) -> list[float]:
    r = http.post(f"{embed_url}/embed", json={"texts": [text], "mode": "query"})
    r.raise_for_status()
    return r.json()["embeddings"][0]


def qdrant_search(
    http: httpx.Client,
    qdrant_url: str,
    collection: str,
    vector: list[float],
    machine_id: str | None,
    top_k: int,
    exact: bool,
) -> list[dict]:
    body: dict = {
        "query": vector,
        "limit": top_k,
        "with_payload": True,
        "with_vector": False,
    }
    if exact:
        body["params"] = {"exact": True}
    if machine_id:
        body["filter"] = {
            "must": [{"key": "machine_id", "match": {"value": machine_id}}]
        }
    r = http.post(f"{qdrant_url}/collections/{collection}/points/query", json=body)
    r.raise_for_status()
    points = r.json()["result"]["points"]
    return [
        {
            "id": p["id"],
            "score": p["score"],
            "file_path": (p.get("payload") or {}).get("file_path", ""),
            "entity": (p.get("payload") or {}).get("entity", ""),
        }
        for p in points
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--embed-url", default=DEFAULT_EMBED_URL)
    ap.add_argument("--qdrant-url", default=f"http://{DEFAULT_QDRANT_HOST}:{DEFAULT_QDRANT_PORT}")
    ap.add_argument("--collection", default=os.getenv("QDRANT_COLLECTION", "omnigraph_code"))
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--exact", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--check", action="store_true",
                    help="Run twice and assert top-10 Jaccard >=0.95 per query (Phase 0.5 acceptance)")
    args = ap.parse_args()

    spec = json.loads(args.queries.read_text())
    machine_id = (spec.get("scope") or {}).get("machine_id")

    def run_once() -> dict:
        with httpx.Client(timeout=30.0) as http:
            results: dict[str, list[dict]] = {}
            for q in spec["queries"]:
                vec = embed_query(http, args.embed_url, q["query"])
                hits = qdrant_search(
                    http,
                    args.qdrant_url,
                    args.collection,
                    vec,
                    machine_id,
                    args.top_k,
                    args.exact,
                )
                results[q["id"]] = hits
            return results

    if args.check:
        first = run_once()
        time.sleep(0.5)
        second = run_once()
        worst = 1.0
        for qid in first:
            a = {(h["file_path"], h["entity"]) for h in first[qid]}
            b = {(h["file_path"], h["entity"]) for h in second[qid]}
            if not a and not b:
                continue
            jaccard = len(a & b) / max(len(a | b), 1)
            print(f"  {qid}: jaccard={jaccard:.3f}")
            worst = min(worst, jaccard)
        if worst < 0.95:
            print(f"FAIL: worst jaccard {worst:.3f} < 0.95", file=sys.stderr)
            return 1
        print(f"PASS: worst jaccard {worst:.3f} >= 0.95")
        return 0

    results = run_once()
    output = {
        "queries_file": str(args.queries.relative_to(REPO)),
        "collection": args.collection,
        "machine_id_filter": machine_id,
        "top_k": args.top_k,
        "exact": args.exact,
        "captured_at": int(time.time()),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2))
    n_hits = sum(len(v) for v in results.values())
    print(f"Wrote {args.output} ({len(results)} queries, {n_hits} hits)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
