# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared plumbing for the evaluation axes: isolation, cost, latency, grounding.

Only genuinely shared concerns live here, so each axis module stays independently
testable and independently runnable. The rules encoded below are measured or
derived, not stylistic -- see ``specs/002-evaluation-harness/research.md`` and
``data-model.md``.

Nothing in this module adds a runtime dependency (Principle V) and nothing here
changes the ``search()`` response shape: scope is *derived* by re-resolving hit ids,
never returned.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import platform
import shlex
import sqlite3
import statistics
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Schema bump signals the extended manifest shape. The seven original keys are
# preserved byte-identical so older committed results stay readable (M1).
MANIFEST_SCHEMA = "longmemeval-scoped-v2"

ORIGINAL_MANIFEST_KEYS = (
    "schema",
    "seed",
    "requested_n",
    "n",
    "ks",
    "modes",
    "embedder",
    "vector_store",
    "data_sha256",
)

# Cost units the contract permits. An allowlist, not a denylist: "bytes" is excluded
# because the corpus is ASCII so bytes carry no information, and on non-ASCII they
# inflate 1.1-3x with nothing visible in the report (C1). An unrecognised string is
# just as wrong as "bytes", so unknown units are rejected too.
COST_UNITS = frozenset({"chars", "ws_tokens"})

# Figures whose meaning is hardware-bound, so a baseline for them is invalid without
# recorded hardware (B1, FR-008). Cost in characters is portable and needs none.
#:
# The per-phase names matter as much as the bare ones: the latency axis keys its baselines
# "<phase>_median_ms" (retrieve_median_ms, index_median_ms, ...), and listing only
# "median_ms" let every one of them bypass the hardware requirement entirely.
HARDWARE_BOUND_FIGURES = frozenset(
    {
        "median_ms",
        "us_per_chunk",
        "us_per_chunk_ratio",
        "ingest_median_ms",
        "embed_median_ms",
        "index_median_ms",
        "retrieve_median_ms",
    }
)

HARDWARE_KEYS = ("cpu", "cores", "ram_mb", "python", "platform")

#: Minimum samples per timed phase.
#:
#: Raised from 20 after measuring the gate against itself. Ten identical runs at 30 samples
#: produced 400-chunk retrieve medians spanning 12.28-26.45 ms (2.15x) on unmodified code,
#: and the per-chunk ratio crossed its own bound in 2 runs of 10. At 120 samples the ms
#: bound was exceeded 0 times in 6 and at 400 samples the per-chunk spread fell to 1.13x.
#: The sample count has to put the noise floor below the gate band, or the band is
#: measuring the machine.
MIN_LATENCY_SAMPLES = 120

# Per-kind grounding rules. One global rule is invalid: the fact leg renders
# "subject predicate object" and would otherwise score 0.0 for identical evidence
# (G2). These are the keys ``contracts/eval-axes-v1.schema.json`` requires.
GROUNDING_RULE_BY_KIND = {
    "chunk": "chunks.text",
    "memory": "memories.text",
    "fact": "subject predicate object",
}
GROUNDING_NORMALIZATION = "nfkc+whitespace+casefold"


class ManifestError(ValueError):
    """Raised when a manifest violates a hard requirement (FR-008, invariant B1)."""


class CorpusError(ManifestError):
    """Raised when a built corpus does not match its declared shape (invariant S3).

    Deliberately an exception rather than a bare ``assert``: ``python -O`` strips
    asserts, and the failure S3 guards is a *silent* extra document, which is exactly
    the defect that would survive an optimised run unnoticed.
    """


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Attribution:
    """One search hit resolved to its owning row and scope.

    Scope comes from the database, never from text: byte-identical content in two
    projects produces two distinct rows with distinct ids, so text cannot attribute
    scope (A2).
    """

    hit_id: str
    hit_kind: str  # chunk | memory | fact | unresolvable
    document_id: str | None
    originating_project_id: str | None
    originating_org_id: str | None
    rank: int
    row_text: str | None

    def as_dict(self, *, omit_text: bool = False) -> dict[str, Any]:
        """Serialise for the report.

        ``omit_text`` drops ``row_text``, which the grounding axis needs but a scope
        report does not: at ~160 hits the corpus text dominates the artifact and buries
        the metrics an operator is reading.
        """
        payload = asdict(self)
        if omit_text:
            payload.pop("row_text", None)
        return payload


