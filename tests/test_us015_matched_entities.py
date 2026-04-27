"""US-015 acceptance: matched_entities aggregation in SemanticSearchResult."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hub" / "mcp_server"))

from models.schema import SemanticSearchResult  # noqa: E402
from tools.semantic_search import _merge_results  # noqa: E402


def _semantic_hit(machine_id, project, file_path, entity, chunk_id, score, snippet=""):
    """Build a Qdrant-shaped semantic result dict."""
    return {
        "score": score,
        "payload": {
            "machine_id": machine_id,
            "project": project,
            "file_path": file_path,
            "entity": entity,
            "chunk_id": chunk_id,
            "snippet": snippet,
        },
    }


def _lexical_hit(machine_id, project, file_path, score, snippet=""):
    return {
        "machine_id": machine_id,
        "project": project,
        "file_path": file_path,
        "score": score,
        "snippet": snippet,
    }


def test_matched_entities_aggregates_across_chunks():
    """3 semantic chunks for same file with entities User/HashPassword/User
    → matched_entities is deduped and sorted."""
    semantic = [
        _semantic_hit("m", "p", "/model.go", "User", "0", 0.9, "snip A"),
        _semantic_hit("m", "p", "/model.go", "HashPassword", "1", 0.8, "snip B"),
        _semantic_hit("m", "p", "/model.go", "User", "2", 0.7, "snip C"),
    ]
    out = _merge_results(semantic, [])
    assert len(out) == 1
    assert out[0].matched_entities == ["HashPassword", "User"]


def test_matched_entities_excludes_empty_entity_strings():
    """Chunks with entity='' don't pollute matched_entities."""
    semantic = [
        _semantic_hit("m", "p", "/foo.go", "", "0", 0.9, "snip"),
        _semantic_hit("m", "p", "/foo.go", "ValidFunc", "1", 0.8, "snip2"),
        _semantic_hit("m", "p", "/foo.go", "", "2", 0.7, "snip3"),
    ]
    out = _merge_results(semantic, [])
    assert out[0].matched_entities == ["ValidFunc"]


def test_matched_entities_excludes_lexical_only_hits():
    """File matched only via lexical (no semantic chunks) → matched_entities == []."""
    lexical = [_lexical_hit("m", "p", "/bar.go", 1.0, "lex snip")]
    out = _merge_results([], lexical)
    assert len(out) == 1
    assert out[0].matched_entities == []


def test_matched_entities_default_empty_for_legacy_consumers():
    """Instantiate SemanticSearchResult without matched_entities → defaults to []."""
    result = SemanticSearchResult(
        file_path="/some/file.go",
        machine_id="m1",
        project="proj",
        snippet="some code",
        score=0.5,
    )
    assert result.matched_entities == []
    dumped = result.model_dump()
    assert "matched_entities" in dumped
    assert dumped["matched_entities"] == []


def test_full_search_pipeline_returns_matched_entities():
    """Integration: multi-chunk semantic results produce non-empty matched_entities."""
    semantic = [
        _semantic_hit("machine1", "myproject", "/src/auth.go", "Login", "0", 0.95, "func Login"),
        _semantic_hit("machine1", "myproject", "/src/auth.go", "Logout", "1", 0.85, "func Logout"),
        _semantic_hit("machine1", "myproject", "/src/db.go", "Connect", "2", 0.75, "func Connect"),
    ]
    lexical = [
        _lexical_hit("machine1", "myproject", "/src/auth.go", 1.0, "auth snippet"),
    ]
    out = _merge_results(semantic, lexical)

    auth_results = [r for r in out if r.file_path == "/src/auth.go"]
    assert len(auth_results) == 1
    auth = auth_results[0]
    assert auth.matched_entities == ["Login", "Logout"]

    db_results = [r for r in out if r.file_path == "/src/db.go"]
    assert len(db_results) == 1
    assert db_results[0].matched_entities == ["Connect"]

    # At least one result has non-empty matched_entities
    assert any(r.matched_entities for r in out)
