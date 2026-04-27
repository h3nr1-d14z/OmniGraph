"""Unit tests for hub/latency.py percentile + threading semantics."""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub"))

from latency import OTHER_ROUTE_LABEL, LatencyTracker, route_label  # noqa: E402


def test_empty_snapshot_skips_routes():
    t = LatencyTracker()
    assert t.snapshot() == {}


def test_single_sample_percentiles_match():
    t = LatencyTracker()
    t.record("r1", 12.5)
    snap = t.snapshot()["r1"]
    assert snap["count"] == 1
    assert snap["p50_ms"] == 12.5
    assert snap["p95_ms"] == 12.5
    assert snap["p99_ms"] == 12.5


def test_nearest_rank_percentile_known_distribution():
    t = LatencyTracker()
    for v in range(1, 101):
        t.record("r1", float(v))
    snap = t.snapshot()["r1"]
    assert snap["count"] == 100
    # Nearest-rank: ceil(0.50 * 100) = 50 → sorted[49] = 50
    assert snap["p50_ms"] == 50.0
    # ceil(0.95 * 100) = 95 → sorted[94] = 95
    assert snap["p95_ms"] == 95.0
    # ceil(0.99 * 100) = 99 → sorted[98] = 99
    assert snap["p99_ms"] == 99.0


def test_window_eviction_keeps_recent_samples_only():
    t = LatencyTracker(window=10)
    for v in range(1, 21):  # 20 samples, window=10 keeps last 10 (11..20)
        t.record("r1", float(v))
    snap = t.snapshot()["r1"]
    assert snap["count"] == 10
    assert snap["p50_ms"] == 15.0  # ceil(.5*10)=5 → sorted[4] = 15
    assert snap["p99_ms"] == 20.0


def test_track_context_manager_records_elapsed():
    t = LatencyTracker()
    with t.track("op"):
        pass
    snap = t.snapshot()["op"]
    assert snap["count"] == 1
    assert 0.0 <= snap["p50_ms"] < 100.0  # generous; just ensure it ran


def test_concurrent_record_thread_safe():
    t = LatencyTracker(window=10000)
    n_threads = 8
    per_thread = 500

    def worker(start: int) -> None:
        for i in range(per_thread):
            t.record("r1", float(start + i))

    threads = [threading.Thread(target=worker, args=(k * per_thread,)) for k in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    snap = t.snapshot()["r1"]
    assert snap["count"] == n_threads * per_thread


def test_separate_routes_isolated():
    t = LatencyTracker()
    t.record("a", 1.0)
    t.record("b", 100.0)
    snap = t.snapshot()
    assert snap["a"]["p50_ms"] == 1.0
    assert snap["b"]["p50_ms"] == 100.0


def test_reset_clears_all_routes():
    t = LatencyTracker()
    t.record("r1", 1.0)
    t.record("r2", 2.0)
    t.reset()
    assert t.snapshot() == {}


def test_invalid_quantile_handles_zero_samples():
    t = LatencyTracker()
    # No record calls — snapshot returns empty
    assert t.snapshot() == {}


@pytest.mark.parametrize("q,expected_count", [(50, 50), (1000, 1000)])
def test_window_capacity_respected(q, expected_count):
    t = LatencyTracker(window=q)
    for i in range(q + 100):
        t.record("r1", float(i))
    snap = t.snapshot()["r1"]
    assert snap["count"] == expected_count


def test_route_label_buckets_unmatched_paths():
    """The api_server middleware records via route_label(scope), so a 404
    with a random path collapses into OTHER_ROUTE_LABEL instead of minting
    a new LatencyTracker key per URL."""

    class FakeRoute:
        def __init__(self, path: str | None) -> None:
            self.path = path

    assert route_label({"route": FakeRoute("/batch")}) == "/batch"
    assert route_label({"route": None}) == OTHER_ROUTE_LABEL
    assert route_label({}) == OTHER_ROUTE_LABEL
    assert route_label({"route": FakeRoute(None)}) == OTHER_ROUTE_LABEL
