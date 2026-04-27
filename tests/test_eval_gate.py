"""Eval gate: regression check on retrieval baselines.

This test wraps `scripts/eval/run_baseline.py --check` via subprocess
so the test path matches the operator path used by `scripts/deploy.sh`."""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.smoke
def test_eval_baseline_jaccard_ge_threshold(require_stack):
    """Two runs of run_baseline.py must produce Jaccard >= 0.95 per query.
    Live stack required (Qdrant + embed-service)."""
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "eval" / "run_baseline.py"), "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"eval gate failed:\n{result.stdout}\n{result.stderr}"
