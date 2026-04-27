"""Static-source verification for US-002 embedding capacity unlock.

Runtime verification requires rebuilt embed-service container; this
acceptance test verifies the source contract via inspection so the fix
cannot regress silently.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_main_py_embed_max_chars_default_is_32000():
    """api_server/main.py defaults EMBED_MAX_CHARS to 32000 after Phase 4
    (Phase 1 raised 2000→6000 as quick win; Phase 4 raises to model context)."""
    src = (REPO / "hub" / "api_server" / "main.py").read_text()
    m = re.search(r'MAX_EMBED_CHARS\s*=\s*int\(os\.getenv\(\s*"EMBED_MAX_CHARS"\s*,\s*"(\d+)"\s*\)\)', src)
    assert m is not None, "MAX_EMBED_CHARS line not found"
    assert int(m.group(1)) == 32000, f"expected 32000 default, got {m.group(1)}"


def test_mlx_backend_passes_max_length_8192():
    """MLX tokenizer call passes max_length=8192 to escape default 512 cap."""
    src = (REPO / "hub" / "embed_service" / "backends" / "mlx_backend.py").read_text()
    assert "max_length=8192" in src, "mlx_backend.py missing max_length=8192"


def test_onnx_backend_enables_truncation_at_8192():
    """ONNX tokenizer enables truncation with max_length=8192."""
    src = (REPO / "hub" / "embed_service" / "backends" / "onnx_backend.py").read_text()
    assert "enable_truncation(max_length=8192)" in src, "onnx_backend.py missing enable_truncation(max_length=8192)"


def test_hub_raises_502_on_embed_failure():
    """US-004: Hub must raise HTTPException(502) on embed-service failure
    instead of swallowing the exception and returning 200."""
    src = (REPO / "hub" / "api_server" / "main.py").read_text()
    assert "HTTPException(status_code=502" in src, "main.py missing HTTPException(status_code=502)"
    assert 'detail=f"embed-service unavailable' in src, "main.py 502 detail message missing"


def test_consistency_doc_exists():
    """US-004: docs/consistency.md documents partial-success contract."""
    doc = REPO / "docs" / "consistency.md"
    assert doc.exists(), "docs/consistency.md missing"
    text = doc.read_text()
    for marker in ("at-least-once", "best-effort", "MERGE", "502"):
        assert marker in text, f"docs/consistency.md missing marker: {marker}"
