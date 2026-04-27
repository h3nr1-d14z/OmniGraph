import os
from urllib.parse import urlparse

import pytest


def pytest_addoption(parser):
    parser.addoption("--smoke", action="store_true", default=False,
                     help="Run @pytest.mark.smoke tests against the live Docker stack.")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--smoke"):
        return
    skip = pytest.mark.skip(reason="Pass --smoke to run live-stack smoke tests")
    for item in items:
        if "smoke" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def require_stack():
    """Skip if hub-api or Qdrant unreachable. Smoke tests opt-in via this fixture."""
    import socket

    import httpx
    hub_url = os.getenv("HUB_URL", "http://localhost:9000")
    qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
    memgraph_bolt = os.getenv("MEMGRAPH_BOLT_URL", "bolt://localhost:7687")

    reasons = []
    for url, name in [(f"{hub_url}/health", "hub-api"), (f"{qdrant_url}/healthz", "qdrant")]:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code not in (200, 204):
                reasons.append(f"{name} returned {r.status_code}")
        except Exception as exc:
            reasons.append(f"{name} unreachable: {exc}")

    parsed = urlparse(memgraph_bolt)
    host, port = parsed.hostname or "localhost", parsed.port or 7687
    try:
        s = socket.create_connection((host, port), timeout=1.0)
        s.close()
    except OSError as exc:
        reasons.append(f"memgraph bolt unreachable: {exc}")
    if reasons:
        pytest.skip(f"Stack not up: {'; '.join(reasons)}")
