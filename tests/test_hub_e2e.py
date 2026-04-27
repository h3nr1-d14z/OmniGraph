"""End-to-end test for Phase 1: Hub (Qdrant + Memgraph + Embed)."""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub", "mcp_server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub", "embed_service"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub"))

import httpx
import pytest
from api_server.main import (
    _build_embedding_chunks,
    _collect_stats,
    _handle_delete,
    _handle_upserts,
    _slice_symbol_chunk,
    _verify_auth,
)
from db.content_store import ContentStore
from db.memgraph_client import MemgraphCodeGraph
from db.qdrant_client import QdrantCodeStore
from fastapi import HTTPException
from models.event import Entity, FileEvent
from starlette.requests import Request

EMBED_URL = os.getenv("EMBED_SERVICE_URL", "http://localhost:8000")


def _embed_vectors():
    print("[test] embed service /health ...")
    r = httpx.get(f"{EMBED_URL}/health")
    assert r.status_code == 200, r.text
    print("[test] embed service OK")

    print("[test] embed service /embed ...")
    r = httpx.post(f"{EMBED_URL}/embed", json={"texts": ["func hashPassword", "class User"]}, timeout=120.0)  # fmt: skip
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["embeddings"]) == 2
    assert data["vector_dim"] == 768
    print(f"[test] embed OK: backend={data['backend']}, dim={data['vector_dim']}")
    return data["embeddings"]


@pytest.fixture
def vectors():
    return _embed_vectors()


def test_embed_service(vectors):
    assert len(vectors) == 2


def test_content_store_fts_search_sync(tmp_path):
    store = ContentStore(str(tmp_path / "content.db"))
    machine_id = "m-fts"
    project = "demo"
    file_path = "/workspace/demo/src/search.go"

    store.upsert_file(machine_id, project, file_path, "package main\nfunc AlphaSearch() string { return \"needle\" }\n")
    results = store.search_files_exact("AlphaSearch needle", machine_id=machine_id, project_scope=project)
    assert results
    assert results[0]["file_path"] == file_path

    store.upsert_file(machine_id, project, file_path, "package main\nfunc BetaSearch() string { return \"haystack\" }\n")
    assert not store.search_files_exact("AlphaSearch needle", machine_id=machine_id, project_scope=project)
    results = store.search_files_exact("BetaSearch haystack", machine_id=machine_id, project_scope=project)
    assert results
    assert results[0]["file_path"] == file_path

    store.delete_file(machine_id, file_path)
    assert not store.search_files_exact("BetaSearch haystack", machine_id=machine_id, project_scope=project)


def test_content_store_refresh_project_tree(tmp_path):
    store = ContentStore(str(tmp_path / "content.db"))
    machine_id = "m-tree"
    project = "demo"
    store.upsert_files(
        [
            (machine_id, project, "/workspace/demo/src/main.go", "package main"),
            (machine_id, project, "/workspace/demo/src/lib/util.go", "package lib"),
        ]
    )
    store.refresh_project_tree(machine_id, project)

    tree = store.get_project_tree(machine_id, project)
    assert tree is not None
    assert tree.startswith("demo/")
    assert "src/" in tree
    assert "main.go" in tree
    assert "util.go" in tree

    store.delete_file(machine_id, "/workspace/demo/src/lib/util.go")
    store.refresh_project_tree(machine_id, project)
    tree = store.get_project_tree(machine_id, project)
    assert tree is not None
    assert "util.go" not in tree


def test_content_store_stats_respects_scope(tmp_path):
    store = ContentStore(str(tmp_path / "content.db"))
    store.upsert_files(
        [
            ("m1", "alpha", "/workspace/alpha/src/main.go", "package main"),
            ("m1", "beta", "/workspace/beta/src/util.go", "package beta"),
            ("m2", "alpha", "/workspace/alpha/src/worker.go", "package worker"),
        ]
    )
    store.refresh_project_tree("m1", "alpha")
    store.refresh_project_tree("m1", "beta")
    store.refresh_project_tree("m2", "alpha")
    store.upsert_query_embedding("query:alpha", [0.1, 0.2])

    all_stats = store.stats()
    assert all_stats["files"] == 3
    assert all_stats["project_trees"] == 3
    assert all_stats["global_query_embeddings"] == 1
    assert all_stats["fts_rows"] == 3
    assert all_stats["db_path"].endswith("content.db")

    scoped_stats = store.stats(machine_id="m1", project="alpha")
    assert scoped_stats["files"] == 1
    assert scoped_stats["project_trees"] == 1
    assert scoped_stats["fts_rows"] == 1
    assert scoped_stats["global_query_embeddings"] == 1


