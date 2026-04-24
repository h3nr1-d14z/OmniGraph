"""ONNX Runtime embedding backend (default, cross-platform)."""

import os
from pathlib import Path

import numpy as np
from huggingface_hub import hf_hub_download, snapshot_download
from tokenizers import Tokenizer

from .base import BaseBackend, mean_pool, prefix_texts

DEFAULT_MODEL = "nomic-ai/nomic-embed-text-v1.5"
ONNX_FILE = "onnx/model.onnx"


class OnnxBackend(BaseBackend):
    """ONNX Runtime backend for Nomic Embed models."""

    def __init__(self, model_name: str | None = None, model_path: str | None = None):
        self.model_name = model_name or os.getenv("EMBED_MODEL_NAME", DEFAULT_MODEL)
        self.model_path = model_path
        self._session = None
        self._tokenizer = None
        self._dim = 768

        self._load()

    def _load(self) -> None:
        import onnxruntime as ort

        providers = ort.get_available_providers()
        print(f"[onnx] Available providers: {providers}")

        if self.model_path and Path(self.model_path).exists():
            onnx_path = self.model_path
            tok_path = Path(self.model_path).parent / "tokenizer.json"
        else:
            cache_dir = os.getenv("MODEL_CACHE")
            kwargs = {"cache_dir": cache_dir} if cache_dir else {}
            local_dir = snapshot_download(
                repo_id=self.model_name,
                allow_patterns=["onnx/*", "tokenizer.json"],
                **kwargs,
            )
            onnx_path = Path(local_dir) / ONNX_FILE
            tok_path = Path(local_dir) / "tokenizer.json"

        if not onnx_path.exists():
            raise FileNotFoundError(f"ONNX model not found at {onnx_path}")

        self._session = ort.InferenceSession(str(onnx_path), providers=providers)
        self._tokenizer = Tokenizer.from_file(str(tok_path))
        print(f"[onnx] Loaded {self.model_name} with providers {self._session.get_providers()}")

    @property
    def name(self) -> str:
        return f"onnx-{self.model_name}"

    @property
    def vector_dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str], mode: str = "document") -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)

        texts = prefix_texts(texts, mode)
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
