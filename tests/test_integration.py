"""Phase 4: Full integration test — seed data via Hub API, then verify MCP tools."""

import json
import os
import subprocess
import sys
import tempfile
import time

# Paths
PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))

# Ensure imports resolve for both mcp_server and embed_service packages
sys.path.insert(0, os.path.join(PROJECT_ROOT, "hub", "mcp_server"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "hub"))

os.environ["QDRANT_COLLECTION"] = os.getenv("QDRANT_TEST_COLLECTION", "omnigraph_test_integration")

import httpx

FIXTURES = os.path.join(PROJECT_ROOT, "tests", "fixtures")
HUB_API_URL = os.getenv("HUB_API_URL", "http://localhost:9000")
EMBED_URL = os.getenv("EMBED_SERVICE_URL", "http://localhost:8000")
AUTH_TOKEN = os.getenv("HUB_AUTH_TOKEN", "changeme")

MACHINE_ID = "test-machine-01"
SEEDED = False


def start_docker():
    print("[setup] Starting Docker services...")
    subprocess.run(
        ["docker", "compose", "up", "-d", "qdrant", "memgraph"],
        cwd=PROJECT_ROOT,
        check=True,
    )
    # Wait for health
    for _ in range(30):
        try:
            r = httpx.get("http://localhost:6333/healthz")
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    print("[setup] Docker ready")


def embed_text(texts: list[str]) -> list[list[float]]:
    r = httpx.post(f"{EMBED_URL}/embed", json={"texts": texts}, timeout=60.0)
    r.raise_for_status()
    return r.json()["embeddings"]


def cleanup_seed_data():
    from db.qdrant_client import QdrantCodeStore
    from db.memgraph_client import MemgraphCodeGraph
    from db.content_store import ContentStore

    qdrant = QdrantCodeStore()
    memgraph = MemgraphCodeGraph()
    store = ContentStore()

    if qdrant.client.collection_exists(qdrant.collection):
        qdrant.client.delete_collection(qdrant.collection)
    qdrant._ensure_collection()

    for path in [
        "/tests/fixtures/project_a/src/model.go",
        "/tests/fixtures/project_a/src/handler.go",
        "/tests/fixtures/project_a/src/repo.go",
        "/tests/fixtures/project_b/src/types.ts",
        "/tests/fixtures/project_b/src/api.ts",
    ]:
        qdrant.delete_by_file(path, MACHINE_ID)
        memgraph.delete_file(path, MACHINE_ID)
        store.delete_file(MACHINE_ID, path)
    memgraph.close()


def ensure_seeded():
    global SEEDED
    if SEEDED:
        return
    cleanup_seed_data()
    seed_project_a()
    seed_project_b()
    SEEDED = True


