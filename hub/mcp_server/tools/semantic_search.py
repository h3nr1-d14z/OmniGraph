"""Tool 1: semantic_search_code — Query Qdrant for code snippets.

Hybrid ranking uses Reciprocal Rank Fusion (RRF). RRF is rank-based, so it
sidesteps the score-scale mismatch between cosine similarity (Qdrant) and
BM25 rank (FTS5) that breaks naive additive merges.

Reference: Cormack, Clarke, Buettcher 2009 — outperforms condorcet and
score-based fusion for short candidate lists (n=10-20).
"""

import hashlib
import os

import httpx
from models.schema import SemanticSearchInput, SemanticSearchResult

EMBED_URL = os.getenv("EMBED_SERVICE_URL", "http://localhost:8000")
EMBED_MODE = "query"
HYBRID_LIMIT = 10
SNIPPET_MAX_CHARS = int(os.getenv("MCP_SNIPPET_MAX_CHARS", "250"))
# RRF constant: smaller k = sharper rank-1 dominance. k=20 calibrated for
# short prefetch lists (HYBRID_LIMIT=10). k=60 (Cormack default) is too
# flat for n=10 — top vs bottom RRF spread <14%.
RRF_K = int(os.getenv("RRF_K", "20"))
# Asymmetric ranker weights. Semantic intent dominates code RAG; lexical
# is precision-boost for identifier-heavy queries.
RRF_SEMANTIC_WEIGHT = float(os.getenv("RRF_SEMANTIC_WEIGHT", "1.0"))
RRF_LEXICAL_WEIGHT = float(os.getenv("RRF_LEXICAL_WEIGHT", "0.7"))
# Phase 5B.3 token-overlap boosts. Applied AFTER RRF aggregation so the
# rank-based fusion semantics are preserved; values are scaled to 1/(k+1)
# (the maximum single-rank RRF contribution) so a full-overlap match adds
# the equivalent of one rank-1 hit. Set to 0 to disable.
RRF_PATH_BOOST = float(os.getenv("RRF_PATH_BOOST", "0.30"))
RRF_ENTITY_BOOST = float(os.getenv("RRF_ENTITY_BOOST", "0.50"))

# Stop-words excluded from token-overlap boosts. Identifier-style queries
# rarely use these and including them would over-reward generic matches.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
    }
)

# Module-level reusable HTTP client for connection pooling
_http_client: httpx.Client | None = None


def _get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(timeout=30.0)
    return _http_client


def _embedding_cache_key(text: str) -> str:
    backend = os.getenv("EMBED_BACKEND", "auto")
    model = os.getenv("EMBED_MODEL_NAME", "nomic-ai/nomic-embed-text-v1.5")
    raw = f"{backend}:{model}:{EMBED_MODE}:{text.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _embed_query(text: str) -> list[float]:
    """Call the local Hub embed service (singleton process holds the model)."""
    from db.content_store import get_store

    store = get_store()
    cache_key = _embedding_cache_key(text)
    cached = store.get_query_embedding(cache_key)
    if cached is not None:
        return cached

    client = _get_http_client()
    r = client.post(
        f"{EMBED_URL}/embed",
        json={"texts": [text], "mode": EMBED_MODE},
    )
    r.raise_for_status()
    embedding = r.json()["embeddings"][0]
    store.upsert_query_embedding(cache_key, embedding)
    return embedding


def _rrf_score(rank: int, k: int = RRF_K) -> float:
    """Reciprocal Rank Fusion score for a 1-indexed rank position."""
    return 1.0 / (k + rank)


# Identifier-split boundaries used by both the query tokenizer and the
# entity tokenizer so a query like "HTML parser" matches an entity named
# `HTMLParser`. Covers four cases:
#   1. lowercase -> Uppercase   (camelCase: aB -> a|B)
#   2. UPPER -> Upper-then-lower (acronym + word: HTMLP -> HTML|P)
#   3. letter -> digit          (Foo123 -> Foo|123)
#   4. digit -> letter          (123Foo -> 123|Foo)
import re as _re