def attribute_hit(conn: sqlite3.Connection, hit: dict[str, Any], *, rank: int) -> Attribution:
    """Resolve one hit id to its source row and owning scope.

    Facts never arrive with a ``fact_`` id: ``facts.add_fact`` mints a ``mem_`` row
    via ``db.ensure_fact_memory`` (``mem_<uuid>``), and migrated rows use
    ``'mem_' || f.id``. A fact-derived hit is therefore a ``mem_`` row that carries a
    ``fact_id``, and it is classified ``fact`` here so the grounding axis applies the
    fact rule rather than the memory rule (G2).

    Unresolvable ids are reported as ``hit_kind == "unresolvable"`` rather than
    skipped: an id with no derivable row is a failure, not a pass (A1).
    """
    hit_id = str(hit.get("id", ""))

    if hit_id.startswith("chunk_"):
        try:
            chunk_id = int(hit_id.split("_", 1)[1])
        except ValueError:
            return _unresolvable(hit_id, rank)
        row = conn.execute(
            "SELECT d.id AS document_id, d.project_id, d.org_id, c.text AS row_text"
            " FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return _unresolvable(hit_id, rank)
        return Attribution(
            hit_id=hit_id,
            hit_kind="chunk",
            document_id=row["document_id"],
            originating_project_id=row["project_id"],
            originating_org_id=row["org_id"],
            rank=rank,
            row_text=row["row_text"],
        )

    if hit_id.startswith("mem_"):
        row = conn.execute(
            "SELECT m.document_id, m.project_id, m.org_id, m.text AS row_text,"
            " f.subject, f.predicate, f.object"
            " FROM memories m LEFT JOIN facts f ON f.id = m.fact_id"
            " WHERE m.id = ?",
            (hit_id,),
        ).fetchone()
        if row is None:
            return _unresolvable(hit_id, rank)
        is_fact = row["subject"] is not None
        # A fact is grounded against its own construction rule, not the memory text.
        row_text = (
            f"{row['subject']} {row['predicate']} {row['object']}" if is_fact else row["row_text"]
        )
        return Attribution(
            hit_id=hit_id,
            hit_kind="fact" if is_fact else "memory",
            document_id=row["document_id"],
            originating_project_id=row["project_id"],
            originating_org_id=row["org_id"],
            rank=rank,
            row_text=row_text,
        )

    return _unresolvable(hit_id, rank)


def _unresolvable(hit_id: str, rank: int) -> Attribution:
    return Attribution(
        hit_id=hit_id,
        hit_kind="unresolvable",
        document_id=None,
        originating_project_id=None,
        originating_org_id=None,
        rank=rank,
        row_text=None,
    )


def attribute_hits(conn: sqlite3.Connection, hits: Sequence[dict[str, Any]]) -> list[Attribution]:
    """Attribute every hit, preserving rank order.

    No hit is ever omitted: an unresolvable id still appears, so the caller can count
    it as a failure (A1).
    """
    return [attribute_hit(conn, hit, rank=rank) for rank, hit in enumerate(hits)]


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def embedder_label(embedder: Any) -> str:
    """Canonical embedder label, e.g. ``"HashEmbedder:64"``.

    Lives here rather than in ``eval_longmemeval`` so every axis formats it the same
    way and a manifest cannot claim an embedder that was not actually used.
    """
    return f"{type(embedder).__name__}:{getattr(embedder, 'dims', 0)}"


def _cpu_model() -> str | None:
    """Best-effort CPU model name. ``None`` when genuinely unknowable, never a guess."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("model name"):
                    value = line.split(":", 1)[1].strip()
                    if value:
                        return value
    except OSError:
        pass
    processor = platform.processor().strip()
    return processor or None


def _total_ram_mb() -> int | None:
    """Physical RAM in MiB, or ``None`` when it cannot be determined.

    ``None`` rather than ``0``: a zero would read as a measurement and satisfy a
    truthiness check, which is the None-vs-0 confusion M3 exists to prevent.
    """
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return int(pages * page_size / (1024 * 1024))


def collect_hardware() -> dict[str, Any]:
    """Hardware for the manifest. Required for any hardware-bound figure (FR-008)."""
    return {
        "cpu": _cpu_model(),
        "cores": os.cpu_count() or 1,
        "ram_mb": _total_ram_mb(),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def build_manifest(
    *,
    seed: int,
    requested_n: int,
    n: int,
    ks: Sequence[int],
    modes: Sequence[str],
    embedder: str,
    vector_store: str,
    data_sha256: str,
    scope_config: dict[str, Any] | None = None,
    cost_config: dict[str, Any] | None = None,
    latency_config: dict[str, Any] | None = None,
    grounding_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble and **validate** the extended manifest.

    Validation runs here, not as an optional extra step: a caller that forgets to call
    it would otherwise write an artifact claiming bytes while gating on characters,
    which is C1's exact failure with nothing objecting.
    """
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "seed": seed,
        "requested_n": requested_n,
        "n": n,
        "ks": list(ks),
        "modes": list(modes),
        "embedder": embedder,
        "vector_store": vector_store,
        "data_sha256": data_sha256,
    }
    for key, value in (
        ("scope_config", scope_config),
        ("cost_config", cost_config),
        ("latency_config", latency_config),
        ("grounding_config", grounding_config),
    ):
        if value is not None:
            manifest[key] = value
    validate_manifest(manifest)
    return manifest


