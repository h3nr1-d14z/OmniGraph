"""Tool 1: semantic_search_code — Query Qdrant for code snippets."""

import hashlib
import os

import httpx

from models.schema import SemanticSearchInput, SemanticSearchResult

EMBED_URL = os.getenv("EMBED_SERVICE_URL", "http://localhost:8000")
EMBED_MODE = "query"
HYBRID_LIMIT = 10

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


def _merge_results(
    semantic_results: list[dict],
    lexical_results: list[dict[str, str | float]],
) -> list[SemanticSearchResult]:
    merged: dict[tuple[str, str, str, str, str], SemanticSearchResult] = {}

    for rank, result in enumerate(semantic_results):
        payload = result["payload"]
        key = (
            payload.get("machine_id", ""),
            payload.get("project", ""),
            payload.get("file_path", ""),
            payload.get("chunk_id", ""),
            payload.get("entity", ""),
        )
        merged[key] = SemanticSearchResult(
            file_path=payload.get("file_path", ""),
            machine_id=payload.get("machine_id", ""),
            project=payload.get("project", ""),
            snippet=payload.get("snippet", "")[:500],
            score=float(result["score"]) + max(0.0, 0.2 - rank * 0.01),
        )

    for lexical in lexical_results:
        key = (
            str(lexical["machine_id"]),
            str(lexical["project"]),
            str(lexical["file_path"]),
            "",
            "lexical",
        )
        lexical_boost = float(lexical["score"]) * 0.1
        if key in merged:
            merged[key].score += lexical_boost
            if not merged[key].snippet:
                merged[key].snippet = str(lexical["snippet"])[:500]
            continue
        merged[key] = SemanticSearchResult(
            file_path=str(lexical["file_path"]),
            machine_id=str(lexical["machine_id"]),
            project=str(lexical["project"]),
            snippet=str(lexical["snippet"])[:500],
            score=lexical_boost,
        )

    return sorted(merged.values(), key=lambda item: item.score, reverse=True)[:HYBRID_LIMIT]


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
