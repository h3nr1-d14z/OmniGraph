"""MLX backend for Apple Silicon (Metal)."""

import os

import numpy as np
import structlog

from .base import BaseBackend, mean_pool, prefix_texts

logger = structlog.get_logger(__name__)

DEFAULT_MODEL = "nomic-ai/nomic-embed-text-v1.5"


class MlxBackend(BaseBackend):
    """MLX backend using HuggingFace transformers via mlx-lm."""

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or os.getenv("EMBED_MODEL_NAME", DEFAULT_MODEL)
        self._model = None
        self._tokenizer = None
        self._dim = 768

        self._load()

    def _load(self) -> None:
        try:
            import mlx.core as mx
            from huggingface_hub import snapshot_download
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise ImportError(
                "mlx or transformers not installed. "
                "Run: pip install mlx mlx-lm transformers"
            ) from exc

        cache_dir = os.getenv("MODEL_CACHE")
        kwargs = {"cache_dir": cache_dir} if cache_dir else {}
        local_dir = snapshot_download(
            repo_id=self.model_name,
            **kwargs,
        )
        self._tokenizer = AutoTokenizer.from_pretrained(local_dir)
        # For MLX we use the model weights directly via mlx-lm if available,
        # otherwise fall back to a simple pooled approach.
        # Nomic models on MLX typically use the transformers path.
        from transformers import AutoModel

        self._model = AutoModel.from_pretrained(local_dir)
        logger.info("model_loaded", backend="mlx", model=self.model_name)

    @property
    def name(self) -> str:
        return f"mlx-{self.model_name}"

    @property
    def vector_dim(self) -> int:
        return self._dim

    def format_input(self, texts: list[str], mode: str) -> list[str]:
        return prefix_texts(texts, mode)

    def embed(self, texts: list[str], mode: str = "document") -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)

        texts = self.format_input(texts, mode)
        inputs = self._tokenizer(
            texts, return_tensors="np", padding=True, truncation=True, max_length=8192
        )
        import mlx.core as mx

        input_ids = mx.array(inputs["input_ids"])
        attention_mask = mx.array(inputs["attention_mask"])

        # Note: This is a simplified path.
        # In production with mlx-lm you would use the dedicated embedding path.
        outputs = self._model(input_ids)
        return mean_pool(np.array(outputs.last_hidden_state), np.array(attention_mask))