def _require_keys(block: dict[str, Any], keys: Iterable[str], label: str) -> None:
    missing = sorted(set(keys) - set(block))
    if missing:
        raise ManifestError(f"{label} is missing required keys: {missing}")


def _hardware_is_usable(hardware: Any) -> bool:
    """True only for hardware that actually identifies a machine (B1)."""
    if isinstance(hardware, str):
        # A Markdown cell may literally say "n/a" or "unknown".
        return hardware.strip().lower() not in {"", "n/a", "none", "unknown", "-"}
    if isinstance(hardware, dict):
        return all(hardware.get(key) not in (None, "") for key in HARDWARE_KEYS)
    return False


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Enforce hard manifest requirements. Raises :class:`ManifestError`."""
    _require_keys(manifest, ORIGINAL_MANIFEST_KEYS, "manifest")

    ks = manifest["ks"]
    if not ks or any(not isinstance(k, int) or isinstance(k, bool) or k < 1 for k in ks):
        # The contract schema requires ks items >= 1. Validating here means a nonsense
        # `--k -5` cannot write an artifact that fails its own contract (M2).
        raise ManifestError(f"manifest ks must be a non-empty list of ints >= 1, got {ks!r}")

    latency = manifest.get("latency_config")
    if latency is not None:
        _require_keys(latency, ("samples", "warmup", "ladder", "hardware"), "latency_config")
        samples = latency["samples"]
        if not isinstance(samples, int) or isinstance(samples, bool):
            raise ManifestError(
                f"latency_config.samples must be an int, got {type(samples).__name__}; "
                "a missing or mistyped count silently disables the floor"
            )
        if samples < MIN_LATENCY_SAMPLES:
            raise ManifestError(
                f"latency_config.samples={samples} is below the floor of "
                f"{MIN_LATENCY_SAMPLES}. Measured, not assumed: at 30 samples the "
                "400-chunk retrieve median varied 2.15x across identical runs on this "
                "hardware, and the ms smoke bound (11.09 x 1.40 = 15.53 ms) was exceeded "
                "1 run in 6 with no code change. At 120 samples the same bound was "
                "exceeded 0 in 6. A gate whose noise floor is wider than its band is not a "
                "gate -- it just trains operators to ignore it (L2)"
            )
        if not isinstance(latency["warmup"], int) or latency["warmup"] < 1:
            raise ManifestError(
                "latency_config.warmup must be an int >= 1; without discarded warmup the "
                "first call pays FTS5 tokenizer setup (L4)"
            )
        if not isinstance(latency["hardware"], dict):
            raise ManifestError(
                "latency_config.hardware must be a dict of measured values; got "
                f"{type(latency['hardware']).__name__}. Missing or unrecorded hardware "
                "makes a hardware-bound figure meaningless (B1, FR-008)"
            )
        _require_keys(latency["hardware"], HARDWARE_KEYS, "latency_config.hardware")
        if not isinstance(latency["hardware"]["ram_mb"], int) or latency["hardware"]["ram_mb"] < 1:
            raise ManifestError(
                "latency_config.hardware.ram_mb must be an int >= 1; 0 or null means the "
                "figure was not measured (M3)"
            )
        if str(latency["hardware"]["cpu"]).strip().lower() in {"unknown", ""}:
            raise ManifestError(
                "latency_config.hardware.cpu is unknown; a +40% band needs real "
                "hardware context (FR-008)"
            )
        _validate_ladder(latency["ladder"], latency.get("prefilter_min"))

    cost = manifest.get("cost_config")
    if cost is not None:
        unit = cost.get("unit")
        if unit not in COST_UNITS:
            raise ManifestError(
                f"cost_config.unit must be one of {sorted(COST_UNITS)}, got {unit!r}; "
                "bytes and unknown units inflate 1.1-3x on non-ASCII (C1)"
            )
        ratio = cost.get("chars_per_ws_token")
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or ratio <= 0:
            raise ManifestError(
                "cost_config.chars_per_ws_token must be a positive measured number; a "
                "hardcoded constant is a fabricated figure in a committed artifact (C2)"
            )

    scope = manifest.get("scope_config")
    if scope is not None:
        _require_keys(scope, ("container_tag", "projects", "expected_documents"), "scope_config")
        if len(scope["projects"]) < 2:
            raise ManifestError(
                f"scope_config has {len(scope['projects'])} project(s); at least 2 projects "
                "are required because one project cannot leak, which would make the "
                "isolation axis unfalsifiable"
            )

    grounding = manifest.get("grounding_config")
    if grounding is not None:
        _require_keys(grounding, ("normalization", "rule_by_kind"), "grounding_config")
        if grounding["normalization"] != GROUNDING_NORMALIZATION:
            raise ManifestError(
                f"grounding_config.normalization must be {GROUNDING_NORMALIZATION!r}"
            )
        _require_keys(
            grounding["rule_by_kind"], GROUNDING_RULE_BY_KIND, "grounding_config.rule_by_kind"
        )


def _validate_ladder(ladder: Any, prefilter_min: Any) -> None:
    """At least three sizes, and none straddling the prefilter threshold (L7).

    A ladder that crosses the threshold measures an ALGORITHM SWITCH rather than a size
    change -- which already produced one false result in this repo. Either keep every
    size on one side, or pin the threshold and record it (L7, FR-004).
    """
    if not isinstance(ladder, (list, tuple)) or len(ladder) < 3:
        raise ManifestError(
            f"latency_config.ladder has {ladder!r}; FR-004 requires at least 3 corpus sizes"
        )
    if any(not isinstance(size, int) or isinstance(size, bool) or size < 1 for size in ladder):
        raise ManifestError(f"latency_config.ladder sizes must be positive ints, got {ladder!r}")
    if prefilter_min is not None:
        # Threshold pinned and recorded, so straddling measures size, not a switch.
        return
    from memoratum.search import prefilter_min_candidates

    threshold = prefilter_min_candidates()
    ordered = sorted(ladder)
    for lower, upper in itertools.pairwise(ordered):
        if lower < threshold <= upper:
            raise ManifestError(
                f"latency_config.ladder {list(ladder)} straddles the prefilter threshold "
                f"{threshold} between {lower} and {upper}; that step measures an algorithm "
                "switch rather than a size change. Keep every size on one side or set "
                "MEMORATUM_SEARCH_PREFILTER_MIN and record prefilter_min (L7)"
            )


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #


def sample_ms(fn: Callable[[], Any], *, samples: int = 30, warmup: int = 5) -> list[float]:
    """Time ``fn`` and return RAW millisecond samples.

    Warmup runs first and is discarded, otherwise the initial call pays FTS5 tokenizer
    setup and statement compilation (L4). Raw samples are returned rather than a
    summary so readers aggregate then take the percentile: p95-of-p95 is not a p95, and
    single-sample relative MAD is 5-16% with max/median 2.11x, so the mean is never
    usable (L3).
    """
    if samples < 1:
        raise ValueError("samples must be >= 1")
    for _ in range(max(0, warmup)):
        fn()
    timings: list[float] = []
    for _ in range(samples):
        start = time.perf_counter()
        fn()
        timings.append((time.perf_counter() - start) * 1000.0)
    return timings


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Percentile over raw samples. ``None`` for empty input, never 0.0 (M3)."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (pct / 100.0) * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def median(values: Sequence[float]) -> float | None:
    """Median over raw samples. ``None`` for empty input, never 0.0 (M3)."""
    if not values:
        return None
    return float(statistics.median(values))


def us_per_chunk(median_ms_value: float | None, chunks: int) -> float | None:
    """Microseconds per chunk -- the hardware-independent gate input (L1).

    Sub-linear growth shows as a *falling* number; a regression to O(n) shows as a
    *rising* one. That direction is meaningful in a way absolute ms is not, and a
    missing input is ``None`` rather than a fabricated 0.0.
    """
    if median_ms_value is None or chunks is None or chunks <= 0:
        return None
    return (median_ms_value * 1000.0) / chunks


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #

PASS = "pass"
FAIL = "fail"

_COMPARISONS = {
    ">": lambda v, b: v > b,
    ">=": lambda v, b: v >= b,
    "<": lambda v, b: v < b,
    "<=": lambda v, b: v <= b,
}


@dataclass
class Gate:
    """Collects checks and derives an overall status.

    A figure that could not be recorded is ``None`` and ``None`` **fails**: a skipped
    metric must never read as a pass (M3). An *empty* gate also fails, because an axis
    that recorded no checks at all measured nothing.
    """

    checks: list[dict[str, Any]] = field(default_factory=list)

    def check(
        self,
        name: str,
        *,
        value: Any,
        bound: Any,
        comparison: str = ">",
        phase: str | None = None,
        delta: Any = None,
        reason: str | None = None,
    ) -> None:
        if comparison not in _COMPARISONS:
            raise ValueError(
                f"unknown comparison {comparison!r}; expected one of {sorted(_COMPARISONS)}"
            )
        if value is None or bound is None:
            status = FAIL
            detail = reason or "figure or bound is null; a missing metric never passes"
        else:
            status = FAIL if _COMPARISONS[comparison](value, bound) else PASS
            detail = reason
        entry: dict[str, Any] = {
            "name": name,
            "status": status,
            "value": value,
            "bound": bound,
        }
        if phase is not None:
            entry["phase"] = phase
        if delta is not None:
            entry["delta"] = delta
        if detail:
            entry["reason"] = detail
        self.checks.append(entry)

    @property
    def status(self) -> str:
        if not self.checks:
            return FAIL
        return FAIL if any(c["status"] == FAIL for c in self.checks) else PASS

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "checks": list(self.checks)}


def evaluate_gate(gate: Gate) -> dict[str, Any]:
    return gate.as_dict()


# --------------------------------------------------------------------------- #
# Baselines
# --------------------------------------------------------------------------- #


def _parse_number(raw: Any) -> float | None:
    """Parse a baseline cell. ``None`` for anything not a finite number.

    Markdown cells arrive as strings, so a cell reading "see below" must become
    ``None`` -- which fails the gate -- rather than raising and killing the run.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip().replace(",", "")
        if not text or text.lower() in {"n/a", "none", "null", "-", "unknown", "tbd"}:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def load_baseline(
    row: dict[str, Any] | None, *, figure: str | None = None
) -> dict[str, Any] | None:
    """Return a usable baseline, or ``None`` when it is missing or invalid.

    A hardware-bound figure without usable hardware is invalid and yields ``None``,
    which then fails the gate -- an unmeasurable baseline must never silently pass
    (B1, B3). The hardware rule is applied from the *figure name* rather than a
    caller-supplied ``kind`` string, so it cannot be bypassed by omitting a field.
    """
    if not row or not isinstance(row, dict):
        # A baseline is a mapping. A bare number or None is not a baseline, and reaching
        # for .get() on it would raise AttributeError mid-gate rather than failing it.
        return None
    name = figure or str(row.get("axis", "") or "")
    # A row whose own axis disagrees with the requested figure is rejected outright.
    # Comparing a whitespace-token figure against a characters baseline is not a tight
    # gate, it is no gate at all: the units differ by ~6x, so anything would pass.
    declared = str(row.get("axis", "") or "")
    if figure and declared and declared != figure:
        return None
    if name in HARDWARE_BOUND_FIGURES and not _hardware_is_usable(row.get("hardware")):
        return None
    value = _parse_number(row.get("value", row.get("figure")))
    if value is None:
        return None
    # A zero baseline is not a measurement: bound 0 * (1 + tol) is 0, so every
    # non-negative figure would pass. Reject rather than gate against nonsense.
    if value <= 0:
        return None
    tolerance = _parse_number(row.get("tolerance"))
    if tolerance is None or tolerance < 0:
        return None
    return {**row, "value": value, "tolerance": tolerance}


