"""US-013 acceptance: Hub-side content-hash short-circuit.

Watcher restart leaves Hub's in-memory `lastContentHash` empty, so the
catch-up scan re-emits every file. Without dedup, Hub would
qdrant.delete_by_file + re-embed the entire corpus (167 minutes for 50k
files at 200ms each). This test suite verifies that:

1. The `content_hash` migration is idempotent (open same DB twice).
2. `get_content_hashes()` and `update_content_hash()` round-trip correctly.
3. `_handle_upserts` skips qdrant + /embed when the incoming
   content_hash matches the cached hash for (machine_id, file_path).
4. A different / NULL existing hash falls through to the full embed path.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub", "mcp_server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub"))

from api_server.main import _handle_upserts
from db.content_store import ContentStore
from models.event import Entity, FileEvent


class _FakeMemgraph:
    def __init__(self):
        self.entities = []
        self.relations = []
        self.deleted = []
        self.replaced = []  # list of (file_path, machine_id, entities, relations)

    def delete_file(self, file_path, machine_id):
        self.deleted.append((file_path, machine_id))

    def upsert_entities(self, entities):
        self.entities.extend(entities)

    def upsert_relations(self, relations):
        self.relations.extend(relations)

    def atomic_replace_file(self, file_path, machine_id, entities, relations):
        # US-014: atomic per-file replacement.
        self.replaced.append((file_path, machine_id, list(entities), list(relations)))
        self.deleted.append((file_path, machine_id))
        self.entities.extend(entities)
        self.relations.extend(relations)


class _FakeContent:
    def __init__(self):
        self.files = []
        self.refreshed = None
        self.deleted = []
        self.hash_writes = []
        self.last_hashes = {}

    def upsert_files(self, files, content_hashes=None):
        self.files.extend(files)
        if content_hashes:
            self.last_hashes.update(content_hashes)

    def delete_file(self, machine_id, file_path):
        self.deleted.append((machine_id, file_path))

    def refresh_project_tree(self, machine_id, project):
        self.refreshed = (machine_id, project)

    def update_content_hash(self, machine_id, file_path, content_hash):
        self.hash_writes.append((machine_id, file_path, content_hash))


class _FakeQdrant:
    def __init__(self):
        self.deleted = []
        self.upserts = []

    def delete_by_file(self, file_path, machine_id):
        self.deleted.append((file_path, machine_id))

    def upsert(self, vectors, payloads):
        self.upserts.append((list(vectors), list(payloads)))


class _RecordingHTTP:
    """Fake httpx.AsyncClient: records /embed calls and returns dummy vectors."""

    def __init__(self, vector_dim: int = 4):
        self.calls = []
        self.vector_dim = vector_dim

    async def post(self, url, *, json):
        self.calls.append({"url": url, "json": json})

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        n = len(json["texts"])
        embeddings = [[0.0] * self.vector_dim for _ in range(n)]
        return _Resp({"embeddings": embeddings, "vector_dim": self.vector_dim, "backend": "fake"})


def _make_event(machine_id="m1", project="demo", path="/src/main.go", content="package main\nfunc main() {}\n", content_hash="hash-v1"):
    return FileEvent(
        type="MODIFY",
        path=path,
        project=project,
        machine_id=machine_id,
        timestamp=1,
        content=content,
        content_hash=content_hash,
        entities=[Entity(name="main", type="function", line=2, start_line=2, end_line=2)],
    )


# --- Schema migration ---------------------------------------------------


def test_content_hash_column_idempotent_migration(tmp_path):
    """Opening the same DB twice must not raise (analog of pg's
    isDuplicateColumnError handling)."""
    db_path = str(tmp_path / "content.db")
    store1 = ContentStore(db_path)
    # First open ran ALTER TABLE ADD COLUMN. Re-opening must be idempotent.
    store2 = ContentStore(db_path)

    # Both connections see the column.
    assert store1.get_content_hashes() == {}
    assert store2.get_content_hashes() == {}


# --- ContentStore round-trip --------------------------------------------


def test_get_content_hashes_returns_dict(tmp_path):
    """Files with non-NULL hash appear in the dict; NULL rows are excluded
    so legacy data self-heals on first watcher event."""
    store = ContentStore(str(tmp_path / "content.db"))
    store.upsert_files(
        [
            ("m1", "demo", "/a.go", "package a"),
            ("m1", "demo", "/b.go", "package b"),
        ],
        content_hashes={("m1", "/a.go"): "hash-a"},
    )

    hashes = store.get_content_hashes()
    assert hashes == {("m1", "/a.go"): "hash-a"}


def test_update_content_hash_persists(tmp_path):
    """Hash written via update_content_hash must round-trip via get_content_hashes."""
    store = ContentStore(str(tmp_path / "content.db"))
    store.upsert_files([("m1", "demo", "/a.go", "package a")])  # NULL hash

    assert store.get_content_hashes() == {}

    store.update_content_hash("m1", "/a.go", "hash-a")

    assert store.get_content_hashes() == {("m1", "/a.go"): "hash-a"}

    # Stats reflect the new column.
    stats = store.stats()
    assert stats["content_hashed"] == 1


# --- _handle_upserts short-circuit --------------------------------------


def test_handle_upserts_skips_embed_when_hash_matches():
    """Identical content_hash → no /embed, no qdrant churn, and (per US-014's
    atomic-replace integration) no graph re-replacement either: graph state
    already reflects this content. Content row's updated_at still bumps so
    the watcher's last-seen heartbeat moves forward."""
    memgraph = _FakeMemgraph()
    qdrant = _FakeQdrant()
    content = _FakeContent()
    http = _RecordingHTTP()
    cache = {("m1", "/src/main.go"): "hash-v1"}

    ev = _make_event(content_hash="hash-v1")

    asyncio.run(_handle_upserts([ev], "m1", "demo", qdrant, memgraph, content, http, cache))

    assert http.calls == [], "embed must not be called when hashes match"
    assert qdrant.upserts == [], "qdrant.upsert must not run on hash match"
    assert qdrant.deleted == [], "qdrant.delete_by_file must not run on hash match"
    # Graph atomic-replace also skipped (state already correct).
    assert memgraph.replaced == []
    # Content row STILL refreshed (updated_at bump for heartbeat) — this is
    # the crucial part of the contract: hash short-circuit is invisible to
    # downstream observers of content_store freshness.
    assert len(content.files) == 1
    assert content.refreshed == ("m1", "demo")
    # Cache unchanged (no pending write needed; hash already matched).
    assert cache == {("m1", "/src/main.go"): "hash-v1"}


