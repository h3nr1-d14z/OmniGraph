"""Rolling-window latency histogram for hub services.

Stdlib-only. Each route keeps the last N samples (default 1000); snapshot()
returns p50/p95/p99 in ms plus sample count. Thread-safe — FastAPI dispatches
sync routes on a threadpool, so the lock is required even with workers=1.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager


class LatencyTracker:
    def __init__(self, window: int = 1000) -> None:
        self._window = window
        self._samples: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def record(self, route: str, elapsed_ms: float) -> None:
        with self._lock:
            buf = self._samples.get(route)
            if buf is None:
                buf = deque(maxlen=self._window)
                self._samples[route] = buf
            buf.append(elapsed_ms)

    @contextmanager
    def track(self, route: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(route, (time.perf_counter() - start) * 1000.0)

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        out: dict[str, dict[str, float | int]] = {}
        with self._lock:
            buffers = {r: list(b) for r, b in self._samples.items()}
        for route, samples in buffers.items():
            if not samples:
                continue
            samples.sort()
            n = len(samples)
            out[route] = {
                "count": n,
                "p50_ms": round(_percentile(samples, 0.50), 3),
                "p95_ms": round(_percentile(samples, 0.95), 3),
                "p99_ms": round(_percentile(samples, 0.99), 3),
            }
        return out

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()


def _percentile(sorted_samples: list[float], q: float) -> float:
    """Nearest-rank percentile on a pre-sorted list. q in [0,1]."""
    n = len(sorted_samples)
    if n == 0:
        return 0.0
    rank = max(1, math.ceil(q * n))
    return sorted_samples[rank - 1]


_default_tracker = LatencyTracker()


def get_tracker() -> LatencyTracker:
    return _default_tracker


# Bucket label for unmatched routes. Distinct unrouted paths (404s with
# random ids) collapse into one entry so LatencyTracker._samples is bounded
# by the number of registered FastAPI routes plus this one fallback.
OTHER_ROUTE_LABEL = "_other_"


def route_label(scope: dict) -> str:
    """Return the matched FastAPI route's path template, or ``OTHER_ROUTE_LABEL``
    if the request didn't match any route. Used by the api_server latency
    middleware to keep tracker keys bounded."""
    route = scope.get("route")
    return getattr(route, "path", None) or OTHER_ROUTE_LABEL