def compare_to_baseline(
    gate: Gate, *, name: str, value: float | None, baseline: dict[str, Any] | None
) -> None:
    """Add a baseline comparison check, reporting the delta on failure (SC-005)."""
    usable = load_baseline(baseline, figure=name)
    if usable is None:
        gate.check(name, value=value, bound=None, comparison=">", reason="no usable baseline")
        return
    bound = usable["value"] * (1.0 + usable["tolerance"])
    delta = None if value is None else float(value) - usable["value"]
    gate.check(name, value=value, bound=bound, comparison=">", delta=delta)


# --------------------------------------------------------------------------- #
# Scoped corpus
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScopedCorpus:
    """Documents attributed to projects, forming the ground truth for isolation."""

    container_tag: str
    projects: tuple[str, ...]
    expected_documents: int
    docs_per_project: int

    def scope_config(self) -> dict[str, Any]:
        return {
            "container_tag": self.container_tag,
            "projects": list(self.projects),
            "expected_documents": self.expected_documents,
        }


def scoped_doc_body(project: str, n: int) -> str:
    """Document body for a project.

    Lexically OVERLAPPING on purpose: every body carries the same query terms and
    differs only in the project name. If project B's text did not match the query, its
    exclusion from a project-A query would prove nothing.
    """
    return (
        f"## Notes {n}\n\n"
        f"The quarterly budget review for {project} is scheduled for tuesday. "
        f"Line item {n} covers travel and lodging."
    )


