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

import json
import os
import platform
import sqlite3
import statistics
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

# PREFILTER_MIN_CANDIDATES. A corpus ladder that crosses this threshold measures an
# ALGORITHM SWITCH rather than a size change, which already produced one false
# result in this repo (L7).
PREFILTER_MIN_CANDIDATES = 512


class ManifestError(ValueError):
    """Raised when a manifest violates a hard requirement (FR-008, invariant B1)."""


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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fact_row_text(row: sqlite3.Row) -> str:
    """The fact leg can never pass a substring test against raw corpus text.

    ``search._fact_text`` renders ``"{subject} {predicate} {object}"``, so grounding
    must use that same construction rule per kind rather than one global rule (G2).
    """
    return f"{row['subject']} {row['predicate']} {row['object']}"


def attribute_hit(conn: sqlite3.Connection, hit: dict[str, Any], *, rank: int) -> Attribution:
    """Resolve one hit id to its source row and owning scope.

    Unresolvable ids are reported as ``hit_kind == "unresolvable"`` rather than
    skipped: an id with no derivable row is a failure, not a pass (A1).
    """
    hit_id = str(hit.get("id", ""))

    if hit_id.startswith("chunk_"):
        try:
            chunk_id = int(hit_id.split("_", 1)[1])
        except (IndexError, ValueError):
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
            "SELECT document_id, project_id, text AS row_text FROM memories WHERE id = ?",
            (hit_id,),
        ).fetchone()
        if row is None:
            return _unresolvable(hit_id, rank)
        return Attribution(
            hit_id=hit_id,
            hit_kind="memory",
            document_id=row["document_id"],
            originating_project_id=row["project_id"],
            originating_org_id=None,
            rank=rank,
            row_text=row["row_text"],
        )

    if hit_id.startswith("fact_"):
        row = conn.execute(
            "SELECT document_id, project_id, subject, predicate, object FROM facts WHERE id = ?",
            (hit_id,),
        ).fetchone()
        if row is None:
            return _unresolvable(hit_id, rank)
        return Attribution(
            hit_id=hit_id,
            hit_kind="fact",
            document_id=row["document_id"],
            originating_project_id=row["project_id"],
            originating_org_id=None,
            rank=rank,
            row_text=_fact_row_text(row),
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
    """Attribute every hit, preserving rank order."""
    return [attribute_hit(conn, hit, rank=rank) for rank, hit in enumerate(hits)]


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def collect_hardware() -> dict[str, Any]:
    """Hardware for the manifest. Required for any latency figure (FR-008)."""
    return {
        "cpu": platform.processor() or platform.machine() or "unknown",
        "cores": os.cpu_count() or 1,
        "ram_mb": _total_ram_mb(),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def _total_ram_mb() -> int:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return 0
    return int(pages * page_size / (1024 * 1024))


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
    """Assemble the extended manifest, preserving the seven original keys."""
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
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Enforce hard manifest requirements.

    Raises :class:`ManifestError` when a latency block lacks hardware, since absolute
    milliseconds without hardware are meaningless (B1, FR-008).
    """
    for key in ORIGINAL_MANIFEST_KEYS:
        if key not in manifest:
            raise ManifestError(f"manifest is missing required key {key!r}")

    latency = manifest.get("latency_config")
    if latency is not None:
        if not latency.get("hardware"):
            raise ManifestError(
                "latency_config requires hardware; absolute milliseconds are "
                "hardware-bound (FR-008)"
            )
        samples = latency.get("samples")
        if isinstance(samples, int) and samples < 20:
            raise ManifestError(
                f"latency_config.samples={samples} is below the floor of 20; at n=5 a "
                "1.0x gate needs a +69% band and cannot detect a 70% regression (L2)"
            )
        ladder = latency.get("ladder") or []
        if len(ladder) < 3:
            raise ManifestError(
                f"latency_config.ladder has {len(ladder)} sizes; FR-004 requires at least 3"
            )

    scope = manifest.get("scope_config")
    if scope is not None and len(scope.get("projects") or []) < 2:
        raise ManifestError("scope_config requires at least 2 projects to detect a leak")

    cost = manifest.get("cost_config")
    if cost is not None:
        if cost.get("unit") == "bytes":
            raise ManifestError(
                "cost_config.unit 'bytes' is not a valid reported unit: the corpus is "
                "ASCII so bytes carry no information, and on non-ASCII they inflate "
                "1.1-3x invisibly (C1)"
            )
        if not cost.get("chars_per_ws_token"):
            raise ManifestError(
                "cost_config.chars_per_ws_token must be measured and recorded, never hardcoded (C2)"
            )


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #


def sample_ms(fn: Callable[[], Any], *, samples: int = 30, warmup: int = 5) -> list[float]:
    """Time ``fn`` and return RAW millisecond samples.

    Warmup iterations are discarded first, otherwise the initial call pays FTS5
    tokenizer setup and statement compilation (L4). Raw samples are returned rather
    than a summary so readers can aggregate then take the percentile: p95-of-p95 is
    not a p95, and single-sample relative MAD is 5-16% with max/median 2.11x, so the
    mean is never usable (L3).
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
    """Cost per chunk -- the hardware-independent gate input (L1).

    Sub-linear growth shows as a *falling* number; a regression to O(n) shows as a
    *rising* one. That direction is meaningful in a way absolute ms is not.
    """
    if median_ms_value is None or chunks <= 0:
        return None
    return (median_ms_value * 1000.0) / chunks


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #

PASS = "pass"
FAIL = "fail"


@dataclass
class Gate:
    """Collects checks and derives an overall status.

    A figure that could not be recorded is ``None`` and ``None`` **fails**: a skipped
    metric must never read as a pass (M3).
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
        if value is None or bound is None:
            status = FAIL
            detail = reason or "figure or bound is null; a missing metric never passes"
        elif comparison == ">":
            status = FAIL if value > bound else PASS
            detail = reason
        elif comparison == ">=":
            status = FAIL if value >= bound else PASS
            detail = reason
        elif comparison == "<":
            status = FAIL if value < bound else PASS
            detail = reason
        elif comparison == "<=":
            status = FAIL if value <= bound else PASS
            detail = reason
        else:
            raise ValueError(f"unknown comparison {comparison!r}")
        entry: dict[str, Any] = {"name": name, "status": status, "value": value, "bound": bound}
        if phase is not None:
            entry["phase"] = phase
        if delta is not None:
            entry["delta"] = delta
        if detail:
            entry["reason"] = detail
        self.checks.append(entry)

    @property
    def status(self) -> str:
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


def load_baseline(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a usable baseline, or ``None`` when it is missing or invalid.

    A latency row without hardware is invalid and yields ``None``, which then fails
    the gate -- an unmeasurable baseline must never silently pass (B1, B3).
    """
    if not row:
        return None
    if row.get("kind") == "latency" and not row.get("hardware"):
        return None
    if row.get("value") is None:
        return None
    return row


def compare_to_baseline(
    gate: Gate, *, name: str, value: float | None, baseline: dict[str, Any] | None
) -> None:
    """Add a baseline comparison check, reporting the delta on failure (SC-005)."""
    usable = load_baseline(baseline)
    if usable is None:
        gate.check(name, value=value, bound=None, comparison=">", reason="no usable baseline")
        return
    bound = float(usable["value"]) * (1.0 + float(usable.get("tolerance", 0.0)))
    delta = None if value is None else float(value) - float(usable["value"])
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
            "container_tag": container_tag_of(self),
            "projects": list(self.projects),
            "expected_documents": self.expected_documents,
        }


def container_tag_of(corpus: ScopedCorpus) -> str:
    return corpus.container_tag


def _doc_body(project: str, n: int) -> str:
    # Lexically OVERLAPPING content on purpose: if project B's text does not match
    # the query, its exclusion proves nothing.
    return (
        f"## Notes {n}\n\n"
        f"The quarterly budget review for {project} is scheduled for tuesday. "
        f"Line item {n} covers travel and lodging."
    )


def build_scoped_corpus(
    conn: sqlite3.Connection,
    *,
    projects: Sequence[str],
    docs_per_project: int,
    container_tag: str = "bench",
    ingest: bool = True,
) -> ScopedCorpus:
    """Build a multi-project corpus and assert the expected per-project counts.

    ``custom_id`` is deliberately REUSED across projects to exercise the real
    collision path. The count assertion matters because ``create_document``'s conflict
    lookup includes the scope clauses, so omitting ``project_id`` on one side does not
    collide -- it silently creates a NULL-scope document, growing the unscoped corpus
    and making leak counts look clean. Document count is never inferred from a zero
    leak count (S3).
    """
    project_ids = tuple(projects)
    if len(project_ids) < 2:
        raise ValueError("isolation needs at least 2 projects")

    if ingest:
        from memoratum import db

        for project in project_ids:
            for n in range(docs_per_project):
                db.create_document(
                    conn,
                    container_tag=container_tag,
                    content=_doc_body(project, n),
                    custom_id=f"doc-{n}",
                    project_id=project,
                )
        from memoratum.embeddings import HashEmbedder
        from memoratum.ingest import process_all

        process_all(conn, HashEmbedder(dims=64))

    corpus = ScopedCorpus(
        container_tag=container_tag,
        projects=project_ids,
        expected_documents=len(project_ids) * docs_per_project,
        docs_per_project=docs_per_project,
    )

    actual = conn.execute(
        "SELECT project_id, COUNT(*) c FROM documents WHERE container_tag = ? GROUP BY project_id",
        (container_tag,),
    ).fetchall()
    counts = {row["project_id"]: row["c"] for row in actual}
    for project in project_ids:
        assert counts.get(project) == docs_per_project, (
            f"project {project!r} has {counts.get(project)} documents, expected "
            f"{docs_per_project}; a NULL-scope document was likely created silently (S3)"
        )
    total = conn.execute(
        "SELECT COUNT(*) c FROM documents WHERE container_tag = ?", (container_tag,)
    ).fetchone()["c"]
    assert total == corpus.expected_documents, (
        f"corpus holds {total} documents, expected {corpus.expected_documents}; a "
        "document landed outside its project scope (S3)"
    )
    return corpus


# --------------------------------------------------------------------------- #
# CLI helpers
# --------------------------------------------------------------------------- #

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_SELF_CHECK_FAILED = 2
EXIT_BAD_INPUT = 3


def write_artifacts(
    result: dict[str, Any], report: str, *, out_md: str = "", out_json: str = ""
) -> None:
    """Print the report and optionally persist Markdown and JSON artifacts."""
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
        Path(out_json).write_text(json.dumps(result, indent=2, ensure_ascii=False))


def iter_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def iter_ints(value: str) -> list[int]:
    return [int(part) for part in iter_csv(value)]


def format_table(rows: Iterable[dict[str, Any]], columns: Sequence[str]) -> str:
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")
    return "\n".join(lines)
