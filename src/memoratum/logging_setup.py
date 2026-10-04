# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Structured logging to stdout, standard library only.

Scope identifiers only: container tag, project id, job id, event name, outcome.
Never memory content, facts, secrets, API keys, or raw query strings -- a log
aggregator is a much wider blast radius than the database it copies.

JSON is the default because that is what a container log driver expects, and it
requires no agent to be installed.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

LOGGER_NAME = "memoratum"

# Keys that must never reach a log line, whatever a caller passes.
_FORBIDDEN_KEYS = frozenset(
    {
        "text",
        "memory",
        "content",
        "fact",
        "payload",
        "body",
        "secret",
        "token",
        "api_key",
        "authorization",
        "key",
        "password",
        "signature",
        "query_string",
    }
)

_MAX_VALUE = 512


class JsonFormatter(logging.Formatter):
    """Render one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key in _FORBIDDEN_KEYS:
                    continue
                payload[key] = _scrub(value)
        if record.exc_info:
            payload["error"] = record.exc_info[0].__name__ if record.exc_info[0] else "error"
        return json.dumps(payload, separators=(",", ":"), default=str)


class TextFormatter(logging.Formatter):
    """Human-readable single line, for local development."""

    def format(self, record: logging.LogRecord) -> str:
        extra = getattr(record, "fields", None)
        suffix = ""
        if isinstance(extra, dict):
            shown = {k: _scrub(v) for k, v in extra.items() if k not in _FORBIDDEN_KEYS}
            if shown:
                suffix = " " + " ".join(f"{k}={v}" for k, v in sorted(shown.items()))
        return f"{record.levelname.lower():<7} {record.getMessage()}{suffix}"


def _scrub(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_VALUE:
        return value[:_MAX_VALUE] + "...[truncated]"
    return value


def configure(settings: Any = None) -> logging.Logger:
    """Install the memoratum logger. Idempotent."""
    level_name = os.environ.get(
        "MEMORATUM_LOG_LEVEL", getattr(settings, "log_level", None) or "INFO"
    )
    fmt = os.environ.get("MEMORATUM_LOG_FORMAT", getattr(settings, "log_format", None) or "json")

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level_name).upper(), logging.INFO))
    # Keep records out of the root logger: uvicorn configures its own handlers and
    # we do not want duplicates on stdout.
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    logger.addHandler(handler)
    return logger


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured event. Forbidden keys are dropped, not just redacted."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        configure()
    logger.log(level, event, extra={"fields": fields})


def reaped(count: int, kinds: dict[str, int] | None = None) -> None:
    log_event("worker.jobs_reaped", count=count, kinds=kinds or {})


def request_completed(method: str, route: str, status: int, duration_ms: float) -> None:
    log_event(
        "http.request",
        method=method,
        route=route,
        status=status,
        duration_ms=round(duration_ms, 2),
    )


def iteration_failed(worker_id: str, error: str, retry_in: float) -> None:
    log_event(
        "worker.iteration_failed",
        level=logging.WARNING,
        worker_id=worker_id,
        error_type=error.split(":", 1)[0][:64],
        retry_in=retry_in,
    )


def monotonic_ms() -> float:
    return time.monotonic() * 1000.0