#: Terms every scoped body must contain, so a query can match all projects at once.
SCOPED_QUERY_TERMS = ("quarterly", "budget", "review", "tuesday")


def build_scoped_corpus(
    conn: sqlite3.Connection,
    *,
    projects: Sequence[str],
    docs_per_project: int,
    container_tag: str = "bench",
    embedder: Any | None = None,
    ingest: bool = True,
) -> ScopedCorpus:
    """Build a multi-project corpus and assert the expected per-project counts.

    ``custom_id`` is deliberately REUSED across projects to exercise the real collision
    path. The count assertion matters because ``create_document``'s conflict lookup
    includes the scope clauses, so omitting ``project_id`` on one side does not collide
    -- it silently creates a NULL-scope document, growing the unscoped corpus and making
    leak counts look clean. Document count is never inferred from a zero leak count (S3).
    """
    project_ids = tuple(projects)
    if len(project_ids) < 2:
        raise CorpusError(
            f"isolation needs at least 2 projects, got {len(project_ids)}; one project cannot leak"
        )
    if docs_per_project < 1:
        raise CorpusError(f"docs_per_project must be >= 1, got {docs_per_project}")

    if ingest:
        from memoratum import db

        for project in project_ids:
            for n in range(docs_per_project):
                db.create_document(
                    conn,
                    container_tag=container_tag,
                    content=scoped_doc_body(project, n),
                    custom_id=f"doc-{n}",
                    project_id=project,
                )
        from memoratum.embeddings import HashEmbedder
        from memoratum.ingest import process_all

        # HashEmbedder only: a remote embedder would put network jitter inside the
        # timed region (L5) and require a provider extra (FR-011).
        process_all(conn, embedder if embedder is not None else HashEmbedder(dims=64))

    corpus = ScopedCorpus(
        container_tag=container_tag,
        projects=project_ids,
        expected_documents=len(project_ids) * docs_per_project,
        docs_per_project=docs_per_project,
    )

    rows = conn.execute(
        "SELECT project_id, COUNT(*) c FROM documents WHERE container_tag = ? GROUP BY project_id",
        (container_tag,),
    ).fetchall()
    counts = {row["project_id"]: row["c"] for row in rows}
    for project in project_ids:
        if counts.get(project) != docs_per_project:
            raise CorpusError(
                f"project {project!r} holds {counts.get(project)} documents, expected "
                f"{docs_per_project}; a NULL-scope document was likely created silently (S3)"
            )
    total = conn.execute(
        "SELECT COUNT(*) c FROM documents WHERE container_tag = ?", (container_tag,)
    ).fetchone()["c"]
    if total != corpus.expected_documents:
        raise CorpusError(
            f"corpus holds {total} documents, expected {corpus.expected_documents}; a "
            "document landed outside its project scope (S3)"
        )
    return corpus