def seed_project_a():
    """Seed Project A (Go backend) into Hub."""
    print("[seed] Project A (Go)...")
    project = "project_a"

    files = [
        ("src/model.go", open(os.path.join(FIXTURES, project, "src/model.go")).read()),
        ("src/handler.go", open(os.path.join(FIXTURES, project, "src/handler.go")).read()),
        ("src/repo.go", open(os.path.join(FIXTURES, project, "src/repo.go")).read()),
    ]

    events = []
    for path, content in files:
        events.append({
            "type": "CREATE",
            "path": f"/tests/fixtures/{project}/{path}",
            "project": project,
            "machine_id": MACHINE_ID,
            "timestamp": int(time.time()),
            "content": content,
            "entities": [],
        })

    # Send via Hub API batch endpoint (if available) or direct DB insert
    # For integration test we insert directly into Qdrant + Memgraph to avoid needing Hub API server running
    from db.qdrant_client import QdrantCodeStore
    from db.memgraph_client import MemgraphCodeGraph
    from db.content_store import ContentStore

    qdrant = QdrantCodeStore()
    memgraph = MemgraphCodeGraph()
    store = ContentStore()

    payloads = []
    texts = []
    for path, content in files:
        full_path = f"/tests/fixtures/{project}/{path}"
        store.upsert_file(MACHINE_ID, project, full_path, content)

        if "type User struct" in content:
            memgraph.upsert_entity(MACHINE_ID, project, full_path, "User", "struct", "")
            snippet = "type User struct {\n    ID       int\n    Email    string\n    Password string\n}"
            texts.append(snippet[:2000])
            payloads.append({
                "machine_id": MACHINE_ID,
                "project": project,
                "file_path": full_path,
                "entity": "User",
                "entity_type": "struct",
                "chunk_id": "0",
                "start_line": 3,
                "end_line": 7,
                "snippet": snippet[:500],
            })
        if "func HashPassword" in content:
            memgraph.upsert_entity(MACHINE_ID, project, full_path, "HashPassword", "function", "")
            snippet = "func HashPassword(password string) string {\n    return fmt.Sprintf(\"hashed:%s\", password)\n}"
            texts.append(snippet[:2000])
            payloads.append({
                "machine_id": MACHINE_ID,
                "project": project,
                "file_path": full_path,
                "entity": "HashPassword",
                "entity_type": "function",
                "chunk_id": "0",
                "start_line": 9,
                "end_line": 11,
                "snippet": snippet[:500],
            })
        if "type GetUserHandler" in content:
            memgraph.upsert_entity(MACHINE_ID, project, full_path, "GetUserHandler", "struct", "")
            snippet = "type GetUserHandler struct {\n    Repo *UserRepository\n}"
            texts.append(snippet[:2000])
            payloads.append({
                "machine_id": MACHINE_ID,
                "project": project,
                "file_path": full_path,
                "entity": "GetUserHandler",
                "entity_type": "struct",
                "chunk_id": "0",
                "start_line": 8,
                "end_line": 10,
                "snippet": snippet[:500],
            })
        if "type UserRepository" in content:
            memgraph.upsert_entity(MACHINE_ID, project, full_path, "UserRepository", "struct", "")
            snippet = "type UserRepository struct {}"
            texts.append(snippet[:2000])
            payloads.append({
                "machine_id": MACHINE_ID,
                "project": project,
                "file_path": full_path,
                "entity": "UserRepository",
                "entity_type": "struct",
                "chunk_id": "0",
                "start_line": 7,
                "end_line": 7,
                "snippet": snippet[:500],
            })

    vectors = embed_text(texts)
    qdrant.upsert(vectors, payloads)

    # Dependencies
    memgraph.upsert_dependency(
        "/tests/fixtures/project_a/src/handler.go",
        "/tests/fixtures/project_a/src/repo.go",
        "DEPENDS_ON",
        MACHINE_ID,
    )
    memgraph.upsert_dependency(
        "/tests/fixtures/project_a/src/handler.go",
        "/tests/fixtures/project_a/src/model.go",
        "DEPENDS_ON",
        MACHINE_ID,
    )
    memgraph.upsert_dependency(
        "/tests/fixtures/project_a/src/repo.go",
        "/tests/fixtures/project_a/src/model.go",
        "DEPENDS_ON",
        MACHINE_ID,
    )

    store.refresh_project_tree(MACHINE_ID, project)

    memgraph.close()
    print("[seed] Project A done")