def test_handle_upserts_runs_embed_on_hash_change():
    """Different content_hash → full re-embed path. After success, cache and
    persistent store are both updated to the new hash."""
    memgraph = _FakeMemgraph()
    qdrant = _FakeQdrant()
    content = _FakeContent()
    http = _RecordingHTTP()
    cache = {("m1", "/src/main.go"): "hash-v1"}

    ev = _make_event(content_hash="hash-v2", content="package main\nfunc main() { return }\n")

    asyncio.run(_handle_upserts([ev], "m1", "demo", qdrant, memgraph, content, http, cache))

    assert len(http.calls) == 1, "embed must be called exactly once on hash change"
    assert http.calls[0]["url"].endswith("/embed")
    assert qdrant.deleted == [("/src/main.go", "m1")]
    assert len(qdrant.upserts) == 1
    # Cache + DB reflect the new hash post-success.
    assert cache[("m1", "/src/main.go")] == "hash-v2"
    assert content.hash_writes == [("m1", "/src/main.go", "hash-v2")]


def test_handle_upserts_runs_embed_on_first_ingest_with_null_existing_hash():
    """File with no prior cache entry → full embed path (self-healing for
    legacy NULL rows). After success, hash is recorded so the second identical
    batch short-circuits."""
    memgraph = _FakeMemgraph()
    qdrant = _FakeQdrant()
    content = _FakeContent()
    http = _RecordingHTTP()
    cache: dict[tuple[str, str], str] = {}

    ev = _make_event(content_hash="hash-v1")

    asyncio.run(_handle_upserts([ev], "m1", "demo", qdrant, memgraph, content, http, cache))

    assert len(http.calls) == 1, "first-touch must run /embed"
    assert qdrant.deleted == [("/src/main.go", "m1")]
    assert len(qdrant.upserts) == 1
    # Hash now cached + persisted.
    assert cache == {("m1", "/src/main.go"): "hash-v1"}
    assert content.hash_writes == [("m1", "/src/main.go", "hash-v1")]

    # Second ingest with same hash → short-circuit.
    http2 = _RecordingHTTP()
    qdrant2 = _FakeQdrant()
    asyncio.run(_handle_upserts([ev], "m1", "demo", qdrant2, memgraph, content, http2, cache))
    assert http2.calls == []
    assert qdrant2.upserts == []


def test_handle_upserts_does_not_persist_hash_on_embed_failure():
    """Embed failure → 502, hash NOT written. Watcher retries; next batch
    should still take the full path."""
    from fastapi import HTTPException

    class _FailingHTTP:
        async def post(self, *args, **kwargs):
            raise RuntimeError("embed-service down")

    memgraph = _FakeMemgraph()
    qdrant = _FakeQdrant()
    content = _FakeContent()
    cache: dict[tuple[str, str], str] = {}

    ev = _make_event(content_hash="hash-v1")

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_handle_upserts([ev], "m1", "demo", qdrant, memgraph, content, _FailingHTTP(), cache))
    assert excinfo.value.status_code == 502

    # Cache + DB must NOT record the hash; partial-success contract preserved.
    assert cache == {}
    assert content.hash_writes == []