# --------------------------------------------------------------------------- #
# CLI helpers
# --------------------------------------------------------------------------- #

EXIT_OK = 0
EXIT_GATE_FAILED = 1
# Named, because both a broken metric and a missing baseline share it, and "2" reading
# as "bad input" is how a missing baseline ends up reported as a usage error.
EXIT_SELF_CHECK_FAILED = 2
EXIT_BASELINE_MISSING = 2
EXIT_BAD_INPUT = 3

#: The one retrieval budget, shared by every axis that reports a retrieval figure.
#:
#: It is deliberately ``max(2*max(ks), 10)`` and deliberately NOT scaled by
#: chunks-per-document. That inflation existed to feed the *old* ``R@k``, which counted
#: distinct sessions; under the corrected definition ``R@k`` reads the first ``k`` hits,
#: so the budget is merely generous. Re-inflating it now would mask a real cost
#: regression (C5, research.md D7 correction 3).
#:
#: One definition, because cost and recall only share a unit while two independent
#: literals happen to agree -- and nothing would object if they diverged.
RETRIEVAL_BUDGET_FLOOR = 10


def retrieval_budget(ks: Sequence[int]) -> int:
    """Hits to retrieve for a given set of retrieval depths."""
    return max(max(ks, default=0) * 2, RETRIEVAL_BUDGET_FLOOR)


