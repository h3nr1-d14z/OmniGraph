"""Jina v2 code-specialized embedding backend.

Uses ``jinaai/jina-embeddings-v2-base-code`` (Apache 2.0, 768-dim, 8192-token
context). Unlike Nomic models, Jina v2 does NOT use ``search_document:`` /
``search_query:`` prefixes — ``format_input`` is a no-op.

License verified Apache 2.0 via HF API 2026-04-26.
"""

import os
from pathlib import Path

import numpy as np
import structlog

from .base import BaseBackend, mean_pool

logger = structlog.get_logger(__name__)

DEFAULT_MODEL = "jinaai/jina-embeddings-v2-base-code"


class JinaCodeBackend(BaseBackend):
    """Code-specialized backend using Jina v2 base code model."""

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or os.getenv("EMBED_MODEL_NAME", DEFAULT_MODEL)
        self._tokenizer = None
        self._session = None
        self._dim = 768
        self._load()

    def _load(self) -> None:
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer

        cache_dir = os.getenv("MODEL_CACHE")
        kwargs = {"cache_dir": cache_dir} if cache_dir else {}
        # FP32 only (~614 MB) — INT8 quantization risks entangling quality drift
        # with the Phase 4 cutover regression gate; revisit after gate passes.
        local_dir = snapshot_download(
            repo_id=self.model_name,
            allow_patterns=["onnx/model.onnx", "tokenizer.json", "config.json"],
            **kwargs,
        )

        onnx_path = Path(local_dir) / "onnx" / "model.onnx"
        tok_path = Path(local_dir) / "tokenizer.json"
        if not onnx_path.exists():
            raise FileNotFoundError(f"ONNX model not found at {onnx_path}")

        providers = ort.get_available_providers()
        self._session = ort.InferenceSession(str(onnx_path), providers=providers)
        self._tokenizer = Tokenizer.from_file(str(tok_path))
        self._tokenizer.enable_truncation(max_length=8192)
        logger.info("model_loaded", backend="jina-code", model=self.model_name, providers=self._session.get_providers())

    @property
    def name(self) -> str:
        return f"jina-{self.model_name}"

    @property
    def vector_dim(self) -> int:
        return self._dim

    def format_input(self, texts: list[str], mode: str) -> list[str]:
        # Jina v2 base code does not require instruction prefixes; query and
        # document modes share the same format.
        return texts

    def embed(self, texts: list[str], mode: str = "document") -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)

        texts = self.format_input(texts, mode)
        encoded = self._tokenizer.encode_batch(texts)

        max_len = max(len(e.ids) for e in encoded)
        input_ids = np.zeros((len(texts), max_len), dtype=np.int64)
        attention_mask = np.zeros((len(texts), max_len), dtype=np.int64)
        for i, e in enumerate(encoded):
            input_ids[i, : len(e.ids)] = e.ids
            attention_mask[i, : len(e.attention_mask)] = e.attention_mask

        token_type_ids = np.zeros_like(input_ids)
        outputs = self._session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )
        return mean_pool(outputs[0], attention_mask)