def test_slice_symbol_chunk_uses_precomputed_lines():
    lines = ["package main", "", "func alpha() {}"]
    assert _slice_symbol_chunk(lines, 3, 3) == "func alpha() {}"


def test_build_embedding_chunks_by_symbol_range():
    content = "package main\n\nfunc alpha() {\n\tprintln(\"a\")\n}\n\nfunc beta() {\n\tprintln(\"b\")\n}\n"
    ev = FileEvent(
        type="CREATE",
        path="/src/main.go",
        project="demo",
        machine_id="m1",
        timestamp=1,
        content=content,
        entities=[
            Entity(name="alpha", type="function", line=3, start_line=3, end_line=5),
            Entity(name="beta", type="function", line=7, start_line=7, end_line=9),
        ],
    )

    chunks = _build_embedding_chunks(ev, "m1", "demo")
    assert len(chunks) == 2
    assert chunks[0]["entity"] == "alpha"
    assert "func alpha()" in chunks[0]["snippet"]
    assert chunks[1]["entity"] == "beta"
    assert "func beta()" in chunks[1]["snippet"]


class _FakeMemgraph:
    def __init__(self):
        self.entities: list = []
        self.relations: list = []
        self.deleted = []

    def delete_file(self, file_path, machine_id):
        self.deleted.append((file_path, machine_id))

    def upsert_entities(self, entities):
        self.entities.extend(entities)

    def upsert_relations(self, relations):
        self.relations.extend(relations)

    def atomic_replace_file(self, file_path, machine_id, entities, relations):
        # Mirror the real client: delete + entity upsert + relation upsert,
        # all observable on existing fake attributes so legacy assertions
        # continue to work under the US-014 atomic path.
        self.deleted.append((file_path, machine_id))
        self.entities.extend(entities)
        self.relations.extend(relations)


class _FakeContent:
    def __init__(self):
        self.files = None
        self.refreshed = None
        self.deleted = []
        self.hash_writes = []

    def upsert_files(self, files, content_hashes=None):
        self.files = files
        self.last_hashes = content_hashes or {}

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
        self.upserts.append((vectors, payloads))


class _FakeHTTP:
    async def post(self, *args, **kwargs):
        raise RuntimeError("skip embeddings")


def test_handle_upserts_passes_graph_relations():
    memgraph = _FakeMemgraph()
    qdrant = _FakeQdrant()
    content = _FakeContent()
    ev = FileEvent(
        type="CREATE",
        path="/src/main.go",
        project="demo",
        machine_id="m1",
        timestamp=1,
        content="package main\nfunc main() { helper() }\n",
        content_hash="abc",
        entities=[Entity(name="main", type="function", line=2, start_line=2, end_line=2)],
        relations=[
            {"type": "CONTAINS", "target": "main", "target_type": "function", "line": 2, "confidence": "syntax"},
            {"type": "CALLS_SYNTAX", "source": "main", "target": "helper", "target_type": "symbol", "line": 2, "confidence": "syntax"},
            {"type": "IMPORTS", "target": "fmt", "target_type": "module", "line": 1, "confidence": "syntax"},
            {"type": "CALLS_RESOLVED", "source": "main", "target": "fmt.Println", "target_type": "symbol", "line": 2, "confidence": "semantic", "layer": "semantic", "status": "resolved", "symbol_id": "go:fmt.Println", "target_ref": "fmt.Println", "package": "fmt", "language": "go"},
        ],
    )

    import asyncio

    import pytest as _pytest
    from fastapi import HTTPException as _HTTPException
    # US-004: embed failure now raises 502 (was silently swallowed). Graph +
    # content writes still committed BEFORE the exception per partial-success
    # contract; the test verifies that contract.
    with _pytest.raises(_HTTPException) as excinfo:
        asyncio.run(_handle_upserts([ev], "m1", "demo", qdrant, memgraph, content, _FakeHTTP()))
    assert excinfo.value.status_code == 502

    assert qdrant.deleted == [("/src/main.go", "m1")]
    assert memgraph.deleted == [("/src/main.go", "m1")]
    assert memgraph.entities == [
        {
            "machine_id": "m1",
            "project": "demo",
            "file_path": "/src/main.go",
            "name": "main",
            "type": "function",
            "content_hash": "abc",
            "start_line": 2,
            "end_line": 2,
        }
    ]
    relations_by_type = {rel["type"]: rel for rel in memgraph.relations}
    assert set(relations_by_type) == {"CONTAINS", "CALLS_SYNTAX", "IMPORTS", "CALLS_RESOLVED"}
    assert relations_by_type["IMPORTS"]["layer"] == "syntax"
    assert relations_by_type["IMPORTS"]["status"] == "observed"
    assert relations_by_type["CALLS_RESOLVED"] == {
        "machine_id": "m1",
        "project": "demo",
        "file_path": "/src/main.go",
        "type": "CALLS_RESOLVED",
        "source": "main",
        "target": "fmt.Println",
        "target_type": "symbol",
        "line": 2,
        "confidence": "semantic",
        "layer": "semantic",
        "status": "resolved",
        "symbol_id": "go:fmt.Println",
        "target_ref": "fmt.Println",
        "package": "fmt",
        "language": "go",
    }
    assert content.files == [("m1", "demo", "/src/main.go", "package main\nfunc main() { helper() }\n")]
    assert content.refreshed == ("m1", "demo")


