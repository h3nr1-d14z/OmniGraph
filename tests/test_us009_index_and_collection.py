"""US-009 acceptance: Memgraph composite index + Qdrant collection-per-model."""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hub" / "mcp_server"))

from db.qdrant_client import QdrantCodeStore  # noqa: E402


def test_memgraph_composite_index_in_schema():
    """Composite index on (machine_id, project, file_path, name) must be
    created during _ensure_schema for fast multi-tenant MERGE."""
    src = (REPO / "hub" / "mcp_server" / "db" / "memgraph_client.py").read_text()
    pattern = re.compile(
        r'CREATE INDEX ON :Entity\(\s*machine_id\s*,\s*project\s*,\s*file_path\s*,\s*name\s*\)'
    )
    assert pattern.search(src), "composite Entity index missing in _ensure_schema"


def test_qdrant_collection_env_driven():
    """QdrantCodeStore must read collection from QDRANT_COLLECTION env."""
    src = (REPO / "hub" / "mcp_server" / "db" / "qdrant_client.py").read_text()
    assert 'os.getenv("QDRANT_COLLECTION"' in src, "qdrant_client must read QDRANT_COLLECTION env"


def test_env_example_documents_collection_per_model():
    """`.env.example` documents code_v1_nomic / code_v2_jina rollback model."""
    env = (REPO / ".env.example").read_text()
    assert "code_v1_nomic" in env, ".env.example missing collection-per-model note"
    assert "code_v2_jina" in env, ".env.example missing post-cutover collection name"


def test_docker_compose_passes_qdrant_collection():
    """docker-compose.yml must pass QDRANT_COLLECTION env to hub-api so cutover
    can flip via .env without editing the compose file."""
    yml = (REPO / "docker-compose.yml").read_text()
    assert "QDRANT_COLLECTION=${QDRANT_COLLECTION" in yml, "hub-api missing QDRANT_COLLECTION env wiring"


def test_qdrant_search_accepts_exact_param():
    """US-006/009: search() supports exact kwarg for deterministic baseline harness."""
    import inspect
    sig = inspect.signature(QdrantCodeStore.search)
    assert "exact" in sig.parameters, "QdrantCodeStore.search missing 'exact' param"
    assert sig.parameters["exact"].default is False, "exact must default to False"
