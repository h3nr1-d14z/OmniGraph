import time

import httpx
import pytest

HUB_URL = "http://localhost:9000"


def poll_stats(predicate, timeout=10.0, interval=0.5, headers=None):
    """Poll GET /stats until predicate(data) is True or timeout."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{HUB_URL}/stats", headers=headers or {}, timeout=2.0)
            if r.status_code == 200:
                last = r.json()
                if predicate(last):
                    return last
        except Exception:
            pass
        time.sleep(interval)
    raise AssertionError(f"poll_stats timed out after {timeout}s; last={last}")
