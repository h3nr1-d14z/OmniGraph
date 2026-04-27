"""Hub /search HTTP endpoint — thin wrapper over the in-process MCP
``semantic_search_code`` so non-MCP clients can hit the same ranker.

Tests use FastAPI TestClient with the search function monkeypatched so
the endpoint contract is exercised without booting Qdrant or embed-service.
"""

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hub" / "api_server"))
sys.path.insert(0, str(REPO / "hub" / "mcp_server"))
sys.path.insert(0, str(REPO / "hub"))


@pytest.fixture
def hub_app(monkeypatch):
    """Lightweight Hub app: lifespan replaced so tests don't need Qdrant
    / Memgraph / embed-service running, and semantic_search_code is
    stubbed to return a deterministic payload."""
    import contextlib

    @contextlib.asynccontextmanager
    async def fake_lifespan(app):
        yield

    import main as hub_main

    # AUTH_TOKEN was captured at module import; monkeypatch.setenv would
    # have no effect on the existing constant, so set the attribute
    # directly. monkeypatch handles teardown automatically.
    monkeypatch.setattr(hub_main, "AUTH_TOKEN", "test-token")
    monkeypatch.setattr(hub_main.app.router, "lifespan_context", fake_lifespan)

    from models.schema import SemanticSearchResult

    def fake_search(inp):
        return [
            SemanticSearchResult(
                file_path="/src/auth.go",
                machine_id="m1",
                project="demo",
                snippet="auth code",
                score=0.99,
                matched_entities=["AuthHandler"],
            )
        ]

    monkeypatch.setattr("tools.semantic_search.semantic_search_code", fake_search)
    return hub_main.app


def test_search_endpoint_returns_results(hub_app):
    from fastapi.testclient import TestClient

    with TestClient(hub_app) as client:
        r = client.post(
            "/search",
            headers={"Authorization": "Bearer test-token"},
            json={"query": "AuthHandler", "machine_id": "m1"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "results" in body
    assert len(body["results"]) == 1
    hit = body["results"][0]
    assert hit["file_path"] == "/src/auth.go"
    assert hit["machine_id"] == "m1"
    assert hit["score"] == 0.99
    assert hit["matched_entities"] == ["AuthHandler"]


def test_search_endpoint_rejects_missing_auth(hub_app):
    from fastapi.testclient import TestClient

    with TestClient(hub_app) as client:
        r = client.post("/search", json={"query": "anything"})
    assert r.status_code == 401


def test_search_endpoint_rejects_wrong_token(hub_app):
    from fastapi.testclient import TestClient

    with TestClient(hub_app) as client:
        r = client.post(
            "/search",
            headers={"Authorization": "Bearer wrong"},
            json={"query": "anything"},
        )
    assert r.status_code == 403


def test_search_endpoint_validates_body(hub_app):
    """`query` is required; missing it must return 422 (Pydantic), not 500."""
    from fastapi.testclient import TestClient

    with TestClient(hub_app) as client:
        r = client.post(
            "/search",
            headers={"Authorization": "Bearer test-token"},
            json={"machine_id": "m1"},
        )
    assert r.status_code == 422