def test_handle_upserts_semantic_only_event_preserves_existing_state():
    memgraph = _FakeMemgraph()
    qdrant = _FakeQdrant()
    content = _FakeContent()
    ev = FileEvent(
        type="MODIFY",
        path="/src/main.go",
        project="demo",
        machine_id="m1",
        timestamp=1,
        content_hash="abc",
        relations=[
            {
                "type": "CALLS_RESOLVED",
                "source": "main",
                "target": "fmt.Println",
                "target_type": "symbol",
                "line": 2,
                "confidence": "semantic",
                "layer": "semantic",
                "status": "resolved",
                "symbol_id": "go:fmt.Println",
                "target_ref": "fmt.Println",
                "package": "fmt",
                "language": "go",
            }
        ],
    )

    import asyncio
    asyncio.run(_handle_upserts([ev], "m1", "demo", qdrant, memgraph, content, _FakeHTTP()))

    assert qdrant.deleted == []
    assert qdrant.upserts == []
    assert memgraph.deleted == []
    assert memgraph.entities == []
    assert content.deleted == []
    assert content.files == []
    assert content.refreshed is None
    assert memgraph.relations == [
        {
            "machine_id": "m1",
            "project": "demo",
            "file_path": "/src/main.go",
            "type": "CALLS_RESOLVED",
            "source": "main",
            "target": "fmt.Println",
            "target_type": "symbol",
            "line": 2,
            "confidence": "semantic",
            "layer": "semantic",
            "status": "resolved",
            "symbol_id": "go:fmt.Println",
            "target_ref": "fmt.Println",
            "package": "fmt",
            "language": "go",
        }
    ]


def test_handle_delete_cleans_rename_paths():
    qdrant = _FakeQdrant()
    memgraph = _FakeMemgraph()
    content = _FakeContent()
    ev = FileEvent(
        type="RENAME",
        path="/src/new.go",
        old_path="/src/old.go",
        project="demo",
        machine_id="m1",
        timestamp=1,
    )

    _handle_delete(ev, qdrant, memgraph, content, "fallback-machine", "fallback-project")

    assert qdrant.deleted == [("/src/new.go", "m1"), ("/src/old.go", "m1")]
    assert memgraph.deleted == [("/src/new.go", "m1"), ("/src/old.go", "m1")]
    assert content.deleted == [("m1", "/src/new.go"), ("m1", "/src/old.go")]
    assert content.refreshed == ("m1", "demo")


class _FakeStatsClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[tuple[str | None, str | None]] = []

    def stats(self, machine_id: str | None = None, project: str | None = None):
        self.calls.append((machine_id, project))
        if self.error is not None:
            raise self.error
        return self.result