_IDENT_SPLIT = _re.compile(
    r"(?<=[a-z])(?=[A-Z])"
    # Require >=2 leading capitals before the acronym boundary, so a
    # genuine acronym like ``HTTPServer`` splits into ``HTTP|Server`` but
    # short two-letter forms like ``OAuth`` (which are conventionally one
    # token) stay intact.
    r"|(?<=[A-Z][A-Z])(?=[A-Z][a-z])"
    r"|(?<=[a-zA-Z])(?=\d)"
    r"|(?<=\d)(?=[a-zA-Z])"
)
_TOKEN_SPLIT = _re.compile(r"[a-z0-9]+")


def _split_identifier_tokens(text: str) -> set[str]:
    """Lower-case alphanumeric tokens with camelCase/acronym/digit
    boundaries respected. Returns tokens >= 3 chars only — short
    fragments produce noise in overlap matching (e.g. `id`, `go`)."""
    boundary = _IDENT_SPLIT.sub(" ", text)
    return {t for t in _TOKEN_SPLIT.findall(boundary.lower()) if len(t) >= 3}


def _tokenize_query(text: str) -> set[str]:
    """Boost-match tokens for the user query. Splits identifier-style
    inputs (``HashPassword``, ``HTMLParser``, ``SHA256Hash``) into the
    same tokens that ``_entity_overlap`` produces, then drops stop-words."""
    return _split_identifier_tokens(text) - _STOPWORDS


_HOME_PREFIX = _re.compile(
    r"^(?:[A-Za-z]:)?/(?:Users|home)/[^/]+/"  # /Users/<name>/, /home/<name>/, C:/Users/<name>/
    r"|^/root/"  # root user home
    r"|^[A-Za-z]:/"  # bare Windows drive letter
)


def _strip_home_prefix(file_path: str) -> str:
    """Normalise OS-specific home-directory prefixes out of an absolute
    path so the path-overlap boost can never score on a developer's
    user-name. Returns the path with the prefix replaced by "/" so the
    remainder still tokenises cleanly."""
    posix = file_path.replace("\\", "/")
    return _HOME_PREFIX.sub("/", posix, count=1)


def _path_overlap(file_path: str, query_tokens: set[str]) -> int:
    """Count of query tokens that match a path component, exact OR prefix.

    Path tokenization mirrors query/entity tokenization (camelCase,
    acronym, digit boundaries) so a path like ``UserProfile.tsx`` yields
    ``{user, profile, tsx}`` and matches the obvious queries. Home
    directory prefixes (``/Users/<name>/``, ``/home/<name>/``, Windows
    drive letters) are stripped first so user-names never leak into the
    boost. After stripping we keep at most the last four components
    (project / package / module / filename) to bound noise from deeply
    nested mono-repo paths."""
    if not file_path or not query_tokens:
        return 0
    from pathlib import PurePosixPath

    normalised = _strip_home_prefix(file_path)
    parts = PurePosixPath(normalised).parts
    components = [p for p in parts if p not in ("/", "")][-4:]
    path_tokens: set[str] = set()
    for c in components:
        path_tokens.update(_split_identifier_tokens(c))
    if not path_tokens:
        return 0
    return sum(
        1 for q in query_tokens if any(p == q or p.startswith(q) for p in path_tokens)
    )


def _entity_overlap(entities: set[str], query_tokens: set[str]) -> int:
    """Count of distinct query tokens covered by any entity name. Each
    entity is fed through the same identifier splitter as the query
    (camelCase + acronym + digit boundaries) so ``HTMLParser`` and a
    ``"HTML parser"`` query both yield ``{html, parser}``. Counts
    distinct overlapping tokens, not entity occurrences, so the boost
    ceiling stays predictable."""
    if not entities or not query_tokens:
        return 0
    entity_tokens: set[str] = set()
    for ent in entities:
        entity_tokens.update(_split_identifier_tokens(ent))
    return len(query_tokens & entity_tokens)