def command(argv: Sequence[str] | None, *, module: str) -> list[str]:
    """The runnable command that regenerates this axis's figures.

    Always ``python -m memoratum.<axis> <flags>``, with the flags taken from ``argv`` when
    given and from ``sys.argv[1:]`` otherwise. Both paths describe the flags that actually
    ran, which is what SC-003 asks for.

    ``sys.executable`` is deliberately **not** used: it is an absolute path into whatever
    virtualenv produced the file, so it does not exist on a reader's machine. The recorded
    line is what an operator would type, not what this process happened to be.
    """
    flags = list(argv) if argv is not None else list(sys.argv[1:])
    return ["python", "-m", module, *flags]


def regeneration_block(argv: Sequence[str] | None) -> str:
    """The fenced block a results file carries so the figure can be regenerated (SC-003).

    A results file that records what the numbers were but not how to get them again is a
    claim rather than a measurement. The command is recorded from ``sys.argv`` rather than
    reconstructed per axis, so it cannot drift from what actually ran — reconstructing it
    would be a second source of truth, and the two would diverge on the first flag change.

    ``argv=None`` yields a block that says so, rather than an empty one: silence would read
    as "no command was needed".
    """
    if argv is None:
        return "## Regenerate this figure\n\nNo regeneration command was recorded for this run.\n"
    quoted = " ".join(shlex.quote(part) for part in argv)
    return f"## Regenerate this figure\n\n```bash\n{quoted}\n```\n"


def write_artifacts(
    result: dict[str, Any],
    report: str,
    *,
    out_md: str = "",
    out_json: str = "",
    argv: Sequence[str] | None = None,
) -> None:
    """Print the report and optionally persist Markdown and JSON artifacts.

    When ``argv`` is supplied the regeneration command is appended, so every emitted
    results file is self-describing (SC-003). Passing it explicitly rather than reading
    ``sys.argv`` here keeps ``main()`` reproducible when called with a synthetic argv by a
    test or a library caller.
    """
    if argv is not None:
        report = report.rstrip("\n") + "\n\n" + regeneration_block(argv)
    print(report)
    if out_md:
        directory = os.path.dirname(out_md)
        if directory:
            os.makedirs(directory, exist_ok=True)
        Path(out_md).write_text(report)
    if out_json:
        directory = os.path.dirname(out_json)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = dict(result)
        if argv is not None:
            payload = {**payload, "command": list(argv)}
        Path(out_json).write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def iter_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def iter_ints(value: str) -> list[int]:
    return [int(part) for part in iter_csv(value)]


def format_table(rows: Iterable[dict[str, Any]], columns: Sequence[str]) -> str:
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")
    return "\n".join(lines)
