"""Hub /batch with a real file event ends up reflected in /stats. Requires
live Qdrant + Memgraph + embed-service + hub-api."""
import os
import time

import httpx
import pytest

HUB_URL = os.getenv("HUB_URL", "http://localhost:9000")
TOKEN = os.getenv("HUB_AUTH_TOKEN", "changeme")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.smoke
def test_smoke_syntax_only(require_stack):
    """Send 1 file event, observe /stats reflects it, then clean up via DELETE."""
    machine_id = f"smoke-{int(time.time())}"
    project = "smoke-proj"
    path = f"/tmp/smoke_{int(time.time())}.py"

    payload = {
        "machine_id": machine_id,
        "project": project,
        "events": [{
            "type": "MODIFY",
            "path": path,
            "machine_id": machine_id,
            "project": project,
            "timestamp": int(time.time()),
            "content": "def smoke(): pass\n",
            "content_hash": f"hash-{int(time.time())}",
            "entities": [{"name": "smoke", "type": "function", "line": 1, "start_line": 1, "end_line": 1}],
            "relations": [],
        }],
    }
    r = httpx.post(f"{HUB_URL}/batch", json=payload, headers=HEADERS, timeout=30)
    assert r.status_code in (200, 502), f"unexpected {r.status_code}: {r.text}"
    # 502 is acceptable if embed transient down — our concern is the graph+content path.

    # Poll /stats for our machine_id
    for _ in range(20):
        s = httpx.get(f"{HUB_URL}/stats", params={"machine_id": machine_id}, headers=HEADERS, timeout=2)
        if s.status_code == 200 and (s.json().get("content_store") or {}).get("files", 0) >= 1:
            break
        time.sleep(0.5)
    else:
        pytest.fail(f"stats never reflected file for machine_id={machine_id}")

    # Cleanup: DELETE event
    cleanup = {
        "machine_id": machine_id, "project": project,
        "events": [{
            "type": "DELETE",
            "path": path, "machine_id": machine_id, "project": project,
            "timestamp": int(time.time()),
        }],
    }
    httpx.post(f"{HUB_URL}/batch", json=cleanup, headers=HEADERS, timeout=10)
