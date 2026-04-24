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
MAX_EMBED_CHARS = int(os.getenv("EMBED_MAX_CHARS", "2000"))
MAX_SNIPPET_CHARS = int(os.getenv("SNIPPET_MAX_CHARS", "500"))


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
    await _handle_upserts(
        upserts,
        batch.machine_id,
        batch.project,
        qdrant,
        memgraph,
        content,
        request.app.state.http,
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
        return chunks

    return [
        {
            "machine_id": machine_id,
            "project": project,
            "file_path": ev.path,
            "entity": ev.entities[0].name if ev.entities else "",
            "entity_type": ev.entities[0].type if ev.entities else "file",
            "chunk_id": "0",
            "start_line": 1,
            "end_line": len(lines),
            "text": ev.content[:MAX_EMBED_CHARS],
            "snippet": ev.content[:MAX_SNIPPET_CHARS],
        }
    ]


async def _handle_upserts(
    events: list[FileEvent],
    machine_id: str,
    project: str,
    qdrant: QdrantCodeStore,
    memgraph: MemgraphCodeGraph,
    content: ContentStore,
    http: httpx.AsyncClient,
) -> None:
    files: list[tuple[str, str, str, str]] = []
    entities: list[dict] = []
    relations: list[dict] = []
    chunks: list[dict[str, str | int]] = []

    for ev in events:
        if ev.content:
            files.append((machine_id, project, ev.path, ev.content))
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
                }
                for rel in ev.relations
            )
        if ev.content and len(ev.content) < 100_000:
            chunks.extend(_build_embedding_chunks(ev, machine_id, project))

    content.upsert_files(files)
    for _, _, file_path, _ in files:
        memgraph.delete_file(file_path, machine_id)
        qdrant.delete_by_file(file_path, machine_id)
    memgraph.upsert_entities(entities)
    memgraph.upsert_relations(relations)
    if files:
        content.refresh_project_tree(machine_id, project)

    if not chunks:
        return

    try:
        r = await http.post(
            f"{EMBED_URL}/embed",
            json={"texts": [str(chunk["text"]) for chunk in chunks], "mode": "document"},
        )
        r.raise_for_status()
        vectors = r.json()["embeddings"]
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
        qdrant.upsert(vectors=vectors, payloads=payloads)
    except Exception as exc:
        print(f"[hub] batch embed failed: {exc}")


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
