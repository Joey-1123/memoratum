# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local metrics registry emitting Prometheus text exposition format.

Standard library only. This is an inbound pull endpoint: the server never dials
out, so it is not external telemetry and does not conflict with the project's
no-egress rule or ``scripts/check_no_telemetry.py``.

Design constraints that matter:

* **Route labels use the registered path template**, never the concrete path. A
  concrete path embeds memory and document ids, which both explodes cardinality
  and leaks identifiers into a metrics store.
* **Tenant identifiers are never labels.** ``container_tag``/``org_id``/
  ``project_id`` are unbounded by construction and multiply by bucket count.
  Per-tenant accounting already lives at ``/v4/usage``.
* All mutation is guarded by a lock: uvicorn runs sync endpoints in a threadpool
  and async ones on the event loop, so an unguarded dict increment is a real race.
* Counters carry the ``_total`` suffix and ``# TYPE`` is always emitted, because
  omitting it leaves a series untyped and therefore unrateable by a scraper.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable
from typing import Any

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

UNMATCHED = "__unmatched__"

_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Explicit (le_string, le_float) pairs. Computing these as floats would render
# 1e-05 and make the exposition text unstable between runs.
DEFAULT_BUCKETS: tuple[tuple[str, float], ...] = (
    ("0.005", 0.005),
    ("0.01", 0.01),
    ("0.025", 0.025),
    ("0.05", 0.05),
    ("0.1", 0.1),
    ("0.25", 0.25),
    ("0.5", 0.5),
    ("1.0", 1.0),
    ("2.5", 2.5),
    ("5.0", 5.0),
    ("10.0", 10.0),
    ("+Inf", float("inf")),
)


def escape_label_value(value: str) -> str:
    """Escape a label value: backslash, double quote, newline. Nothing else."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def escape_help(value: str) -> str:
    """Escape HELP text: backslash and newline, but NOT the double quote.

    The asymmetry is the classic hand-rolled bug; a HELP line containing a quote is
    perfectly legal and escaping it produces output no scraper expects.
    """
    return value.replace("\\", "\\\\").replace("\n", "\\n")


class Metric:
    def __init__(self, name: str, kind: str, help_text: str) -> None:
        if not _NAME_RE.match(name):
            raise ValueError(f"invalid metric name: {name!r}")
        self.name = name
        self.kind = kind
        self.help = help_text
        self.values: dict[tuple[tuple[str, str], ...], float] = {}
        self.buckets: tuple[tuple[str, float], ...] = ()
        self.counts: dict[tuple[tuple[str, str], ...], int] = {}
        self.sums: dict[tuple[tuple[str, str], ...], float] = {}

    def _key(self, labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
        for key in labels:
            if not _LABEL_NAME_RE.match(key):
                raise ValueError(f"invalid label name: {key!r}")
        return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


class Registry:
    """Thread-safe metric registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, Metric] = {}
        self._enabled = True
        self._build_info: Metric | None = None

    # -- registration ----------------------------------------------------
    def counter(self, name: str, help_text: str) -> Metric:
        with self._lock:
            return self._register(name, "counter", help_text)

    def gauge(self, name: str, help_text: str) -> Metric:
        with self._lock:
            return self._register(name, "gauge", help_text)

    def histogram(self, name: str, help_text: str) -> Metric:
        with self._lock:
            metric = self._register(name, "histogram", help_text)
            metric.buckets = DEFAULT_BUCKETS
            return metric

    def _register(self, name: str, kind: str, help_text: str) -> Metric:
        existing = self._metrics.get(name)
        if existing is not None:
            return existing
        metric = Metric(name, kind, help_text)
        self._metrics[name] = metric
        return metric

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_build_info(self, version: str) -> None:
        with self._lock:
            metric = self._register("memoratum_build_info", "gauge", "Build information.")
            metric.values[metric._key({"version": version})] = 1

    # -- recording -------------------------------------------------------
    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        if not self._enabled:
            return
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                return
            key = metric._key(labels)
            metric.values[key] = metric.values.get(key, 0.0) + value

    def set(self, name: str, value: float, **labels: str) -> None:
        if not self._enabled:
            return
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                return
            metric.values[metric._key(labels)] = float(value)

    def observe(self, name: str, value: float, **labels: str) -> None:
        if not self._enabled:
            return
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None or not metric.buckets:
                return
            key = metric._key(labels)
            metric.counts[key] = metric.counts.get(key, 0) + 1
            metric.sums[key] = metric.sums.get(key, 0.0) + value
            current = metric.values.setdefault(key, 0.0)
            for _le, bound in metric.buckets:
                if value <= bound:
                    current += 1
            metric.values[key] = current

    # -- rendering -------------------------------------------------------
    def render(self) -> str:
        with self._lock:
            lines: list[str] = []
            for name in sorted(self._metrics):
                metric = self._metrics[name]
                lines.append(f"# HELP {name} {escape_help(metric.help)}")
                lines.append(f"# TYPE {name} {metric.kind}")
                if metric.kind == "histogram":
                    lines.extend(self._render_histogram(metric))
                else:
                    for key, value in sorted(metric.values.items()):
                        lines.append(f"{_sample(name, key, value)}")
            lines.append("")
            return "\n".join(lines)

    def _render_histogram(self, metric: Metric) -> Iterable[str]:
        """Emit cumulative ``_bucket`` series, then ``_sum`` and ``_count``.

        Buckets are cumulative by definition: each ``le`` reports how many
        observations were less than or equal to it, which is what
        ``histogram_quantile`` expects when it works over bucket rates.
        """
        keys = sorted(set(metric.values) | set(metric.counts))
        for key in keys:
            cumulative = metric.values.get(key, 0.0)
            for le, _bound in metric.buckets:
                yield (f"{metric.name}_bucket{_labels_block(key, le=le)} {_format(cumulative)}")
            yield f"{_sample(f'{metric.name}_sum', key, metric.sums.get(key, 0.0))}"
            yield f"{_sample(f'{metric.name}_count', key, metric.counts.get(key, 0))}"


