"""
In-process observability: request counters, latency histograms, event ingestion totals.
Exposed at GET /api/metrics in a Prometheus-compatible text format.

For production scale, replace with opentelemetry-sdk + prometheus_client.
"""
from __future__ import annotations
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List


class _Metrics:
    def __init__(self):
        self._lock = Lock()
        self._request_count:   Dict[str, int]         = defaultdict(int)
        self._request_errors:  Dict[str, int]         = defaultdict(int)
        self._latencies:       Dict[str, List[float]] = defaultdict(list)
        self._events_ingested: int = 0
        self._events_rejected: int = 0
        self._events_duplicate: int = 0
        self._start_time = time.time()

    def record_request(self, endpoint: str, status_code: int, latency_ms: float):
        with self._lock:
            key = f"{endpoint}"
            self._request_count[key] += 1
            if status_code >= 400:
                self._request_errors[key] += 1
            self._latencies[key].append(latency_ms)
            # Keep only last 1000 samples per endpoint to bound memory
            if len(self._latencies[key]) > 1000:
                self._latencies[key] = self._latencies[key][-1000:]

    def record_ingest(self, accepted: int, rejected: int, duplicate: int):
        with self._lock:
            self._events_ingested  += accepted
            self._events_rejected  += rejected
            self._events_duplicate += duplicate

    def _p50(self, values: List[float]) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        return s[len(s) // 2]

    def _p99(self, values: List[float]) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        return s[int(len(s) * 0.99)]

    def to_prometheus_text(self) -> str:
        lines = []
        uptime = round(time.time() - self._start_time, 1)

        lines.append("# HELP store_api_uptime_seconds API uptime in seconds")
        lines.append("# TYPE store_api_uptime_seconds gauge")
        lines.append(f"store_api_uptime_seconds {uptime}")

        lines.append("# HELP store_api_requests_total Total HTTP requests by endpoint")
        lines.append("# TYPE store_api_requests_total counter")
        with self._lock:
            for endpoint, count in self._request_count.items():
                safe = endpoint.replace("/", "_").replace("{", "").replace("}", "").strip("_")
                lines.append(f'store_api_requests_total{{endpoint="{endpoint}"}} {count}')

            lines.append("# HELP store_api_request_errors_total Total HTTP errors by endpoint")
            lines.append("# TYPE store_api_request_errors_total counter")
            for endpoint, count in self._request_errors.items():
                lines.append(f'store_api_request_errors_total{{endpoint="{endpoint}"}} {count}')

            lines.append("# HELP store_api_latency_p50_ms Median request latency ms by endpoint")
            lines.append("# HELP store_api_latency_p99_ms p99 request latency ms by endpoint")
            lines.append("# TYPE store_api_latency_p50_ms gauge")
            lines.append("# TYPE store_api_latency_p99_ms gauge")
            for endpoint, lats in self._latencies.items():
                lines.append(f'store_api_latency_p50_ms{{endpoint="{endpoint}"}} {self._p50(lats):.2f}')
                lines.append(f'store_api_latency_p99_ms{{endpoint="{endpoint}"}} {self._p99(lats):.2f}')

            lines.append("# HELP store_events_ingested_total Total events ingested successfully")
            lines.append("# TYPE store_events_ingested_total counter")
            lines.append(f"store_events_ingested_total {self._events_ingested}")

            lines.append("# HELP store_events_rejected_total Total events rejected (schema/error)")
            lines.append("# TYPE store_events_rejected_total counter")
            lines.append(f"store_events_rejected_total {self._events_rejected}")

            lines.append("# HELP store_events_duplicate_total Total duplicate events deduplicated")
            lines.append("# TYPE store_events_duplicate_total counter")
            lines.append(f"store_events_duplicate_total {self._events_duplicate}")

        return "\n".join(lines) + "\n"

    def to_json(self) -> dict:
        """JSON view of the same metrics for non-Prometheus consumers."""
        with self._lock:
            return {
                "uptime_seconds": round(time.time() - self._start_time, 1),
                "requests": dict(self._request_count),
                "errors":   dict(self._request_errors),
                "latency_p50_ms": {
                    ep: round(self._p50(lats), 2)
                    for ep, lats in self._latencies.items()
                },
                "latency_p99_ms": {
                    ep: round(self._p99(lats), 2)
                    for ep, lats in self._latencies.items()
                },
                "events_ingested":  self._events_ingested,
                "events_rejected":  self._events_rejected,
                "events_duplicate": self._events_duplicate,
            }


# Module-level singleton
metrics = _Metrics()
