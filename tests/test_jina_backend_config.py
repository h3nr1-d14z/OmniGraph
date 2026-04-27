"""Acceptance test for US-015: jina_code_backend.py allow_patterns narrowing.

Source-grep is the right level here: the model load is heavy (614 MB) and
not exercised in unit tests. We assert the `snapshot_download` call uses the
narrowed pattern via static read.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "hub" / "embed_service" / "backends" / "jina_code_backend.py"


def test_allow_patterns_uses_specific_onnx_filename():
    src = BACKEND.read_text()
    assert '"onnx/model.onnx"' in src, (
        "expected narrowed allow_patterns containing 'onnx/model.onnx'"
    )


def test_allow_patterns_no_longer_uses_wildcard_glob():
    src = BACKEND.read_text()
    assert '"onnx/*"' not in src, (
        "expected the onnx/* glob to be replaced — it pulls 460 MB of unused "
        "FP16/INT8 variants"
    )


def test_tokenizer_and_config_still_in_allow_patterns():
    src = BACKEND.read_text()
    # Sanity: don't accidentally drop the tokenizer or config from the pull set
    assert '"tokenizer.json"' in src
    assert '"config.json"' in src
