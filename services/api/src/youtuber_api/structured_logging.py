from __future__ import annotations

import json
import logging
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace


_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": _correlation_id.get(),
        }
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")
        event = getattr(record, "event_fields", None)
        if isinstance(event, dict):
            payload.update(event)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_logger() -> logging.Logger:
    logger = logging.getLogger("youtuber.request")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def bind_correlation(value: str) -> Token[str]:
    return _correlation_id.set(value)


def reset_correlation(token: Token[str]) -> None:
    _correlation_id.reset(token)
