"""Hub API Server — receives batches from Watchers and indexes into Qdrant + Memgraph."""

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

# Import from sibling package
import sys

sys.path.insert(1, os.path.join(os.path.dirname(__file__), "..", "mcp_server"))

from db.content_store import ContentStore, get_store
from db.memgraph_client import MemgraphCodeGraph, get_memgraph
from db.qdrant_client import QdrantCodeStore, get_qdrant
from models.event import FileEvent  # type: ignore

EMBED_URL = os.getenv("EMBED_SERVICE_URL", "http://localhost:8000")
AUTH_TOKEN = os.getenv("HUB_AUTH_TOKEN", "changeme")
# Per-entity cap aligned to embedding model context (~8192 tokens × ~4 chars/token).
# Whole-file path uses sliding window instead of a fixed cap.
MAX_EMBED_CHARS = int(os.getenv("EMBED_MAX_CHARS", "32000"))
MAX_SNIPPET_CHARS = int(os.getenv("SNIPPET_MAX_CHARS", "500"))
# Sliding window for whole-file fallback (when no entities extracted).
SLIDING_WINDOW_CHARS = int(os.getenv("SLIDING_WINDOW_CHARS", "2400"))
SLIDING_STRIDE_CHARS = int(os.getenv("SLIDING_STRIDE_CHARS", "600"))
# Hard caps to bound embed-service load on pathological inputs.
MAX_CHUNKS_PER_FILE = int(os.getenv("MAX_CHUNKS_PER_FILE", "64"))
MAX_CHUNKS_PER_REQUEST = int(os.getenv("MAX_CHUNKS_PER_REQUEST", "32"))


class BatchIn(BaseModel):
    machine_id: str
    project: str
    events: list[dict]
    sent_at: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.qdrant = get_qdrant()
    app.state.memgraph = get_memgraph()
    app.state.content = get_store()
    app.state.http = httpx.AsyncClient(timeout=30.0)
    # US-013: bulk preload content hashes so a watcher restart's catch-up scan
    # short-circuits identical-hash events instead of re-embedding the corpus.
    app.state.content_hashes = app.state.content.get_content_hashes()
    yield
    await app.state.http.aclose()
    app.state.memgraph.close()


app = FastAPI(title="OmniGraph Hub API", lifespan=lifespan)


