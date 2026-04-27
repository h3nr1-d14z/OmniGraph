#!/usr/bin/env python3
"""Hub health check. Exit 0 = healthy, 1 = unhealthy.

Env:
  HUB_URL                       Base URL (default: http://localhost:9000)
  HUB_AUTH_TOKEN                Bearer token (default: changeme)
  HEALTHCHECK_LAG_WARN_RATIO    float threshold for vector lag (default: 0.8)
  HEALTHCHECK_DEAD_JOBS_MAX     int threshold for dead jobs (default: 0)
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def parse_args():
    p = argparse.ArgumentParser(description="OmniGraph hub health check")
    p.add_argument("--url", default=os.getenv("HUB_URL", "http://localhost:9000"))
    p.add_argument("--token", default=os.getenv("HUB_AUTH_TOKEN", "changeme"))
    p.add_argument("--lag-warn-ratio", type=float, default=float(os.getenv("HEALTHCHECK_LAG_WARN_RATIO", "0.8")))
    p.add_argument("--dead-jobs-max", type=int, default=int(os.getenv("HEALTHCHECK_DEAD_JOBS_MAX", "0")))
    p.add_argument("--quiet", action="store_true", help="Suppress stdout on success")
    return p.parse_args()


def fetch_stats(base_url, token):
    req = urllib.request.Request(f"{base_url}/stats")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def evaluate(data, args):
    issues = []
    qdrant = data.get("qdrant") or {}
    memgraph = data.get("memgraph") or {}
    cs = data.get("content_store") or {}
    wq = data.get("watcher_queue") or {}

    files = cs.get("files", 0)
    points = qdrant.get("points", 0)
    entities = memgraph.get("entities", 0)
    dead_jobs = ((wq.get("state_counts") or {}).get("dead", 0)) if wq else 0

    if data.get("status") == "degraded":
        issues.append(f"hub status=degraded errors={data.get('errors')}")

    if entities > 0:
        ratio = points / entities
        if ratio < args.lag_warn_ratio:
            issues.append(f"vector_lag_ratio={ratio:.3f} < {args.lag_warn_ratio} (points={points}, entities={entities})")

    if dead_jobs > args.dead_jobs_max:
        issues.append(f"dead_semantic_jobs={dead_jobs} > {args.dead_jobs_max}")

    return {
        "healthy": len(issues) == 0,
        "files": files,
        "points": points,
        "entities": entities,
        "dead_semantic_jobs": dead_jobs,
        "issues": issues,
    }


def main():
    args = parse_args()
    try:
        data = fetch_stats(args.url, args.token)
    except (urllib.error.URLError, OSError) as exc:
        sys.stdout.write(json.dumps({"healthy": False, "issues": [f"connection error: {exc}"]}) + "\n")
        sys.exit(1)

    result = evaluate(data, args)
    if not args.quiet or not result["healthy"]:
        sys.stdout.write(json.dumps(result) + "\n")
    sys.exit(0 if result["healthy"] else 1)


if __name__ == "__main__":
    main()
