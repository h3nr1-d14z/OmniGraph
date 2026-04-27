"""US-010 acceptance: re-index script + cutover runbook.

The quantitative gate (eval/jina_v2.json vs eval/baseline.json) requires
operator-driven cutover (jina model download + embed-service rebuild +
re-index runtime). This test verifies the infrastructure is in place; the
gate itself is run in docs/reindex.md "Acceptance gate" section.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_reindex_script_exists_and_executable():
    script = REPO / "scripts" / "reindex.py"
    assert script.exists(), "scripts/reindex.py missing"
    assert os.access(script, os.X_OK), "scripts/reindex.py not executable (chmod +x)"


def test_reindex_dry_run_lists_files_idempotently():
    """--dry-run must succeed and produce identical output across two runs."""
    cmd = [sys.executable, "scripts/reindex.py", "--dry-run"]
    r1 = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=30)
    r2 = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=30)
    assert r1.returncode == 0, f"first run failed: {r1.stderr}"
    assert r2.returncode == 0, f"second run failed: {r2.stderr}"
    assert r1.stdout == r2.stdout, "dry-run output not deterministic (idempotency proxy failed)"


def test_runbook_documents_5step_quiesce_protocol():
    doc = (REPO / "docs" / "reindex.md").read_text()
    # 5-step protocol must mention all critical steps.
    for marker in (
        "Step 1: Disable semantic worker",
        "Step 2: Drain outbox",
        "Step 3: Build new Qdrant collection",
        "Step 4: Stop Hub",
        "Step 4.5: Healthcheck gate",
        "Step 5: Re-index",
    ):
        assert marker in doc, f"docs/reindex.md missing protocol marker: {marker!r}"


def test_runbook_has_rollback_matrix_with_5_rows():
    doc = (REPO / "docs" / "reindex.md").read_text()
    # Match table rows: lines starting with `| ` that aren't headers/separators.
    rollback_section = doc.split("## Rollback matrix", 1)[-1].split("##", 1)[0]
    rows = [line for line in rollback_section.splitlines()
            if line.startswith("| ") and "---" not in line and "Component" not in line]
    assert len(rows) >= 5, f"rollback matrix needs ≥5 rows, found {len(rows)}"


def test_runbook_documents_acceptance_gate_thresholds():
    doc = (REPO / "docs" / "reindex.md").read_text()
    assert "≥80%" in doc, "acceptance gate threshold ≥80% missing from runbook"
    assert "no query loses" in doc, "acceptance gate 'no query loses >2' missing from runbook"
    assert "eval/jina_v2.json" in doc, "runbook must reference eval/jina_v2.json output"


def test_runbook_documents_healthcheck_60s_timeout():
    doc = (REPO / "docs" / "reindex.md").read_text()
    assert re.search(r"60s|60 ?second", doc, re.IGNORECASE), "60s healthcheck timeout missing"
