"""Live API request metrics for the administrator dashboard (Story 9.4).

Every HTTP request the API serves is timed and counted by ``RequestMetricsMiddleware``.
The dashboard reads the rolling window kept here, so latency and error rate are measured
from real traffic, not configured or invented. The window lives in memory: it starts empty
when the API restarts, and the dashboard says so instead of showing zeros as if they were
measurements.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import threading
import time

WINDOW_SECONDS = 15 * 60
MAX_SAMPLES = 20_000
# The dashboard polls this path itself; counting it would make an idle system look busy.
IGNORED_PATH_PREFIXES = ("/api/v1/admin/dashboard",)


@dataclass(frozen=True)
class _Sample:
    at: float
    latency_ms: float
    status: int


class RequestMetrics:
    """Thread-safe rolling window of recent requests."""

    def __init__(self, window_seconds: int = WINDOW_SECONDS, max_samples: int = MAX_SAMPLES) -> None:
        self._window = window_seconds
        self._samples: deque[_Sample] = deque(maxlen=max_samples)
        self._lock = threading.Lock()
        self._started_at = time.time()

    def record(self, latency_ms: float, status: int, *, now: float | None = None) -> None:
        with self._lock:
            self._samples.append(_Sample(now if now is not None else time.time(), latency_ms, status))

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._started_at = time.time()

    def snapshot(self, *, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        cutoff = now - self._window
        with self._lock:
            if any(sample.at < cutoff for sample in self._samples):
                kept = [sample for sample in self._samples if sample.at >= cutoff]
                self._samples.clear()
                self._samples.extend(kept)
            samples = list(self._samples)
            observed_seconds = min(self._window, max(0.0, now - self._started_at))
        total = len(samples)
        errors = sum(1 for sample in samples if sample.status >= 500)
        client_errors = sum(1 for sample in samples if 400 <= sample.status < 500)
        latencies = sorted(sample.latency_ms for sample in samples)
        return {
            "window_seconds": self._window,
            "observed_seconds": round(observed_seconds),
            "request_count": total,
            "server_error_count": errors,
            "client_error_count": client_errors,
            # None, not 0, when nothing was served: "no data" is not "no errors".
            "error_rate_percent": round(errors * 100 / total, 2) if total else None,
            "average_latency_ms": round(sum(latencies) / total, 1) if total else None,
            "p95_latency_ms": round(_percentile(latencies, 95), 1) if total else None,
            "max_latency_ms": round(latencies[-1], 1) if total else None,
        }


def _percentile(sorted_values: list[float], percent: float) -> float:
    rank = max(1, math.ceil(percent / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


request_metrics = RequestMetrics()


class RequestMetricsMiddleware:
    """Pure ASGI middleware: time to the response headers, so a long-lived stream
    (workflow progress, chat) is not counted as one very slow request."""

    def __init__(self, app, metrics: RequestMetrics | None = None) -> None:
        self.app = app
        self.metrics = metrics or request_metrics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith(IGNORED_PATH_PREFIXES):
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        recorded = False

        async def send_wrapper(message):
            nonlocal recorded
            if message["type"] == "http.response.start" and not recorded:
                recorded = True
                self.metrics.record((time.perf_counter() - started) * 1000, int(message["status"]))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if not recorded:
                recorded = True
                self.metrics.record((time.perf_counter() - started) * 1000, 500)
            raise
