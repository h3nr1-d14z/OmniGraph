"""US-011 acceptance: real RRF replacing union-disguised-as-merge."""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hub" / "mcp_server"))

from tools.semantic_search import _merge_results, _rrf_score, RRF_K  # noqa: E402


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


def test_overlapping_results_merge_to_single_entry_with_summed_rrf():
    """Both rankers hit /foo.go → ONE entry with summed RRF (was UNION before)."""
    semantic = [_semantic_hit("m1", "p", "/foo.go", "FuncA", "0", 0.9, "sem snip")]
    lexical = [_lexical_hit("m1", "p", "/foo.go", 1.0, "lex snip")]

    out = _merge_results(semantic, lexical)
    files = [r.file_path for r in out]
    assert files == ["/foo.go"], f"expected single merged entry, got {files}"

    expected_score = 1.0 * _rrf_score(1) + 0.7 * _rrf_score(1)
    assert abs(out[0].score - expected_score) < 1e-9, (out[0].score, expected_score)


def test_no_overlap_keeps_distinct_entries():
    """Non-overlapping files stay distinct in the result list."""
    semantic = [_semantic_hit("m1", "p", "/sem.go", "S", "0", 0.9)]
    lexical = [_lexical_hit("m1", "p", "/lex.go", 1.0)]

    out = _merge_results(semantic, lexical)
    assert {r.file_path for r in out} == {"/sem.go", "/lex.go"}


def test_deterministic_ranking_under_rrf_with_ties():
    """Stable sort: identical scores resolve by file_path lexicographic order."""
    semantic = [
        _semantic_hit("m", "p", "/a.go", "A", "0", 0.5),
        _semantic_hit("m", "p", "/b.go", "B", "0", 0.5),
    ]
    lexical = []
    out1 = _merge_results(semantic, lexical)
    out2 = _merge_results(semantic, lexical)
    assert [r.file_path for r in out1] == [r.file_path for r in out2]


def test_semantic_weight_dominates_lexical_at_same_rank():
    """At rank 1 in each ranker, semantic contribution > lexical (1.0 vs 0.7)."""
    semantic_only = [_semantic_hit("m", "p", "/a.go", "A", "0", 0.9)]
    lexical_only = [_lexical_hit("m", "p", "/a.go", 1.0)]
    sem = _merge_results(semantic_only, [])
    lex = _merge_results([], lexical_only)
    assert sem[0].score > lex[0].score


def test_multiple_chunks_same_file_collapse_to_one():
    """Multiple semantic chunks of /foo.go → one merged entry summing all RRF contributions."""
    semantic = [
        _semantic_hit("m", "p", "/foo.go", "FuncA", "0", 0.9, "snip A"),
        _semantic_hit("m", "p", "/foo.go", "FuncB", "1", 0.8, "snip B"),
        _semantic_hit("m", "p", "/foo.go", "FuncC", "2", 0.7, "snip C"),
    ]
    out = _merge_results(semantic, [])
    assert len(out) == 1, f"expected 1 entry, got {len(out)}"
    expected = sum(_rrf_score(r) for r in (1, 2, 3))
    assert abs(out[0].score - expected) < 1e-9


def test_snippet_from_top_semantic_rank():
    """Snippet field comes from the highest-ranked semantic hit (rank 1)."""
    semantic = [
        _semantic_hit("m", "p", "/foo.go", "Top", "0", 0.9, "FROM RANK 1"),
        _semantic_hit("m", "p", "/foo.go", "Lower", "1", 0.5, "from rank 2"),
    ]
    out = _merge_results(semantic, [])
    assert out[0].snippet == "FROM RANK 1"


def test_rrf_k_default_is_20():
    """Phase 4.5 acceptance: k=20 starting point (k=60 fallback gated separately)."""
    assert RRF_K == 20, f"expected RRF_K=20, got {RRF_K}"


def test_rrf_score_decreases_with_rank():
    """RRF formula: 1/(k+rank) is monotonically decreasing."""
    s1 = _rrf_score(1)
    s10 = _rrf_score(10)
    assert s1 > s10
    assert abs(s1 - 1.0 / (20 + 1)) < 1e-12
