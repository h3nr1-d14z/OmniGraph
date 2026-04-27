"""Phase 5B.3 token-overlap boost mechanics.

The boost runs AFTER RRF aggregation and only nudges scores; queries with
no >=3-char tokens (purely natural language) must produce identical
ranking to the no-boost path so existing eval baselines stay stable.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hub" / "mcp_server"))

from tools.semantic_search import (  # noqa: E402
    _entity_overlap,
    _merge_results,
    _path_overlap,
    _tokenize_query,
)


def _semantic_hit(file_path: str, entity: str, score: float = 0.9):
    return {
        "score": score,
        "payload": {
            "machine_id": "m1",
            "project": "p",
            "file_path": file_path,
            "entity": entity,
            "chunk_id": "0",
            "snippet": "",
        },
    }


def test_tokenize_query_drops_stopwords_and_short_tokens():
    assert _tokenize_query("how to hash a password") == {"hash", "password"}
    # OAuth keeps its full form because the acronym splitter only fires
    # when >=2 leading capitals precede the boundary — short two-letter
    # initialisms stay together as one token.
    assert _tokenize_query("what is the OAuth flow") == {"oauth", "flow"}


def test_tokenize_query_splits_camel_and_pascal_case():
    """Codex review (2026-04): code-search queries are usually identifier
    names, not natural language. ``HashPassword`` and ``getUserById``
    must split into the same tokens that _entity_overlap produces."""
    assert _tokenize_query("HashPassword") == {"hash", "password"}
    # `by` and `id` get filtered by the >=3 char rule, leaving only get/user.
    assert _tokenize_query("getUserById") == {"get", "user"}
    assert _tokenize_query("fetchUserProfile") == {"fetch", "user", "profile"}


def test_tokenize_query_returns_empty_for_pure_stopwords():
    # No identifier tokens — boost layer is a no-op for this query.
    assert _tokenize_query("what is it") == set()


def test_path_overlap_splits_path_components():
    assert _path_overlap("src/auth/handler.go", {"auth", "handler"}) == 2
    assert _path_overlap("src/auth/handler.go", {"missing"}) == 0
    # Short tokens (<3 chars) excluded by the regex filter.
    assert _path_overlap("src/auth/handler.go", {"go"}) == 0


def test_path_overlap_handles_camelcase_and_digits_in_filenames():
    """Codex review (2026-04): code paths often use CamelCase filenames
    (UserProfile.tsx, SHA256Hash.go). The path tokenizer must split the
    same way the query/entity tokenizers do so an obvious search like
    ``profile`` reaches the right file."""
    assert (
        _path_overlap(
            "/src/components/UserProfile.tsx", _tokenize_query("user profile")
        )
        == 2
    )
    assert _path_overlap("/lib/SHA256Hash.go", _tokenize_query("sha256 hash")) == 3


def test_path_overlap_drops_home_directory_components():
    """Absolute file paths on developer machines start with
    /Users/<name> (macOS) or /home/<name> (Linux). Those components are
    irrelevant to ranking and historically caused queries like ``user``
    to false-match every absolute path on macOS. Path overlap must not
    score on the home-directory legs.

    Includes the SHALLOW path case (``/Users/alice/myrepo/main.go``)
    where the 4-component window alone wouldn't filter the home prefix.
    Windows-style backslashes (``C:\\Users\\bob\\...``) and the bare
    drive-letter form are normalised to POSIX before stripping."""
    macos_deep = "/Users/alice/Projects/myrepo/src/handlers/login.go"
    macos_shallow = "/Users/alice/myrepo/main.go"
    linux_path = "/home/bob/code/auth/login.go"
    windows_path = r"C:\Users\charlie\repos\src\login.go"
    bare_drive = "D:/proj/login.go"

    for p in (macos_deep, macos_shallow, linux_path, windows_path, bare_drive):
        assert _path_overlap(p, _tokenize_query("user alice bob charlie")) == 0, (
            f"home prefix leaked tokens for {p}"
        )
    # The genuine signal still surfaces.
    assert _path_overlap(macos_deep, _tokenize_query("login handler")) == 2
    assert _path_overlap(linux_path, _tokenize_query("login auth")) == 2
    assert _path_overlap(windows_path, _tokenize_query("login")) == 1


def test_path_overlap_supports_prefix_match():
    """Codex review (2026-04): partial-name searches need prefix match.
    A query like ``auth`` should reach paths like
    ``/src/authentication/handler.go``; pure exact-set intersection misses
    that case and breaks the documented path-prefix boost."""
    assert _path_overlap("/src/authentication/handler.go", {"auth"}) == 1
    # Same query token also matches the longer 'auth' standalone.
    assert _path_overlap("/src/auth/x.go", {"auth"}) == 1
    # Partial-but-not-prefix MUST NOT match (would over-boost noise).
    assert _path_overlap("/src/lauth.go", {"auth"}) == 0
    # Multiple distinct query tokens count distinct (not duplicated).
    assert _path_overlap("/src/authentication/handler.go", {"auth", "handler"}) == 2


def test_entity_overlap_splits_camel_and_snake_case():
    assert _entity_overlap({"getUserById"}, {"user", "get"}) == 2
    assert _entity_overlap({"hash_password"}, {"hash", "password"}) == 2
    assert _entity_overlap({"GetUserById"}, {"id"}) == 0  # 'id' < 3 chars
    assert _entity_overlap({"foo"}, {"bar"}) == 0


def test_entity_overlap_splits_acronyms_and_digits():
    """Codex review (2026-04): code identifiers commonly use acronyms
    (HTMLParser, GetHTTPServer) and digit boundaries (SHA256Hash, Base64)
    that the camelCase-only splitter missed. Both the entity and query
    tokenizers must produce identical tokens for these forms or the
    entity boost silently goes to zero on common code-search queries."""
    # Acronym + word boundary: HTMLParser -> {html, parser}
    assert _entity_overlap({"HTMLParser"}, _tokenize_query("HTML parser")) == 2
    assert _entity_overlap({"GetHTTPServer"}, _tokenize_query("get HTTP server")) == 3
    # Digit boundary: SHA256Hash -> {sha, 256, hash}
    assert _entity_overlap({"SHA256Hash"}, _tokenize_query("sha256 hash")) == 3
    assert _entity_overlap({"Base64"}, _tokenize_query("base 64")) == 1  # 64 < 3 chars
    # Lower-then-digit: foo123 -> {foo, 123}
    assert (
        _entity_overlap({"version2024Patch"}, _tokenize_query("version 2024 patch"))
        == 3
    )


def test_no_query_tokens_preserves_baseline_ranking():
    """Pure NL queries (no identifier tokens) must produce identical output
    to the no-boost path — protects existing eval baselines."""
    semantic = [
        _semantic_hit("/src/foo.go", "FooHandler", 0.9),
        _semantic_hit("/src/bar.go", "BarHandler", 0.8),
    ]
    no_boost = _merge_results(semantic, [], None)
    empty_boost = _merge_results(semantic, [], set())
    assert [r.file_path for r in no_boost] == [r.file_path for r in empty_boost]
    assert [r.score for r in no_boost] == [r.score for r in empty_boost]


def test_path_match_lifts_lower_ranked_file_above_higher():
    """A semantically-weaker hit whose PATH matches the query should
    overtake a semantically-stronger hit with no path overlap when the
    boost is enough to close the rank-1 vs rank-2 gap."""
    # rank-1 has no overlap; rank-2 has 2 token overlaps in path.
    semantic = [
        _semantic_hit("/src/unrelated.go", "X", 0.99),
        _semantic_hit("/src/auth/handler.go", "X", 0.10),
    ]
    out = _merge_results(semantic, [], {"auth", "handler"})
    assert out[0].file_path == "/src/auth/handler.go", (
        "path-overlap row must rank first"
    )


def test_entity_match_lifts_lower_ranked_file():
    """Same as path test but boost driven by entity-name overlap."""
    semantic = [
        _semantic_hit("/src/a.go", "Unrelated", 0.99),
        _semantic_hit("/src/b.go", "hashPassword", 0.10),
    ]
    out = _merge_results(semantic, [], {"hash", "password"})
    assert out[0].file_path == "/src/b.go"


def test_boost_does_not_invent_results():
    """Boost may reorder, never introduce file_paths absent from the
    merged result set."""
    semantic = [_semantic_hit("/src/foo.go", "Foo", 0.9)]
    out = _merge_results(semantic, [], {"missing", "tokens"})
    assert {r.file_path for r in out} == {"/src/foo.go"}


def test_boost_is_bounded_by_query_size():
    """Codex review (2026-04): boost must NOT stack linearly with overlap
    count, otherwise a multi-token query lets a low-ranked file overtake
    the entire fused ranking purely on token coverage. The boost is
    normalized by query-token count so a perfect-overlap match earns the
    same magnitude regardless of how many tokens the user typed."""
    from tools.semantic_search import (  # noqa: E402
        RRF_ENTITY_BOOST,
        RRF_PATH_BOOST,
        _rrf_score,
    )

    boost_unit = _rrf_score(1)

    # Single-token query, perfect entity overlap, no path overlap.
    semantic = [_semantic_hit("/x.go", "auth", 0.0)]
    out = _merge_results(semantic, [], {"auth"})
    expected_max_single = RRF_ENTITY_BOOST * boost_unit  # 1/1 entity coverage
    base = 1.0 * _rrf_score(1)  # RRF_SEMANTIC_WEIGHT * rank-1
    assert abs(out[0].score - (base + expected_max_single)) < 1e-9, (
        f"single-token full-coverage = base + boost_unit; got {out[0].score}"
    )

    # Five-token query, perfect entity overlap on all 5: boost MUST be the
    # same magnitude as the single-token case, not 5× larger.
    semantic = [_semantic_hit("/x.go", "authHandlerLoginOauthSession", 0.0)]
    out = _merge_results(semantic, [], {"auth", "handler", "login", "oauth", "session"})
    assert abs(out[0].score - (base + expected_max_single)) < 1e-9, (
        f"five-token full-coverage must equal one-token full-coverage; got {out[0].score}"
    )


def test_boost_is_disabled_when_constants_zero(monkeypatch):
    """Setting RRF_PATH_BOOST=0 + RRF_ENTITY_BOOST=0 must restore the
    pre-5B.3 ranking behaviour. The reload-then-reload-back pattern is
    required because monkeypatch reverts env vars but module-level
    constants captured those env values at import time."""
    import importlib

    import tools.semantic_search as ss

    monkeypatch.setenv("RRF_PATH_BOOST", "0")
    monkeypatch.setenv("RRF_ENTITY_BOOST", "0")
    importlib.reload(ss)
    # Pair the env-mutation reload with a teardown reload so the next test
    # in the same process picks up the original (env-restored) constants.
    monkeypatch.undo()  # restore env immediately
    try:
        semantic = [
            _semantic_hit("/src/auth.go", "Auth", 0.99),
            _semantic_hit("/src/other.go", "X", 0.10),
        ]
        out = ss._merge_results(semantic, [], {"auth"})
        # First file is rank-1 in semantic; without boost it stays first.
        assert out[0].file_path == "/src/auth.go"
    finally:
        importlib.reload(ss)
