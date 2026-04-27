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
# RRF constant: smaller k = sharper rank-1 dominance. k=20 calibrated for
# short prefetch lists (HYBRID_LIMIT=10). k=60 (Cormack default) is too
# flat for n=10 — top vs bottom RRF spread <14%.
RRF_K = int(os.getenv("RRF_K", "20"))
# Asymmetric ranker weights. Semantic intent dominates code RAG; lexical
# is precision-boost for identifier-heavy queries.
RRF_SEMANTIC_WEIGHT = float(os.getenv("RRF_SEMANTIC_WEIGHT", "1.0"))
RRF_LEXICAL_WEIGHT = float(os.getenv("RRF_LEXICAL_WEIGHT", "0.7"))

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


def _merge_results(
    semantic_results: list[dict],
    lexical_results: list[dict[str, str | float]],
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
                entry["snippet"] = snippet[:500]
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
                entry["snippet"] = snippet[:500]

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

    return _merge_results(semantic_results, lexical_results)