def _merge_results(
    semantic_results: list[dict],
    lexical_results: list[dict[str, str | float]],
    query_tokens: set[str] | None = None,
) -> list[SemanticSearchResult]:
    """File-level RRF merge of semantic and lexical results.

    Merge key is ``(machine_id, project, file_path)`` — collapses multiple
    chunks of the same file into one ranked entry whose score sums RRF
    contributions from both rankers.
    """
    by_file: dict[tuple[str, str, str], dict] = {}

    for rank, result in enumerate(semantic_results, start=1):
        payload = result["payload"]
        key = (
            payload.get("machine_id", ""),
            payload.get("project", ""),
            payload.get("file_path", ""),
        )
        rrf = RRF_SEMANTIC_WEIGHT * _rrf_score(rank)
        entry = by_file.setdefault(
            key,
            {
                "machine_id": key[0],
                "project": key[1],
                "file_path": key[2],
                "snippet": "",
                "score": 0.0,
                "best_semantic_rank": 9999,
                "entities": set(),
            },
        )
        entry["score"] += rrf
        # Take snippet from the highest-ranked semantic chunk (best summary).
        if rank < entry["best_semantic_rank"]:
            entry["best_semantic_rank"] = rank
            snippet = payload.get("snippet", "") or ""
            if snippet:
                entry["snippet"] = snippet[:SNIPPET_MAX_CHARS]
        entity = payload.get("entity", "")
        if entity:
            entry["entities"].add(entity)

    for rank, lexical in enumerate(lexical_results, start=1):
        key = (
            str(lexical.get("machine_id", "")),
            str(lexical.get("project", "")),
            str(lexical.get("file_path", "")),
        )
        rrf = RRF_LEXICAL_WEIGHT * _rrf_score(rank)
        entry = by_file.setdefault(
            key,
            {
                "machine_id": key[0],
                "project": key[1],
                "file_path": key[2],
                "snippet": "",
                "score": 0.0,
                "best_semantic_rank": 9999,
                "entities": set(),
            },
        )
        entry["score"] += rrf
        if not entry["snippet"]:
            snippet = str(lexical.get("snippet", "")) or ""
            if snippet:
                entry["snippet"] = snippet[:SNIPPET_MAX_CHARS]

    # Phase 5B.3 token-overlap boosts. Applied per file so they shift
    # ranking only for queries with identifier overlap; pure NL queries
    # (no tokens >= 3 chars) keep the original RRF ordering.
    #
    # Boost is bounded: each category contributes at most one rank-1 RRF
    # unit per file regardless of how many tokens overlap. Stacking the
    # full unit per hit would let a 4-word query dominate the fused
    # ranking purely on overlap (Codex review 2026-04). To still reward
    # higher-overlap matches, the contribution scales with the SHARE of
    # query tokens that hit (hits / |query_tokens|) so partial-coverage
    # matches earn proportionally less than full coverage.
    if query_tokens:
        boost_unit = _rrf_score(1)  # rank-1 RRF magnitude
        n_query = len(query_tokens)
        for entry in by_file.values():
            path_hits = _path_overlap(entry["file_path"], query_tokens)
            entity_hits = _entity_overlap(entry["entities"], query_tokens)
            entry["score"] += RRF_PATH_BOOST * boost_unit * (path_hits / n_query)
            entry["score"] += RRF_ENTITY_BOOST * boost_unit * (entity_hits / n_query)

    out = [
        SemanticSearchResult(
            file_path=e["file_path"],
            machine_id=e["machine_id"],
            project=e["project"],
            snippet=e["snippet"],
            score=e["score"],
            matched_entities=sorted(e["entities"]),
        )
        for e in by_file.values()
    ]
    # Stable secondary sort by file_path for deterministic ranking under
    # tied RRF scores (acceptance: deterministic snapshot test).
    out.sort(key=lambda r: r.file_path)
    out.sort(key=lambda r: r.score, reverse=True)
    return out[:HYBRID_LIMIT]


def semantic_search_code(input_data: SemanticSearchInput) -> list[SemanticSearchResult]:
    """Find code snippets/files by semantic meaning."""
    from db.content_store import get_store
    from db.qdrant_client import get_qdrant

    query_vector = _embed_query(input_data.query)

    store = get_qdrant()
    semantic_results = store.search(
        query_vector=query_vector,
        project_scope=input_data.project_scope,
        machine_id=input_data.machine_id,
        limit=HYBRID_LIMIT,
    )

    content_store = get_store()
    lexical_results = content_store.search_files_exact(
        query=input_data.query,
        machine_id=input_data.machine_id,
        project_scope=input_data.project_scope,
        limit=HYBRID_LIMIT,
    )

    return _merge_results(
        semantic_results, lexical_results, _tokenize_query(input_data.query)
    )
