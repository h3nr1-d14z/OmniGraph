"""Base embedding backend interface."""

from abc import ABC, abstractmethod

import numpy as np


def prefix_texts(texts: list[str], mode: str = "document") -> list[str]:
    prefix = "search_query" if mode == "query" else "search_document"
    return [f"{prefix}: {text}" for text in texts]


def mean_pool(last_hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    mask = np.expand_dims(attention_mask, -1).astype(np.float32)
    masked = last_hidden * mask
    summed = masked.sum(axis=1)
    counts = mask.sum(axis=1).clip(min=1e-9)
    return (summed / counts).astype(np.float32)


class BaseBackend(ABC):
    """Base class for all embedding backends."""

    @abstractmethod
    def embed(self, texts: list[str], mode: str = "document") -> np.ndarray:
        """Embed a batch of texts into vectors."""
        ...

    @property
    @abstractmethod
    def vector_dim(self) -> int:
        """Dimension of output vectors."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend name for logging."""
        ...

    @abstractmethod
    def format_input(self, texts: list[str], mode: str) -> list[str]:
        """Apply per-backend pre-tokenizer transformation (e.g., model-specific
        instruction prefixes). Each backend must override:

        - Nomic-style models: prepend ``search_document:`` / ``search_query:``.
        - Jina-style code models: return texts unchanged.
        - Future code-specific models: implement their own contract.

        Adding a new backend without overriding this method is a hard error
        (forced by ABC), preventing silent prefix-mismatch bugs.
        """
        ...
