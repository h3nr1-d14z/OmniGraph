"""End-to-end test for Phase 1: Hub (Qdrant + Memgraph + Embed)."""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub", "mcp_server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub", "embed_service"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub"))

import httpx
import pytest
from api_server.main import _build_embedding_chunks, _slice_symbol_chunk
from db.content_store import ContentStore
from db.qdrant_client import QdrantCodeStore
from db.memgraph_client import MemgraphCodeGraph
from models.event import Entity, FileEvent

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

    machine_id = "machine-test-01"
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
