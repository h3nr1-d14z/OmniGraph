"""Acceptance tests for US-016 (runbook updates) and US-017 (frozen baselines).

Exercises the documentation contract that Phase 4 cutover depends on. The
runbook is operator-facing — drift here is a real failure mode (operator
runs the wrong command, fails the gate, rolls back unnecessarily).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNBOOK = REPO / "docs" / "runbooks" / "reindex.md"
FROZEN_DIR = REPO / "tests" / "eval" / "baselines"
FROZEN_README = FROZEN_DIR / "README.md"


# ---------- runbook content -----------------------------------------------


def test_runbook_size_comment_corrected():
    src = RUNBOOK.read_text()
    assert "614 MB" in src, "expected updated size comment '~614 MB'"
    assert "~250MB ONNX" not in src, (
        "stale '~250MB ONNX' comment must be removed — the model is 612 MB"
    )


def test_runbook_step_4_6_smoke_embed_present():
    src = RUNBOOK.read_text()
    assert "Step 4.6" in src
    assert "Smoke embed" in src
    assert "768" in src, "smoke embed must assert 768-dim output"


def test_runbook_steps_5_1_through_5_4_present():
    src = RUNBOOK.read_text()
    for marker in ("Step 5.1", "Step 5.2", "Step 5.3", "Step 5.4"):
        assert marker in src, f"missing {marker} in runbook"


def test_runbook_step_5_5_freezes_baseline():
    src = RUNBOOK.read_text()
    assert "Step 5.5" in src
    assert "tests/eval/baselines/code_v2_jina.json" in src


def test_runbook_invokes_compare_baselines_with_frozen_nomic():
    src = RUNBOOK.read_text()
    assert "scripts/eval/compare_baselines.py" in src
    assert "tests/eval/baselines/code_v1_nomic.json" in src, (
        "Step 5.3 must compare jina_v2 against the frozen nomic reference, "
        "not the live eval/baseline.json scratch file"
    )


def test_runbook_narrowed_allow_patterns():
    src = RUNBOOK.read_text()
    assert "'onnx/model.onnx'" in src, (
        "Step 3.5 pre-warm must use narrowed pattern matching jina_code_backend.py"
    )


def test_runbook_pre_flight_baseline_collection_check():
    src = RUNBOOK.read_text()
    assert "Baseline collection-field validation" in src
    assert "omnigraph_code" in src, (
        "pre-flight must warn about stale captures against legacy collection"
    )


def test_runbook_skip_machine_ids_documented():
    src = RUNBOOK.read_text()
    assert "skip-machine-ids" in src, (
        "operator needs to know how to exclude test fixtures during cutover"
    )


# ---------- US-017 frozen baselines ---------------------------------------


def test_frozen_baselines_directory_exists():
    assert FROZEN_DIR.is_dir()
    assert (FROZEN_DIR / ".gitkeep").exists()


def test_frozen_baselines_policy_readme():
    text = FROZEN_README.read_text()
    assert "never" in text.lower() and "overwrite" in text.lower()
    assert "code_v2_jina.json" in text
    assert "code_v1_nomic.json" in text


def test_frozen_baselines_policy_lists_chain():
    """Policy must enumerate the per-phase chain so operators know where to
    promote the next baseline."""
    text = FROZEN_README.read_text()
    for name in ("code_v1_nomic", "code_v2_jina", "code_v3_jina_symbol"):
        assert name in text, f"missing {name} in frozen-baseline policy chain"
