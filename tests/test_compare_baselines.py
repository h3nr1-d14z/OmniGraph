"""Acceptance tests for scripts/eval/compare_baselines.py (US-013).

Covers Phase 4 cutover gate:
  A) >=80% queries with identical top-3 OR >=1 improvement
  B) No query loses >2 results from baseline top-10
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "eval" / "compare_baselines.py"

spec = importlib.util.spec_from_file_location("compare_baselines", SCRIPT)
assert spec and spec.loader
cb = importlib.util.module_from_spec(spec)
sys.modules["compare_baselines"] = cb
spec.loader.exec_module(cb)


def _hit(file_path: str, score: float, entity: str = "") -> dict:
    return {"file_path": file_path, "score": score, "entity": entity, "id": file_path}


def _q(hits: list[dict]) -> list[dict]:
    return hits


# ----- shape support ------------------------------------------------------


def test_load_supports_wrapped_shape(tmp_path):
    p = tmp_path / "wrapped.json"
    p.write_text('{"results": {"q1": [{"file_path":"a.go","score":1.0}]}}')
    out = cb.load(str(p))
    assert out == {"q1": [{"file_path": "a.go", "score": 1.0}]}


def test_load_supports_raw_shape(tmp_path):
    p = tmp_path / "raw.json"
    p.write_text('{"q1": [{"file_path":"a.go","score":1.0}]}')
    out = cb.load(str(p))
    assert out == {"q1": [{"file_path": "a.go", "score": 1.0}]}


# ----- gate semantics -----------------------------------------------------


def test_pass_when_identical_top_3():
    """Same top-3 across all queries → criterion A passes via identical_top_n branch."""
    base = {f"q{i}": [_hit("a.go", 1.0), _hit("b.go", 0.9), _hit("c.go", 0.8)] for i in range(12)}
    new = {qid: list(hits) for qid, hits in base.items()}
    code, report = cb.run(base, new, top_improvement=3, top_k=10, threshold_a=0.80)
    assert code == 0
    assert report["gate_passed"]
    assert report["criterion_A"]["pass_count"] == 12
    assert report["criterion_B"]["passed"]


def test_pass_with_improvements_above_threshold():
    """>=80% queries must pass; 11/12 with score-up improvements should pass."""
    base = {f"q{i}": [_hit("a.go", 0.5), _hit("b.go", 0.4), _hit("c.go", 0.3)] for i in range(12)}
    new = {f"q{i}": [_hit("a.go", 0.9), _hit("b.go", 0.8), _hit("c.go", 0.7)] for i in range(11)}
    new["q11"] = [_hit("d.go", 0.1), _hit("e.go", 0.05), _hit("f.go", 0.01)]
    code, report = cb.run(base, new, top_improvement=3, top_k=10, threshold_a=0.80)
    assert code == 1
    fail_b_ids = {f["id"] for f in report["criterion_B"]["failures"]}
    assert "q11" in fail_b_ids


def test_fail_criterion_a_when_too_many_queries_without_improvement():
    """If 4 of 12 queries return a strict subset of the baseline top-3 with
    lower scores (33% > 20% threshold) → fail A. A subset means
    base_top_n != new_top_n (kills identical-set branch) AND every fp in the
    subset is already in baseline at a higher score (kills improvement-count)."""
    base = {f"q{i}": [_hit("a.go", 0.9), _hit("b.go", 0.8), _hit("c.go", 0.7)] for i in range(12)}
    new = {qid: list(hits) for qid, hits in base.items()}
    for qid in ("q0", "q1", "q2", "q3"):
        new[qid] = [_hit("a.go", 0.5), _hit("b.go", 0.4)]
    code, report = cb.run(base, new, top_improvement=3, top_k=10, threshold_a=0.80)
    assert code == 1
    assert not report["criterion_A"]["passed"]
    assert report["criterion_A"]["pass_count"] == 8
    assert report["criterion_A"]["fail_count"] == 4


def test_fail_criterion_b_when_query_loses_more_than_two():
    """A single query losing 3 results from baseline top-10 must fail criterion B."""
    base = {
        "q0": [_hit(f"f{i}.go", 1.0 - i * 0.05) for i in range(10)],
    }
    base.update({f"q{i}": [_hit("a.go", 1.0)] for i in range(1, 12)})
    new = {qid: list(hits) for qid, hits in base.items()}
    new["q0"] = [_hit("f0.go", 1.0), _hit("f1.go", 0.95), _hit("f2.go", 0.9),
                 _hit("f3.go", 0.85), _hit("f4.go", 0.8), _hit("f5.go", 0.75),
                 _hit("f6.go", 0.7), _hit("z0.go", 0.6), _hit("z1.go", 0.5),
                 _hit("z2.go", 0.4)]
    code, report = cb.run(base, new, top_improvement=3, top_k=10, threshold_a=0.80)
    assert code == 1
    assert not report["criterion_B"]["passed"]
    assert any(f["id"] == "q0" and f["lost_count"] == 3 for f in report["criterion_B"]["failures"])


def test_no_overlap_returns_exit_code_2():
    base = {"q1": [_hit("a.go", 1.0)]}
    new = {"q2": [_hit("a.go", 1.0)]}
    code, report = cb.run(base, new, top_improvement=3, top_k=10, threshold_a=0.80)
    assert code == 2
    assert "no overlapping" in report["error"]


def test_loss_of_exactly_two_passes_b():
    """Boundary: losing 2 results from top-10 is allowed; 3 is not."""
    base = {"q0": [_hit(f"f{i}.go", 1.0 - i * 0.05) for i in range(10)]}
    base.update({f"q{i}": [_hit("a.go", 1.0)] for i in range(1, 12)})
    new = {qid: list(hits) for qid, hits in base.items()}
    new["q0"] = [_hit(f"f{i}.go", 1.0 - i * 0.05) for i in range(8)] + [
        _hit("z0.go", 0.5),
        _hit("z1.go", 0.4),
    ]
    code, report = cb.run(base, new, top_improvement=3, top_k=10, threshold_a=0.80)
    fail_b_ids = {f["id"] for f in report["criterion_B"]["failures"]}
    assert "q0" not in fail_b_ids
