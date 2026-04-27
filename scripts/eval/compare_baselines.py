#!/usr/bin/env python3
"""Phase 4 acceptance gate — compare two eval baseline JSONs.

Gate (both criteria must pass):
  A) >=80% of queries show >=1 improvement OR identical top-3 set vs baseline.
  B) No query loses >2 known-good results from baseline top-10.

Exit 0 = PASS (both criteria met).
Exit 1 = FAIL (gate failed; details printed to stdout as JSON).
Exit 2 = usage error (no overlapping query IDs).

Usage:
    python scripts/eval/compare_baselines.py eval/baseline.json eval/jina_v2.json
    python scripts/eval/compare_baselines.py old.json new.json --top-k 10 --top-improvement 3

Decoupled from `run_baseline.py --check` (which is reproducibility-only).
"""

import argparse
import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    """Load a baseline JSON.

    Supports both the wrapped shape produced by run_baseline.py
    (``{"results": {qid: [...]}}``) and the raw shape (``{qid: [...]}``).
    """
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict) and "results" in data and isinstance(data["results"], dict):
        return data["results"]
    return data


def _paths(hits: list[dict]) -> list[str]:
    return [h["file_path"] for h in hits]


def _scores(hits: list[dict]) -> dict[str, float]:
    return {h["file_path"]: h["score"] for h in hits}


def evaluate_query(
    qid: str,
    baseline_hits: list[dict],
    new_hits: list[dict],
    top_improvement: int,
    top_k: int,
) -> dict:
    base_top_n = set(_paths(baseline_hits[:top_improvement]))
    new_top_n = set(_paths(new_hits[:top_improvement]))
    base_top_k = set(_paths(baseline_hits[:top_k]))
    new_top_k = set(_paths(new_hits[:top_k]))
    base_scores = _scores(baseline_hits[:top_improvement])
    new_scores = _scores(new_hits[:top_improvement])

    identical_top_n = new_top_n == base_top_n
    improvement_count = sum(
        1
        for fp in new_top_n
        if fp not in base_top_n or new_scores.get(fp, 0) > base_scores.get(fp, 0)
    )
    passes_a = identical_top_n or improvement_count >= 1

    lost = base_top_k - new_top_k
    passes_b = len(lost) <= 2

    return {
        "query_id": qid,
        "passes_A": passes_a,
        "passes_B": passes_b,
        "identical_top_n": identical_top_n,
        "improvement_count": improvement_count,
        "lost_results": sorted(lost),
        "lost_count": len(lost),
    }


def run(
    baseline: dict,
    new: dict,
    top_improvement: int,
    top_k: int,
    threshold_a: float,
) -> tuple[int, dict]:
    query_ids = sorted(set(baseline) & set(new))
    if not query_ids:
        return 2, {"error": "no overlapping query IDs between files"}

    per_query = [
        evaluate_query(qid, baseline[qid], new[qid], top_improvement, top_k)
        for qid in query_ids
    ]

    pass_a = sum(1 for r in per_query if r["passes_A"])
    fail_a = [r for r in per_query if not r["passes_A"]]
    fail_b = [r for r in per_query if not r["passes_B"]]
    pass_a_ratio = pass_a / len(per_query)

    global_pass_a = pass_a_ratio >= threshold_a
    global_pass_b = not fail_b

    report = {
        "total_queries": len(per_query),
        "criterion_A": {
            "threshold": threshold_a,
            "pass_ratio": round(pass_a_ratio, 4),
            "pass_count": pass_a,
            "fail_count": len(fail_a),
            "passed": global_pass_a,
            "failures": [
                {
                    "id": r["query_id"],
                    "improvement_count": r["improvement_count"],
                    "identical_top_n": r["identical_top_n"],
                }
                for r in fail_a
            ],
        },
        "criterion_B": {
            "max_loss_allowed": 2,
            "passed": global_pass_b,
            "failures": [
                {
                    "id": r["query_id"],
                    "lost_count": r["lost_count"],
                    "lost_results": r["lost_results"],
                }
                for r in fail_b
            ],
        },
        "gate_passed": global_pass_a and global_pass_b,
        "per_query": per_query,
    }
    return (0 if report["gate_passed"] else 1), report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline", help="Frozen baseline JSON (e.g. eval/baseline.json)")
    ap.add_argument("new_results", help="New capture JSON (e.g. eval/jina_v2.json)")
    ap.add_argument("--top-k", type=int, default=10,
                    help="Window for criterion B (default: 10)")
    ap.add_argument("--top-improvement", type=int, default=3,
                    help="Window for top-set comparison in criterion A (default: 3)")
    ap.add_argument("--threshold-a", type=float, default=0.80,
                    help="Fraction of queries that must pass criterion A (default: 0.80)")
    args = ap.parse_args()

    baseline = load(args.baseline)
    new = load(args.new_results)
    code, report = run(baseline, new, args.top_improvement, args.top_k, args.threshold_a)
    if code == 2:
        print(f"ERROR: {report['error']}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