def _verify_auth(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth[7:]
    if token != AUTH_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


@app.post("/batch")
async def receive_batch(batch: BatchIn, request: Request):
    """Receive a batch of file events from a Watcher node."""
    _verify_auth(request)

    qdrant: QdrantCodeStore = request.app.state.qdrant
    memgraph: MemgraphCodeGraph = request.app.state.memgraph
    content: ContentStore = request.app.state.content

    events = [FileEvent(**ev_raw) for ev_raw in batch.events]
    deletes = [ev for ev in events if ev.type in ("DELETE", "RENAME")]
    upserts = [ev for ev in events if ev.type not in ("DELETE", "RENAME")]

    for ev in deletes:
        _handle_delete(ev, qdrant, memgraph, content, batch.machine_id, batch.project)
        request.app.state.content_hashes.pop((ev.machine_id or batch.machine_id, ev.path), None)
        if ev.old_path and ev.old_path != ev.path:
            request.app.state.content_hashes.pop((ev.machine_id or batch.machine_id, ev.old_path), None)
    await _handle_upserts(
        upserts,
        batch.machine_id,
        batch.project,
        qdrant,
        memgraph,
        content,
        request.app.state.http,
        request.app.state.content_hashes,
    )

    return {"status": "ok", "processed": len(batch.events)}


def _slice_symbol_chunk(lines: list[str], start_line: int | None, end_line: int | None) -> str:
    if not lines or not start_line or not end_line or start_line <= 0 or end_line < start_line:
        return ""
    if start_line > len(lines):
        return ""
    start_idx = start_line - 1
    end_idx = min(end_line, len(lines))
    return "\n".join(lines[start_idx:end_idx]).strip()


def _sliding_windows(content: str, window: int, stride: int) -> list[tuple[int, int, str]]:
    """Return list of (start_char, end_char, text) windows. Stride < window
    creates overlap. Empty content yields no windows."""
    if not content or window <= 0 or stride <= 0:
        return []
    out: list[tuple[int, int, str]] = []
    pos = 0
    n = len(content)
    while pos < n:
        end = min(pos + window, n)
        out.append((pos, end, content[pos:end]))
        if end >= n:
            break
        pos += stride
    return out


def _build_embedding_chunks(ev: FileEvent, machine_id: str, project: str) -> list[dict[str, str | int]]:
    if not ev.content:
        return []

    chunks: list[dict[str, str | int]] = []
    seen_ranges: set[tuple[str, int, int]] = set()
    lines = ev.content.splitlines()

    for idx, ent in enumerate(ev.entities or []):
        chunk_text = _slice_symbol_chunk(lines, ent.start_line, ent.end_line)
        if not chunk_text:
            continue
        range_key = (ent.name, ent.start_line or 0, ent.end_line or 0)
        if range_key in seen_ranges:
            continue
        seen_ranges.add(range_key)
        chunks.append(
            {
                "machine_id": machine_id,
                "project": project,
                "file_path": ev.path,
                "entity": ent.name,
                "entity_type": ent.type,
                "chunk_id": str(idx),
                "start_line": ent.start_line or 0,
                "end_line": ent.end_line or 0,
                "text": chunk_text[:MAX_EMBED_CHARS],
                "snippet": chunk_text[:MAX_SNIPPET_CHARS],
            }
        )

    if chunks:
        return _cap_chunks_per_file(chunks, ev.path)

    # Whole-file fallback: sliding window so the file tail isn't silently
    # skipped (a single capped chunk would truncate at MAX_EMBED_CHARS).
    windows = _sliding_windows(ev.content, SLIDING_WINDOW_CHARS, SLIDING_STRIDE_CHARS)
    fallback_entity = ev.entities[0].name if ev.entities else ""
    fallback_type = ev.entities[0].type if ev.entities else "file"
    for idx, (start_ch, end_ch, text) in enumerate(windows):
        # Approximate line numbers from char offsets (cheap heuristic; precise
        # mapping isn't needed for retrieval — only for snippet display).
        start_line = ev.content.count("\n", 0, start_ch) + 1
        end_line = ev.content.count("\n", 0, end_ch) + 1
        chunks.append(
            {
                "machine_id": machine_id,
                "project": project,
                "file_path": ev.path,
                "entity": fallback_entity,
                "entity_type": fallback_type,
                "chunk_id": f"win{idx}",
                "start_line": start_line,
                "end_line": end_line,
                "text": text,
                "snippet": text[:MAX_SNIPPET_CHARS],
            }
        )

    return _cap_chunks_per_file(chunks, ev.path)


def _cap_chunks_per_file(chunks: list[dict[str, str | int]], path: str) -> list[dict[str, str | int]]:
    if len(chunks) <= MAX_CHUNKS_PER_FILE:
        return chunks
    print(f"[hub] {path}: produced {len(chunks)} chunks, capped to {MAX_CHUNKS_PER_FILE} (rest skipped)")
    return chunks[:MAX_CHUNKS_PER_FILE]


async def _handle_upserts(
    events: list[FileEvent],
    machine_id: str,
    project: str,
    qdrant: QdrantCodeStore,
    memgraph: MemgraphCodeGraph,
    content: ContentStore,
    http: httpx.AsyncClient,
    content_hashes: dict[tuple[str, str], str] | None = None,
) -> None:
    # US-013: content-hash short-circuit cache. None → behave as if all events
    # are first-touch (used by direct unit-test callers). The /batch route
    # always passes the live app.state cache.
    cache: dict[tuple[str, str], str] = content_hashes if content_hashes is not None else {}

    files: list[tuple[str, str, str, str]] = []
    file_hashes: dict[tuple[str, str], str] = {}
    entities: list[dict] = []
    relations: list[dict] = []
    chunks: list[dict[str, str | int]] = []
    # Files that should skip the qdrant delete + re-embed because their
    # content_hash matches what we already indexed. Graph upserts still run
    # (relations may carry new layer/status info; MERGE is idempotent).
    embed_skipped: set[str] = set()
    # Files whose new content_hash should be persisted post-Qdrant-upsert.
    pending_hash_writes: list[tuple[str, str, str]] = []

    for ev in events:
        ev_machine = ev.machine_id or machine_id
        key = (ev_machine, ev.path)
        existing = cache.get(key)
        ev_hash = ev.content_hash or None
        hash_match = ev_hash is not None and existing == ev_hash

        if ev.content:
            files.append((machine_id, project, ev.path, ev.content))
            if ev_hash:
                file_hashes[(machine_id, ev.path)] = ev_hash
        if ev.entities:
            entities.extend(
                {
                    "machine_id": machine_id,
                    "project": project,
                    "file_path": ev.path,
                    "name": ent.name,
                    "type": ent.type,
                    "content_hash": ev.content_hash or "",
                    "start_line": ent.start_line or 0,
                    "end_line": ent.end_line or 0,
                }
                for ent in ev.entities
            )
        if ev.relations:
            relations.extend(
                {
                    "machine_id": machine_id,
                    "project": project,
                    "file_path": ev.path,
                    "type": rel.type,
                    "source": rel.source or "",
                    "target": rel.target,
                    "target_type": rel.target_type or "",
                    "line": rel.line or 0,
                    "confidence": rel.confidence or "syntax",
                    "layer": rel.layer or "syntax",
                    "status": rel.status or "observed",
                    "symbol_id": rel.symbol_id or "",
                    "target_ref": rel.target_ref or "",
                    "package": rel.package or "",
                    "language": rel.language or "",
                }
                for rel in ev.relations
            )
        if hash_match:
            embed_skipped.add(ev.path)
            continue
        if ev_hash:
            pending_hash_writes.append((ev_machine, ev.path, ev_hash))
        if ev.content and len(ev.content) < 100_000:
            chunks.extend(_build_embedding_chunks(ev, machine_id, project))

    content.upsert_files(files, content_hashes=file_hashes)
    # US-014: replace each content-bearing file's graph state inside one atomic
    # transaction so delete + entity upsert + relation upsert can't tear (e.g.
    # delete commits then entity upsert raises). Group entities/relations by
    # file_path before the loop. Files in embed_skipped have unchanged
    # content_hash and skip both qdrant re-delete and the graph replace
    # (graph already correct).
    by_path: dict[str, dict[str, list[dict]]] = {}
    for ent in entities:
        by_path.setdefault(ent["file_path"], {"entities": [], "relations": []})["entities"].append(ent)
    for rel in relations:
        by_path.setdefault(rel["file_path"], {"entities": [], "relations": []})["relations"].append(rel)
    content_paths = {f[2] for f in files}
    for _, _, file_path, _ in files:
        if file_path in embed_skipped:
            continue
        bucket = by_path.get(file_path, {"entities": [], "relations": []})
        memgraph.atomic_replace_file(
            file_path,
            machine_id,
            bucket["entities"],
            bucket["relations"],
        )
        # qdrant runs after the graph commit so a Qdrant failure leaves the
        # graph consistent. delete_by_file is idempotent — safe to retry.
        qdrant.delete_by_file(file_path, machine_id)
    # Semantic-only events (no ev.content, hence not in `files`) carry only
    # relations meant to merge onto the existing graph state. The legacy
    # delete-then-upsert path skipped delete for these; we preserve that
    # contract via plain upsert_relations (idempotent MERGE, no atomicity
    # needed because there's no delete to tear against).
    semantic_only_relations = [
        rel for rel in relations if rel["file_path"] not in content_paths
    ]
    if semantic_only_relations:
        memgraph.upsert_relations(semantic_only_relations)
    if files:
        content.refresh_project_tree(machine_id, project)

    if embed_skipped:
        print(f"[hub] content-hash short-circuit: skipped embed for {len(embed_skipped)} unchanged file(s)")

    if not chunks:
        return

    # Batch /embed calls at MAX_CHUNKS_PER_REQUEST to bound embed-service
    # request size on multi-window pathological files (e.g. minified bundles
    # that slipped past the 100KB gate).
    try:
        all_vectors: list[list[float]] = []
        for start in range(0, len(chunks), MAX_CHUNKS_PER_REQUEST):
            sub = chunks[start : start + MAX_CHUNKS_PER_REQUEST]
            r = await http.post(
                f"{EMBED_URL}/embed",
                json={"texts": [str(chunk["text"]) for chunk in sub], "mode": "document"},
            )
            r.raise_for_status()
            all_vectors.extend(r.json()["embeddings"])

        payloads = [
            {
                "machine_id": chunk["machine_id"],
                "project": chunk["project"],
                "file_path": chunk["file_path"],
                "entity": chunk["entity"],
                "entity_type": chunk["entity_type"],
                "chunk_id": chunk["chunk_id"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "snippet": chunk["snippet"],
            }
            for chunk in chunks
        ]
        qdrant.upsert(vectors=all_vectors, payloads=payloads)
    except HTTPException:
        raise
    except Exception as exc:
        # Surface embed failure as 502 so watcher retries via outbox.
        # Graph + content_store are already committed (at-least-once);
        # embedding is best-effort with auto-recovery on next file
        # modification (MERGE is idempotent on composite key). See
        # docs/consistency.md. Do NOT update content_hash on failure: the
        # watcher will retry, and we want the next batch to take the full
        # re-embed path.
        print(f"[hub] batch embed failed: {exc}")
        raise HTTPException(status_code=502, detail=f"embed-service unavailable: {exc}") from exc

    # Embed succeeded: persist new hashes to cache + DB so future identical
    # batches short-circuit. Only runs on the success path (after qdrant.upsert).
    for ev_machine, ev_path, ev_hash in pending_hash_writes:
        cache[(ev_machine, ev_path)] = ev_hash
        content.update_content_hash(ev_machine, ev_path, ev_hash)


def _handle_delete(
    ev: FileEvent,
    qdrant: QdrantCodeStore,
    memgraph: MemgraphCodeGraph,
    content: ContentStore,
    fallback_machine_id: str,
    fallback_project: str,
) -> None:
    machine_id = ev.machine_id or fallback_machine_id
    project = ev.project or fallback_project
    paths = [ev.path]
    if ev.old_path and ev.old_path != ev.path:
        paths.append(ev.old_path)
    for path in paths:
        qdrant.delete_by_file(path, machine_id)
        memgraph.delete_file(path, machine_id)
        content.delete_file(machine_id, path)
    content.refresh_project_tree(machine_id, project)


def _collect_stats(
    content: ContentStore,
    qdrant: QdrantCodeStore,
    memgraph: MemgraphCodeGraph,
    machine_id: str | None = None,
    project: str | None = None,
) -> dict:
    scope = {"machine_id": machine_id, "project": project}
    response = {
        "status": "ok",
        "scope": {key: value for key, value in scope.items() if value is not None},
        "content_store": None,
        "qdrant": None,
        "memgraph": None,
        "errors": {},
    }

    components = {
        "content_store": content,
        "qdrant": qdrant,
        "memgraph": memgraph,
    }
    for name, client in components.items():
        try:
            response[name] = client.stats(machine_id=machine_id, project=project)
        except Exception as exc:
            print(f"[hub] stats {name} failed: {exc}")
            response["status"] = "degraded"
            response["errors"][name] = "unavailable"

    return response


@app.get("/stats")
async def stats(request: Request, machine_id: str | None = None, project: str | None = None):
    _verify_auth(request)
    return _collect_stats(
        content=request.app.state.content,
        qdrant=request.app.state.qdrant,
        memgraph=request.app.state.memgraph,
        machine_id=machine_id,
        project=project,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("HUB_API_PORT", "9000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, workers=1)