def _labels_block(key: tuple[tuple[str, str], ...], *, le: str | None = None) -> str:
    """Render a complete ``{...}`` label block, or '' when there are no labels."""
    parts: list[str] = []
    if le is not None:
        parts.append(f'le="{escape_label_value(le)}"')
    parts.extend(f'{name}="{escape_label_value(value)}"' for name, value in key)
    if not parts:
        return ""
    return "{" + ",".join(parts) + "}"


def _sample(name: str, key: tuple[tuple[str, str], ...], value: float) -> str:
    return f"{name}{_labels_block(key)} {_format(value)}"


def _format(value: float) -> str:
    if value == float("inf"):
        return "+Inf"
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return str(int(value))
    return repr(value)


# -- the concrete metric set -------------------------------------------------

REGISTRY = Registry()

HTTP_REQUESTS = REGISTRY.counter("memoratum_http_requests_total", "Total HTTP requests handled.")
HTTP_DURATION = REGISTRY.histogram(
    "memoratum_http_request_duration_seconds", "HTTP request latency in seconds."
)
JOBS_DEPTH = REGISTRY.gauge("memoratum_jobs_queue_depth", "Jobs currently in each status.")
JOBS_AGE = REGISTRY.gauge(
    "memoratum_jobs_oldest_queued_age_seconds",
    "Age in seconds of the oldest queued job.",
)
JOBS_REAPED = REGISTRY.counter("memoratum_jobs_reaped_total", "Leases recovered by the reaper.")
WEBHOOK_DELIVERIES = REGISTRY.counter(
    "memoratum_webhook_deliveries_total", "Webhook delivery outcomes."
)
DB_WRITE_CONFLICTS = REGISTRY.counter(
    "memoratum_db_write_conflicts_total", "Writes that waited on the SQLite write lock."
)
REGISTRY.set_build_info("0.35.0")

# Paths excluded from their own instrumentation, matching the existing /health
# exemption in the rate-limit middleware: a scrape must not inflate the series it
# reports.
SELF_EXEMPT_PATHS = frozenset({"/metrics", "/health", "/health/live", "/health/ready"})


def record_request(method: str, route: str, status: int, duration: float) -> None:
    """Record one request. ``route`` must already be a path template."""
    if not REGISTRY.enabled:
        return
    template = route or UNMATCHED
    REGISTRY.inc(
        "memoratum_http_requests_total", 1, method=method, route=template, status=str(status)
    )
    REGISTRY.observe(
        "memoratum_http_request_duration_seconds", duration, method=method, route=template
    )


def record_job_depth(by_status: dict[str, int], oldest_age: float) -> None:
    """Set queue gauges from one indexed aggregate."""
    if not REGISTRY.enabled:
        return
    for status, count in by_status.items():
        REGISTRY.set("memoratum_jobs_queue_depth", count, status=status)
    REGISTRY.set("memoratum_jobs_oldest_queued_age_seconds", max(0.0, oldest_age))


def render() -> str:
    return REGISTRY.render()


def now() -> float:
    return time.time()


def as_dict() -> dict[str, Any]:
    """Snapshot for debugging and tests."""
    with REGISTRY._lock:
        return {
            name: {"kind": metric.kind, "series": len(metric.values)}
            for name, metric in REGISTRY._metrics.items()
        }