def test_collect_stats_isolates_component_errors():
    content = _FakeStatsClient(result={"files": 2})
    qdrant = _FakeStatsClient(error=RuntimeError("qdrant unavailable"))
    memgraph = _FakeStatsClient(result={"entities": 3, "edges": 4})

    stats = _collect_stats(
        content=content,
        qdrant=qdrant,
        memgraph=memgraph,
        machine_id="m1",
        project="demo",
    )

    assert stats["status"] == "degraded"
    assert stats["scope"] == {"machine_id": "m1", "project": "demo"}
    assert stats["content_store"] == {"files": 2}
    assert stats["qdrant"] is None
    assert stats["memgraph"] == {"entities": 3, "edges": 4}
    assert stats["errors"] == {"qdrant": "unavailable"}
    assert content.calls == [("m1", "demo")]
    assert qdrant.calls == [("m1", "demo")]
    assert memgraph.calls == [("m1", "demo")]


def test_verify_auth_requires_bearer_auth():
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as excinfo:
        _verify_auth(request)
    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "Missing bearer token"


def test_collect_stats_returns_scoped_stats():
    content = _FakeStatsClient(result={"files": 2})
    qdrant = _FakeStatsClient(result={"points": 5, "exact": False})
    memgraph = _FakeStatsClient(result={"entities": 3, "edges": 4})

    stats = _collect_stats(
        content=content,
        qdrant=qdrant,
        memgraph=memgraph,
        machine_id="m1",
        project="demo",
    )

    assert stats == {
        "status": "ok",
        "scope": {"machine_id": "m1", "project": "demo"},
        "content_store": {"files": 2},
        "qdrant": {"points": 5, "exact": False},
        "memgraph": {"entities": 3, "edges": 4},
        "errors": {},
    }


class _FakeQdrantCountResult:
    count = 7


