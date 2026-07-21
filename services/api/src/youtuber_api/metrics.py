from __future__ import annotations

import time
from collections import Counter
from threading import Lock

from fastapi import Request, Response


_lock = Lock()
_requests: Counter[tuple[str, str, int]] = Counter()
_durations: Counter[tuple[str, str]] = Counter()
_duration_sum: Counter[tuple[str, str]] = Counter()


def observe_request(method: str, route: str, status: int, duration_seconds: float) -> None:
    with _lock:
        _requests[(method, route, status)] += 1
        _durations[(method, route)] += 1
        _duration_sum[(method, route)] += duration_seconds


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics() -> str:
    lines = [
        "# HELP youtuber_http_requests_total HTTP requests completed by route and status.",
        "# TYPE youtuber_http_requests_total counter",
    ]
    with _lock:
        requests = dict(_requests)
        duration_counts = dict(_durations)
        duration_sums = dict(_duration_sum)
    for (method, route, status), count in sorted(requests.items()):
        lines.append(
            f'youtuber_http_requests_total{{method="{_escape(method)}",route="{_escape(route)}",status="{status}"}} {count}'
        )
    lines.extend([
        "# HELP youtuber_http_request_duration_seconds Request latency by route.",
        "# TYPE youtuber_http_request_duration_seconds summary",
    ])
    for key, count in sorted(duration_counts.items()):
        method, route = key
        labels = f'method="{_escape(method)}",route="{_escape(route)}"'
        lines.append(f"youtuber_http_request_duration_seconds_count{{{labels}}} {count}")
        lines.append(f"youtuber_http_request_duration_seconds_sum{{{labels}}} {duration_sums[key]:.9f}")
    lines.extend([
        "# HELP youtuber_build_info Static application build information.",
        "# TYPE youtuber_build_info gauge",
        'youtuber_build_info{service="api",version="0.1.0",increment="5"} 1',
    ])
    return "\n".join(lines) + "\n"


async def metrics_response(_: Request) -> Response:
    return Response(render_metrics(), media_type="text/plain; version=0.0.4; charset=utf-8")


def monotonic() -> float:
    return time.monotonic()
