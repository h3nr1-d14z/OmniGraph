"""Embed-service down → Hub returns 502 → watcher must retry. Uses respx
to mock the embed call without stopping any real service."""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "hub"))
sys.path.insert(0, str(REPO / "hub" / "mcp_server"))

from api_server.main import AUTH_TOKEN, app  # noqa: E402

EMBED_URL = "http://localhost:8000"


class _FakeQdrant:
    def delete_by_file(self, file_path, machine_id):
        pass

    def upsert(self, vectors, payloads):
        pass


class _FakeMemgraph:
    def atomic_replace_file(self, file_path, machine_id, entities, relations):
        pass

    def upsert_relations(self, relations):
        pass

    def delete_file(self, file_path, machine_id):
        pass


class _FakeContent:
    def upsert_files(self, files, content_hashes=None):
        pass

    def delete_file(self, machine_id, file_path):
        pass

    def refresh_project_tree(self, machine_id, project):
        pass

    def get_content_hashes(self):
        return {}

    def update_content_hash(self, machine_id, file_path, content_hash):
        pass


@pytest.mark.smoke
def test_smoke_offline_recovery_mock():
    """503 from embed-service → 502 from hub /batch."""
    async def _run():
        # Pre-populate app.state so the lifespan is not required.
        app.state.qdrant = _FakeQdrant()
        app.state.memgraph = _FakeMemgraph()
        app.state.content = _FakeContent()
        app.state.content_hashes = {}
        app.state.http = httpx.AsyncClient(timeout=5.0)

        with respx.mock(assert_all_called=False) as mock:
            mock.post(f"{EMBED_URL}/embed").respond(503, json={"detail": "unavailable"})
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                payload = {
                    "machine_id": "smoke-machine",
                    "project": "smoke-proj",
                    "events": [{
                        "type": "MODIFY",
                        "path": "/tmp/smoke_test.py",
                        "machine_id": "smoke-machine",
                        "project": "smoke-proj",
                        "timestamp": 1,
                        "content": "def hello(): pass\n",
                        "content_hash": "smoke-hash-1",
                        "entities": [{"name": "hello", "type": "function", "line": 1, "start_line": 1, "end_line": 1}],
                        "relations": [],
                    }],
                }
                r = await client.post("/batch", json=payload, headers={"Authorization": f"Bearer {AUTH_TOKEN}"})
                assert r.status_code == 502, f"expected 502, got {r.status_code}: {r.text}"

        await app.state.http.aclose()

    asyncio.run(_run())
