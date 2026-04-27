"""FastAPI embedding service for OmniGraph Hub."""

import os
import threading
from contextlib import asynccontextmanager

import numpy as np
from backends import get_backend
from fastapi import FastAPI
from pydantic import BaseModel

# Lazy-load backend on first request to avoid slow startup
_backend = None
_backend_lock = threading.Lock()


def _get_backend():
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = get_backend()
    return _backend


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eager-load model so /ready never triggers a cold load
    _get_backend()
    yield


app = FastAPI(title="OmniGraph Embed Service", lifespan=lifespan)


class EmbedRequest(BaseModel):
    texts: list[str]
    mode: str = "document"


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    backend: str
    vector_dim: int


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    backend = _get_backend()
    return {"status": "ready", "backend": backend.name, "vector_dim": backend.vector_dim}


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest):
    backend = _get_backend()
    vectors: np.ndarray = backend.embed(req.texts, mode=req.mode)
    return EmbedResponse(
        embeddings=vectors.tolist(),
        backend=backend.name,
        vector_dim=backend.vector_dim,
    )


@app.get("/info")
def info():
    backend = _get_backend()
    return {
        "backend": backend.name,
        "vector_dim": backend.vector_dim,
        "model": os.getenv("EMBED_MODEL_NAME", "nomic-ai/nomic-embed-text-v1.5"),
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("EMBED_SERVICE_PORT", "8000"))
    workers = int(os.getenv("EMBED_WORKERS", "1"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, workers=workers)