def seed_project_b():
    """Seed Project B (TypeScript frontend) into Hub."""
    print("[seed] Project B (TypeScript)...")
    project = "project_b"

    files = [
        ("src/types.ts", open(os.path.join(FIXTURES, project, "src/types.ts")).read()),
        ("src/api.ts", open(os.path.join(FIXTURES, project, "src/api.ts")).read()),
    ]

    from db.qdrant_client import QdrantCodeStore
    from db.memgraph_client import MemgraphCodeGraph
    from db.content_store import ContentStore

    qdrant = QdrantCodeStore()
    memgraph = MemgraphCodeGraph()
    store = ContentStore()

    payloads = []
    texts = []
    for path, content in files:
        full_path = f"/tests/fixtures/{project}/{path}"
        store.upsert_file(MACHINE_ID, project, full_path, content)

        if "interface UserProfile" in content:
            memgraph.upsert_entity(MACHINE_ID, project, full_path, "UserProfile", "interface", "")
            snippet = "export interface UserProfile {\n  id: number\n  email: string\n}"
            texts.append(snippet[:2000])
            payloads.append({
                "machine_id": MACHINE_ID,
                "project": project,
                "file_path": full_path,
                "entity": "UserProfile",
                "entity_type": "interface",
                "chunk_id": "0",
                "start_line": 1,
                "end_line": 4,
                "snippet": snippet[:500],
            })
        if "interface AuthState" in content:
            memgraph.upsert_entity(MACHINE_ID, project, full_path, "AuthState", "interface", "")
            snippet = "export interface AuthState {\n  token: string | null\n}"
            texts.append(snippet[:2000])
            payloads.append({
                "machine_id": MACHINE_ID,
                "project": project,
                "file_path": full_path,
                "entity": "AuthState",
                "entity_type": "interface",
                "chunk_id": "1",
                "start_line": 6,
                "end_line": 8,
                "snippet": snippet[:500],
            })
        if "function fetchUser" in content or "fetchUser" in content:
            memgraph.upsert_entity(MACHINE_ID, project, full_path, "fetchUser", "function", "")
            snippet = "export async function fetchUser(id: number): Promise<UserProfile> {\n  const response = await fetch(`/api/users/${id}`)\n  return response.json()\n}"
            texts.append(snippet[:2000])
            payloads.append({
                "machine_id": MACHINE_ID,
                "project": project,
                "file_path": full_path,
                "entity": "fetchUser",
                "entity_type": "function",
                "chunk_id": "0",
                "start_line": 3,
                "end_line": 6,
                "snippet": snippet[:500],
            })

    vectors = embed_text(texts)
    qdrant.upsert(vectors, payloads)

    store.refresh_project_tree(MACHINE_ID, project)

    memgraph.upsert_dependency(
        "/tests/fixtures/project_b/src/api.ts",
        "/tests/fixtures/project_b/src/types.ts",
        "DEPENDS_ON",
        MACHINE_ID,
    )

    memgraph.close()
    print("[seed] Project B done")


def test_semantic_search():
    ensure_seeded()
    print("[test] semantic_search_code...")
    from models.schema import SemanticSearchInput
    from tools.semantic_search import semantic_search_code

    inp = SemanticSearchInput(query="User struct with password hashing", project_scope="project_a")
    results = semantic_search_code(inp)

    assert len(results) > 0, "no search results"
    top = results[0]
    assert top.project == "project_a", f"expected project_a, got {top.project}"
    assert top.machine_id == MACHINE_ID, f"expected {MACHINE_ID}, got {top.machine_id}"
    assert top.file_path.endswith(("model.go", "handler.go")), f"unexpected file: {top.file_path}"
    assert "HashPassword" in top.snippet or "User struct" in top.snippet, f"expected symbol-level snippet, got: {top.snippet}"
    assert all(r.project == "project_a" for r in results[:3]), results[:3]
    print(f"[test] semantic_search OK: top={top.file_path} score={top.score:.3f}")


def test_dependency_graph():
    ensure_seeded()
    print("[test] get_dependency_graph...")
    from models.schema import DependencyGraphInput
    from tools.dependency_graph import get_dependency_graph

    inp = DependencyGraphInput(entity_name="GetUserHandler", direction="downstream", machine_id=MACHINE_ID)
    result = get_dependency_graph(inp)

    edges = result.edges
    assert len(edges) >= 1, f"expected edges, got {edges}"
    assert result.summary["entity"] == "GetUserHandler"
    assert result.summary["direction"] == "downstream"
    assert result.summary["edge_count"] == len(edges)
    assert any(node.get("name") == "GetUserHandler" for node in result.nodes), result.nodes
    assert result.groups["dependencies"], result.groups
    print(f"[test] dependency_graph OK: {len(edges)} downstream edges")