class _FakeQdrantClient:
    def __init__(self):
        self.calls = []

    def count(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeQdrantCountResult()


def test_qdrant_stats_uses_exact_for_scoped_counts(monkeypatch):
    monkeypatch.setenv("QDRANT_STATS_EXACT", "false")
    monkeypatch.setenv("QDRANT_STATS_SCOPED_EXACT", "true")
    store = object.__new__(QdrantCodeStore)
    store.collection = "test"
    store.client = _FakeQdrantClient()

    unscoped = store.stats()
    scoped = store.stats(machine_id="m1", project="demo")

    assert unscoped == {"collection": "test", "points": 7, "exact": False}
    assert scoped == {"collection": "test", "points": 7, "exact": True}
    assert store.client.calls[0]["exact"] is False
    assert store.client.calls[0]["count_filter"] is None
    assert store.client.calls[1]["exact"] is True
    assert store.client.calls[1]["count_filter"] is not None


def test_qdrant(vectors):
    print("[test] qdrant upsert ...")
    store = QdrantCodeStore()
    machine_id = str(uuid.uuid4())
    payloads = [
        {
            "machine_id": machine_id,
            "project": "demo",
            "file_path": "/src/auth.go",
            "entity": "hashPassword",
            "snippet": "func hashPassword(p string) string",
        },
        {
            "machine_id": machine_id,
            "project": "demo",
            "file_path": "/src/user.go",
            "entity": "User",
            "snippet": "type User struct { ID int }",
        },
    ]
    store.upsert(vectors, payloads)
    print("[test] qdrant upsert OK")

    print("[test] qdrant search ...")
    # Search with same query
    r = httpx.post(f"{EMBED_URL}/embed", json={"texts": ["password hashing function"]})  # fmt: skip
    query_vec = r.json()["embeddings"][0]
    results = store.search(query_vec, project_scope="demo", machine_id=machine_id, limit=5)
    assert len(results) > 0
    print(f"[test] qdrant search OK: top={results[0]['payload']['file_path']} score={results[0]['score']:.3f}")  # fmt: skip

    print("[test] qdrant tombstone ...")
    store.delete_by_file("/src/auth.go", machine_id)
    results_after = store.search(query_vec, project_scope="demo", machine_id=machine_id, limit=5)
    remaining = [r for r in results_after if r["payload"]["file_path"] == "/src/auth.go"]
    assert len(remaining) == 0, "tombstone failed"
    print("[test] qdrant tombstone OK")


def test_memgraph():
    print("[test] memgraph connect ...")
    graph = MemgraphCodeGraph()
    print("[test] memgraph connect OK")

    machine_id = f"machine-test-{uuid.uuid4().hex}"
    project = "demo"

    print("[test] memgraph upsert entities ...")
    graph.upsert_entity(machine_id, project, "/src/main.go", "main", "function", "abc123")
    graph.upsert_entity(machine_id, project, "/src/utils.go", "hashPassword", "function", "def456")
    print("[test] memgraph upsert OK")

    print("[test] memgraph dependency ...")
    graph.upsert_dependency("/src/main.go", "/src/utils.go", "DEPENDS_ON", machine_id)
    print("[test] memgraph dependency OK")

    print("[test] memgraph query downstream ...")
    deps = graph.get_dependencies("main", direction="downstream", machine_id=machine_id)
    assert len(deps["edges"]) >= 1
    print(f"[test] memgraph downstream OK: {len(deps['edges'])} edges")

    print("[test] memgraph syntax relations ...")
    graph.upsert_entities(
        [
            {
                "machine_id": machine_id,
                "project": project,
                "file_path": "/src/graph.go",
                "name": "GraphMain",
                "type": "function",
                "content_hash": "ghi789",
                "start_line": 3,
                "end_line": 6,
            }
        ]
    )
    graph.upsert_relations(
        [
            {
                "machine_id": machine_id,
                "project": project,
                "file_path": "/src/graph.go",
                "type": "IMPORTS",
                "source": "",
                "target": "fmt",
                "target_type": "module",
                "line": 3,
                "confidence": "syntax",
            },
            {
                "machine_id": machine_id,
                "project": project,
                "file_path": "/src/graph.go",
                "type": "CALLS_SYNTAX",
                "source": "GraphMain",
                "target": "fmt.Println",
                "target_type": "symbol",
                "line": 4,
                "confidence": "syntax",
            },
            {
                "machine_id": machine_id,
                "project": project,
                "file_path": "/src/graph.go",
                "type": "IMPORTS_RESOLVED",
                "source": "",
                "target": "fmt",
                "target_type": "module",
                "line": 3,
                "confidence": "semantic",
                "layer": "semantic",
                "status": "resolved",
                "symbol_id": "",
                "target_ref": "fmt",
                "package": "fmt",
                "language": "go",
            },
            {
                "machine_id": machine_id,
                "project": project,
                "file_path": "/src/graph.go",
                "type": "CALLS_RESOLVED",
                "source": "GraphMain",
                "target": "fmt.Println",
                "target_type": "symbol",
                "line": 4,
                "confidence": "semantic",
                "layer": "semantic",
                "status": "resolved",
                "symbol_id": "go:fmt.Println",
                "target_ref": "fmt.Println",
                "package": "fmt",
                "language": "go",
            },
            {
                "machine_id": machine_id,
                "project": project,
                "file_path": "/src/graph.go",
                "type": "REFERENCES",
                "source": "GraphMain",
                "target": "fmt.Println",
                "target_type": "symbol",
                "line": 4,
                "confidence": "semantic",
                "layer": "semantic",
                "status": "resolved",
                "symbol_id": "go:fmt.Println",
                "target_ref": "fmt.Println",
                "package": "fmt",
                "language": "go",
            },
        ]
    )
    graph_deps = graph.get_dependencies("GraphMain", direction="downstream", machine_id=machine_id)
    assert any(edge["relation"] == "CALLS_SYNTAX" and edge["to"].endswith(":fmt.Println") for edge in graph_deps["edges"]), graph_deps
    assert any(edge["relation"] == "CALLS_RESOLVED" and edge["symbol_id"] == "go:fmt.Println" for edge in graph_deps["edges"]), graph_deps
    assert any(edge["relation"] == "REFERENCES" and edge["to_type"] == "ResolvedSymbol" for edge in graph_deps["edges"]), graph_deps
    assert any(edge["relation"] == "IMPORTS_RESOLVED" for edge in graph_deps["edges"]), graph_deps
    stats = graph.stats(machine_id=machine_id, project=project)
    assert stats["edges"] >= 6, stats

    print("[test] memgraph tombstone ...")
    graph.delete_file("/src/utils.go", machine_id)
    deps_after = graph.get_dependencies("main", direction="downstream", machine_id=machine_id)
    assert len(deps_after["edges"]) == 0, "memgraph tombstone failed"
    print("[test] memgraph tombstone OK")

    graph.close()


def main():
    print("=== OmniGraph Phase 1 E2E Test ===\n")

    embedded_vectors = _embed_vectors()
    test_qdrant(embedded_vectors)
    test_memgraph()

    print("\n=== All Phase 1 tests passed ===")


if __name__ == "__main__":
    main()
