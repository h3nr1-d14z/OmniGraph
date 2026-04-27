"""US-007 acceptance: per-backend format_input dispatch.

Verifies the abstract method contract without instantiating backends
(model downloads are gated to runtime/Phase 4 cutover). Each concrete
backend class must override format_input — adding a new backend without
overriding it is a hard error (forced by ABC).
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Make hub/embed_service importable.
sys.path.insert(0, str(REPO / "hub" / "embed_service"))

from backends.base import BaseBackend, prefix_texts  # noqa: E402


def test_baseBackend_format_input_is_abstract():
    """Subclassing BaseBackend without overriding format_input must fail."""

    class Bad(BaseBackend):
        # Intentionally missing format_input.
        @property
        def name(self): return "bad"
        @property
        def vector_dim(self): return 768
        def embed(self, texts, mode="document"): return None

    try:
        Bad()
    except TypeError as e:
        assert "format_input" in str(e), e
        return
    raise AssertionError("expected TypeError for missing format_input")


def test_nomic_mlx_format_input_prepends_prefix():
    """MlxBackend.format_input prepends search_document:/search_query: per nomic contract."""
    src = (REPO / "hub" / "embed_service" / "backends" / "mlx_backend.py").read_text()
    assert "def format_input(self, texts: list[str], mode: str) -> list[str]:" in src, "MlxBackend missing format_input override"
    assert "return prefix_texts(texts, mode)" in src, "MlxBackend should delegate to prefix_texts"


def test_nomic_onnx_format_input_prepends_prefix():
    """OnnxBackend.format_input prepends search_document:/search_query: per nomic contract."""
    src = (REPO / "hub" / "embed_service" / "backends" / "onnx_backend.py").read_text()
    assert "def format_input(self, texts: list[str], mode: str) -> list[str]:" in src, "OnnxBackend missing format_input override"
    assert "return prefix_texts(texts, mode)" in src, "OnnxBackend should delegate to prefix_texts"


def test_jina_code_format_input_is_no_op():
    """JinaCodeBackend.format_input returns texts unchanged (no nomic prefix)."""
    src = (REPO / "hub" / "embed_service" / "backends" / "jina_code_backend.py").read_text()
    assert "def format_input(self, texts: list[str], mode: str) -> list[str]:" in src, "JinaCodeBackend missing format_input"
    assert "return texts" in src, "JinaCodeBackend.format_input should return texts unchanged"
    # The CODE (not docstring/comments) must not call prefix_texts.
    assert "prefix_texts" not in src, "JinaCodeBackend must NOT use prefix_texts (nomic-only helper)"


def test_prefix_texts_helper_still_works():
    """The prefix_texts helper retains its contract for nomic backends."""
    out = prefix_texts(["hello"], "document")
    assert out == ["search_document: hello"], out
    out = prefix_texts(["hello"], "query")
    assert out == ["search_query: hello"], out


def test_backend_factory_supports_jina():
    """get_backend('jina') routes to JinaCodeBackend (without instantiating)."""
    src = (REPO / "hub" / "embed_service" / "backends" / "__init__.py").read_text()
    assert 'name == "jina"' in src, "get_backend missing 'jina' branch"
    assert "JinaCodeBackend" in src, "__init__ should import JinaCodeBackend on jina branch"