def test_read_file():
    ensure_seeded()
    print("[test] read_full_file...")
    from models.schema import ReadFileInput
    from tools.read_file import read_full_file

    path = os.path.join(FIXTURES, "project_a", "src", "model.go")
    inp = ReadFileInput(machine_id=MACHINE_ID, file_path=path)
    content = read_full_file(inp)

    assert "type User struct" in content, "User struct not found"
    print("[test] read_full_file OK")


def test_project_tree():
    ensure_seeded()
    print("[test] get_project_tree...")
    from models.schema import ProjectTreeInput
    from tools.project_tree import get_project_tree

    inp = ProjectTreeInput(machine_id=MACHINE_ID, project_name="project_a")
    tree = get_project_tree(inp)

    assert "model.go" in tree or "src/" in tree, f"tree missing files: {tree}"
    print("[test] get_project_tree OK")


def test_machine_scoped_search():
    ensure_seeded()
    print("[test] machine-scoped semantic search...")
    from models.schema import SemanticSearchInput
    from tools.semantic_search import semantic_search_code

    inp = SemanticSearchInput(query="User struct with password hashing", project_scope="project_a", machine_id=MACHINE_ID)
    results = semantic_search_code(inp)

    assert results, "no machine-scoped results"
    assert all(r.machine_id == MACHINE_ID for r in results), results
    assert all(r.project == "project_a" for r in results), results
    print("[test] machine-scoped semantic search OK")


def test_symbol_chunks_same_file_are_preserved():
    ensure_seeded()
    print("[test] same-file symbol chunks preserved...")
    from models.schema import SemanticSearchInput
    from tools.semantic_search import semantic_search_code

    inp = SemanticSearchInput(query="User password hash", project_scope="project_a", machine_id=MACHINE_ID)
    results = semantic_search_code(inp)
    snippets = "\n".join(r.snippet for r in results if r.file_path.endswith("model.go"))
    assert "type User struct" in snippets, snippets
    assert "HashPassword" in snippets, snippets
    print("[test] same-file symbol chunks preserved OK")


def test_cross_project():
    ensure_seeded()
    print("[test] cross-project search (User)...")
    from models.schema import SemanticSearchInput
    from tools.semantic_search import semantic_search_code

    inp = SemanticSearchInput(query="User model interface definition")
    results = semantic_search_code(inp)

    projects_found = {r.project for r in results}
    assert "project_a" in projects_found, f"projects: {projects_found}"
    assert "project_b" in projects_found, f"projects: {projects_found}"
    assert results[0].machine_id == MACHINE_ID, results[0]
    print(f"[test] cross-project OK: found in {projects_found}")


def test_tombstone():
    ensure_seeded()
    print("[test] tombstone deletion...")
    from db.qdrant_client import QdrantCodeStore
    from db.memgraph_client import MemgraphCodeGraph
    from db.content_store import ContentStore
    from models.schema import SemanticSearchInput
    from tools.semantic_search import semantic_search_code

    qdrant = QdrantCodeStore()
    memgraph = MemgraphCodeGraph()
    store = ContentStore()

    path = "/tests/fixtures/project_b/src/api.ts"
    qdrant.delete_by_file(path, MACHINE_ID)
    memgraph.delete_file(path, MACHINE_ID)
    store.delete_file(MACHINE_ID, path)

    # Search should no longer return api.ts
    inp = SemanticSearchInput(query="fetchUser API call", project_scope="project_b")
    results = semantic_search_code(inp)
    assert results, "expected remaining project_b results after tombstone"
    for r in results:
        assert r.project == "project_b", r
        assert r.machine_id == MACHINE_ID, r
        assert "api.ts" not in r.file_path, f"api.ts still returned after tombstone: {r.file_path}"

    memgraph.close()
    print("[test] tombstone OK")


def main():
    print("=== OmniGraph Phase 4 Integration Test ===\n")

    start_docker()
    seed_project_a()
    seed_project_b()

    test_semantic_search()
    test_dependency_graph()
    test_read_file()
    test_project_tree()
    test_cross_project()
    test_tombstone()

    print("\n=== All Phase 4 integration tests passed ===")


if __name__ == "__main__":
    main()
