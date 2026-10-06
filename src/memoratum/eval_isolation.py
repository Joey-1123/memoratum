# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Scope isolation axis: prove tenant isolation is measured, not assumed.

Why this axis exists: the two worst security findings in feature 001 were a write landing
in the global NULL scope (``Mem0AddIn`` had no ``project_id``) and a typo'd field being
silently accepted (``projct_id`` -> ``201``, ``project_id = None``). A single unscoped
corpus reports **perfect recall** for both, because recall is monotonically indifferent to
where data landed -- so a leakage bug can only *improve* the score. The metric family
that existed could not observe the failure mode that actually occurred.

Design rules that are load-bearing here (see ``research.md`` D1 and ``data-model.md``):

* The gate is the **absolute count** ``leaked_hits == 0``, not a rate. The correct value is
  exactly zero, so there is no tolerance to tune and no false-positive surface (I1).
* ``leak_rate`` and ``query_leak_rate`` are **trends only** and never gate (I3).
* Unscoped queries get their **own bucket**, in neither the gate numerator nor the
  denominator. Every scope filter in this codebase is conditional on ``is not None``, so
  ``project_id=None`` genuinely means "caller requested no scope" (I2).
* An unresolvable hit **cannot be cleared**, so it fails rather than passing (A1).
* Every axis ships a falsifiability self-check: if the detector stops detecting, that is
  worse than a leak, and it exits ``2`` (I4, SC-001).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from memoratum import db as db_module
from memoratum.embeddings import HashEmbedder
from memoratum.eval_axes import (
    EXIT_BAD_INPUT,
    EXIT_GATE_FAILED,
    EXIT_OK,
    EXIT_SELF_CHECK_FAILED,
    FAIL,
    Gate,
    attribute_hits,
    build_manifest,
    build_scoped_corpus,
    command,
    embedder_label,
    iter_ints,
    retrieval_budget,
    write_artifacts,
)
from memoratum.search import search

Query = tuple[Callable[[sqlite3.Connection], list[dict[str, Any]]], str | None]

CORPUS_QUERY = "quarterly budget review"


def default_query(project_id: str | None, *, limit: int, container_tag: str) -> Callable:
    """Build the standard probe: retrieve the shared corpus terms under one scope."""

    def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return search(
            conn,
            HashEmbedder(dims=64),
            CORPUS_QUERY,
            container_tag=container_tag,
            project_id=project_id,
            limit=limit,
        )

    return run


def is_leak(attribution: Any, requested_project_id: str | None) -> bool:
    """Is this hit a leak for the requested scope?

    ``unresolvable`` counts as a leak rather than being skipped: a hit whose origin
    cannot be established has not been shown to be in scope (A1).

    The count and the per-leak report both call this, so the number that fails the gate
    and the records an operator reads can never disagree.
    """
    return attribution.hit_kind == "unresolvable" or (
        attribution.originating_project_id != requested_project_id
    )


def default_detector(attributions: Sequence[Any], requested_project_id: str | None) -> int:
    """Count leaks among the attributions for one query."""
    return sum(1 for a in attributions if is_leak(a, requested_project_id))


def evaluate_isolation(
    conn: sqlite3.Connection,
    *,
    queries: Sequence[Query],
    projects: Sequence[str],
    container_tag: str,
    docs_per_project: int,
    ks: Sequence[int],
    seed: int,
    n: int | None = None,
    detector: Callable[[Sequence[Any], str | None], int] | None = None,
    corpus: Any | None = None,
    include_attribution: bool = False,
) -> dict[str, Any]:
    """Measure cross-scope leakage over a set of scoped and unscoped queries.

    ``corpus`` may be supplied to reuse an existing corpus; otherwise one is built and
    its per-project counts asserted (S3).

    ``include_attribution`` adds every attributed hit to the report. It is off by default
    because it is large, and automatically forced on whenever a leak or an unresolvable
    hit is present -- the cases where an operator actually needs it.
    """
    if len(projects) < 2:
        raise ValueError(
            f"isolation needs at least 2 projects, got {len(projects)}; one project "
            "cannot leak, which would make the axis unfalsifiable"
        )
    # Resolved at call time, not bound as a default argument, so a caller that swaps the
    # module's detector is actually honoured. A default captured at def time would make
    # the self-check unfalsifiable-by-substitution.
    detect = detector if detector is not None else default_detector
    if corpus is None:
        corpus = build_scoped_corpus(
            conn, projects=projects, docs_per_project=docs_per_project, container_tag=container_tag
        )

    scoped_queries = 0
    leaked_hits = 0
    leaked_queries = 0
    returned_hits = 0
    unresolvable_hits = 0
    unscoped_queries = 0
    unscoped_cross_scope_hits = 0
    per_query: list[dict[str, Any]] = []
    leaks_detail: list[dict[str, Any]] = []
    attributed_all: list[Any] = []
    unscoped_origins: set[str] = set()
    scoped_origins: set[str] = set()

    for run, requested in queries:
        hits = run(conn)
        attributions = attribute_hits(conn, hits)
        attributed_all.extend(attributions)
        leaks = detect(attributions, requested)

        if requested is None:
            # Unscoped: cross-project hits are EXPECTED. Bucketed separately and
            # excluded from both the gate numerator and denominator (I2).
            unscoped_queries += 1
            origins = {
                str(a.originating_project_id) for a in attributions if a.hit_kind != "unresolvable"
            }
            unscoped_origins.update(origins)
            if len(origins) > 1:
                unscoped_cross_scope_hits += sum(
                    1 for a in attributions if a.originating_project_id is not None
                )
            continue

        scoped_queries += 1
        returned_hits += len(attributions)
        unresolvable_hits += sum(1 for a in attributions if a.hit_kind == "unresolvable")
        scoped_origins.update(
            str(a.originating_project_id)
            for a in attributions
            if a.originating_project_id is not None
        )
        if leaks:
            leaked_queries += 1
            leaked_hits += leaks
            leaks_detail.extend(_leak_records(attributions, requested))
        # Per-query rate, always emitted next to returned_hits: 0.5% over 2 hits and
        # 0.5% over 200 hits are different facts (I3).
        per_query_rate = (leaks / len(attributions)) if attributions else None
        per_query.append(
            {
                "scope": requested,
                "returned_hits": len(attributions),
                "leaked_hits": leaks,
                "leak_rate": per_query_rate,
                # The leaks actually listed below must be exactly those counted here.
                "leak_records": sum(1 for a in attributions if is_leak(a, requested)),
            }
        )

    leak_rate = (leaked_hits / returned_hits) if returned_hits else None
    query_leak_rate = (leaked_queries / scoped_queries) if scoped_queries else None

    isolation = {
        "scoped_queries": scoped_queries,
        "leaked_hits": leaked_hits,
        "leaked_queries": leaked_queries,
        "returned_hits": returned_hits,
        "unresolvable_hits": unresolvable_hits,
        "leak_rate": leak_rate,
        "query_leak_rate": query_leak_rate,
        "unscoped_queries": unscoped_queries,
        "unscoped_cross_scope_hits": unscoped_cross_scope_hits,
        "per_query": per_query,
        "leaks_detail": leaks_detail,
        "_unscoped_origins": sorted(unscoped_origins),
        "_scoped_origins": sorted(scoped_origins),
    }
    # Full per-hit attribution is opt-in: at ~160 hits it dominates the artifact and
    # buries the metrics an operator is reading. A leak run includes it automatically,
    # because that is exactly when an operator needs to see every hit's origin.
    if include_attribution or leaked_hits or unresolvable_hits:
        isolation["_attributed"] = [a.as_dict(omit_text=True) for a in attributed_all]

    gate = Gate()
    # THE GATE: an absolute count, so no tolerance needs tuning (I1).
    gate.check("leaked_hits", value=leaked_hits, bound=0, comparison=">")
    # A run that measured no scoped query has not observed isolation.
    gate.check(
        "scoped_queries_observed",
        value=scoped_queries if scoped_queries else None,
        bound=1,
        comparison="<",
        reason="no scoped query ran, so isolation was never observed",
    )
    if unresolvable_hits:
        gate.check(
            "unresolvable_hits",
            value=unresolvable_hits,
            bound=0,
            comparison=">",
            reason="a hit whose origin cannot be established has not been shown to be in scope",
        )

    manifest = build_manifest(
        seed=seed,
        requested_n=len(queries),
        n=n if n is not None else len(queries),
        ks=list(ks),
        modes=["scoped-probe"],
        embedder=embedder_label(HashEmbedder(dims=64)),
        # Honest label, matching the cost axis: `search()` is called with no vector_store
        # factory here, so the in-process SQLite scan runs. Claiming "SQLiteVectorStore"
        # would make this manifest compare as equal to one measured against the real store
        # while a different code path produced it (M2 hard-compares this field).
        vector_store="sqlite",
        data_sha256="synthetic-scoped-corpus",
        scope_config=corpus.scope_config(),
    )

    return {
        "axis": "isolation",
        "schema": "memoratum-eval-axes-v1",
        "manifest": manifest,
        "isolation": isolation,
        "gate": gate.as_dict(),
    }


def _leak_records(attributions: Sequence[Any], requested: str | None) -> list[dict[str, Any]]:
    """Per-leak attribution required by SC-004."""
    return [
        {
            "hit_id": a.hit_id,
            "hit_kind": a.hit_kind,
            "document_id": a.document_id,
            "originating_project_id": a.originating_project_id,
            "requested_project_id": requested,
            "rank": a.rank,
        }
        for a in attributions
        if is_leak(a, requested)
    ]


# --------------------------------------------------------------------------- #
# Falsifiability self-check
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SelfCheckOutcome:
    """Result of the falsifiability self-check (I4, SC-001)."""

    detected: bool
    injected_leaks: int
    clean_leaked_hits: int
    exit_code: int
    detail: str


def run_self_check(
    conn: sqlite3.Connection,
    *,
    projects: Sequence[str],
    docs_per_project: int = 3,
    container_tag: str = "bench",
    ks: Sequence[int] = (5,),
    detector: Callable[[Sequence[Any], str | None], int] | None = None,
    seed: int = 42,
) -> SelfCheckOutcome:
    """Inject a leak that MUST be detected, and verify the detector sees it.

    The injected leak is an unscoped retrieval presented as scoped. The store's own
    filtering is not broken -- so this tests the **metric**, which is the thing that
    could silently stop working. A clean run only means something if this passes.

    Both halves matter. A detector that misses the injection is broken, and so is one
    that reports leaks for a correctly scoped query: the second would turn CI red on
    every run for an unrelated reason, teaching operators to ignore the gate.
    """
    detect = detector if detector is not None else default_detector
    try:
        return _self_check_in(conn, projects, docs_per_project, container_tag, ks, detect, seed)
    finally:
        # run_self_check is handed a connection it does not own, but leaving it open
        # leaks the SQLite handle plus its WAL and SHM files for the life of the process.
        conn.close()


def _self_check_in(
    conn: sqlite3.Connection,
    projects: Sequence[str],
    docs_per_project: int,
    container_tag: str,
    ks: Sequence[int],
    detect: Callable[[Sequence[Any], str | None], int],
    seed: int,
) -> SelfCheckOutcome:
    target = projects[0]
    probe_projects = tuple(projects)
    corpus = build_scoped_corpus(
        conn,
        projects=probe_projects,
        docs_per_project=docs_per_project,
        container_tag=container_tag,
    )

    def leaky(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        # Unscoped retrieval, attributed to `target` -- a genuine scope violation from
        # the caller's point of view.
        return search(
            conn,
            HashEmbedder(dims=64),
            CORPUS_QUERY,
            container_tag=container_tag,
            limit=20,
        )

    injected = evaluate_isolation(
        conn,
        queries=[(leaky, target)],
        projects=probe_projects,
        container_tag=container_tag,
        docs_per_project=docs_per_project,
        ks=list(ks),
        seed=seed,
        detector=detect,
        corpus=corpus,
    )
    injected_leaks = injected["isolation"]["leaked_hits"]

    clean = evaluate_isolation(
        conn,
        queries=[(default_query(target, limit=10, container_tag=container_tag), target)],
        projects=probe_projects,
        container_tag=container_tag,
        docs_per_project=docs_per_project,
        ks=list(ks),
        seed=seed,
        detector=detect,
        corpus=corpus,
    )
    clean_leaked = clean["isolation"]["leaked_hits"]

    if injected_leaks <= 0:
        return SelfCheckOutcome(
            detected=False,
            injected_leaks=0,
            clean_leaked_hits=clean_leaked,
            exit_code=EXIT_SELF_CHECK_FAILED,
            detail=(
                "the detector did not see a deliberately injected cross-scope hit, so a "
                "clean result in this run means nothing (I4)"
            ),
        )
    if clean_leaked != 0:
        return SelfCheckOutcome(
            detected=True,
            injected_leaks=injected_leaks,
            clean_leaked_hits=clean_leaked,
            exit_code=EXIT_SELF_CHECK_FAILED,
            detail=(
                f"the detector reported {clean_leaked} leaks for a correctly scoped query, "
                "so it would fail every real run (false positives)"
            ),
        )
    return SelfCheckOutcome(
        detected=True,
        injected_leaks=injected_leaks,
        clean_leaked_hits=0,
        exit_code=EXIT_OK,
        detail=(
            f"detection proven: {injected_leaks} injected cross-scope hits found, "
            "0 false positives on a correctly scoped query"
        ),
    )


# --------------------------------------------------------------------------- #
# Reporting and CLI
# --------------------------------------------------------------------------- #

LEAK_PREVIEW_ROWS = 20


def summarize(result: dict[str, Any], *, self_check: SelfCheckOutcome | None = None) -> str:
    iso = result["isolation"]
    lines = [
        "# Scope isolation",
        "",
        f"gate: {result['gate']['status'].upper()}",
        "",
        "## Scoped queries (the gate)",
        f"- scoped_queries: {iso['scoped_queries']}",
        f"- leaked_hits: {iso['leaked_hits']}  <- gate, must be 0",
        f"- leaked_queries: {iso['leaked_queries']}",
        f"- returned_hits: {iso['returned_hits']}",
        f"- leak_rate: {_fmt(iso['leak_rate'])} (trend only, never gates)",
        f"- query_leak_rate: {_fmt(iso['query_leak_rate'])} (trend only, never gates)",
        f"- unresolvable_hits: {iso['unresolvable_hits']}",
        "",
        "## Unscoped queries (informational)",
        f"- unscoped_queries: {iso['unscoped_queries']}",
        f"- unscoped_cross_scope_hits: {iso['unscoped_cross_scope_hits']}",
        f"- origins seen: {', '.join(iso['_unscoped_origins']) or 'none'}",
        "",
        (
            "Excluded from the gate numerator and denominator: `project_id=null` means no "
            "scope was requested, so cross-project hits are expected."
        ),
        "",
    ]
    leaks = iso["leaks_detail"]
    if leaks:
        lines += ["## Leaks", ""]
        for leak in leaks[:LEAK_PREVIEW_ROWS]:
            lines.append(
                f"- rank {leak['rank']}: {leak['hit_id']} "
                f"({leak['hit_kind']}) from {leak['originating_project_id']!r} "
                f"returned to a query scoped to {leak['requested_project_id']!r}"
            )
        if len(leaks) > LEAK_PREVIEW_ROWS:
            lines.append(f"- ... and {len(leaks) - LEAK_PREVIEW_ROWS} more")
        lines.append("")
    if self_check is not None:
        lines += [
            "## Falsifiability self-check",
            f"- detected injected leak: {self_check.detected}",
            f"- injected_leaks: {self_check.injected_leaks}",
            f"- false positives on a clean query: {self_check.clean_leaked_hits}",
            f"- {self_check.detail}",
            "",
        ]
    scope = result["manifest"]["scope_config"]
    lines += [
        "## Manifest",
        f"- schema: {result['manifest']['schema']}",
        f"- projects: {', '.join(scope['projects'])}",
        f"- expected_documents: {scope['expected_documents']}",
        f"- embedder: {result['manifest']['embedder']}",
        "",
    ]
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _project_names(count: int) -> tuple[str, ...]:
    return tuple(f"proj-{chr(ord('a') + i)}" for i in range(count))


def _command(argv: list[str] | None) -> list[str]:
    return command(argv, module="memoratum.eval_isolation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scope isolation axis")
    parser.add_argument("--projects", type=int, default=3)
    parser.add_argument("--sessions-per-project", type=int, default=40)
    parser.add_argument("--queries", type=int, default=40)
    parser.add_argument("--unscoped-queries", type=int, default=10)
    parser.add_argument("--k", default="5,10")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--container-tag", default="bench")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--out-md", default="")
    parser.add_argument("--out-json", default="")
    args = parser.parse_args(argv)

    if args.projects < 2:
        print("isolation needs --projects >= 2; one project cannot leak", file=sys.stderr)
        return EXIT_BAD_INPUT
    if args.sessions_per_project < 1 or args.queries < 1:
        print("--sessions-per-project and --queries must be >= 1", file=sys.stderr)
        return EXIT_BAD_INPUT

    projects = _project_names(args.projects)
    ks = iter_ints(args.k)
    limit = retrieval_budget(ks)
    # TemporaryDirectory, matching eval_longmemeval and eval_cost: a bare mkdtemp leaks
    # for the life of the process, and a suite that runs this axis repeatedly is enough
    # to fill /tmp.
    directory = tempfile.TemporaryDirectory(prefix="memoratum-isolation-")
    conn: sqlite3.Connection | None = None
    try:
        conn = db_module.connect(os.path.join(directory.name, "isolation.db"))
        corpus = build_scoped_corpus(
            conn,
            projects=projects,
            docs_per_project=args.sessions_per_project,
            container_tag=args.container_tag,
        )

        queries: list[Query] = [
            (
                default_query(
                    projects[i % len(projects)], limit=limit, container_tag=args.container_tag
                ),
                projects[i % len(projects)],
            )
            for i in range(args.queries)
        ]
        queries += [
            (default_query(None, limit=limit, container_tag=args.container_tag), None)
            for _ in range(args.unscoped_queries)
        ]

        result = evaluate_isolation(
            conn,
            queries=queries,
            projects=projects,
            container_tag=args.container_tag,
            docs_per_project=args.sessions_per_project,
            ks=ks,
            seed=args.seed,
            corpus=corpus,
        )

        self_check = None
        if args.require_clean:
            self_check = run_self_check(
                db_module.connect(os.path.join(directory.name, "selfcheck.db")),
                projects=projects,
                docs_per_project=min(args.sessions_per_project, 8),
                container_tag=args.container_tag,
                ks=ks,
                seed=args.seed,
            )
            result["self_check"] = {
                "detected": self_check.detected,
                "injected_leaks": self_check.injected_leaks,
                "clean_leaked_hits": self_check.clean_leaked_hits,
                "detail": self_check.detail,
            }

        write_artifacts(
            result,
            summarize(result, self_check=self_check),
            out_md=args.out_md,
            out_json=args.out_json,
            argv=_command(argv),
        )

        # A broken metric outranks a detected leak: exit 2 first (I4).
        if self_check is not None and self_check.exit_code != EXIT_OK:
            return self_check.exit_code
        return EXIT_GATE_FAILED if result["gate"]["status"] == FAIL else EXIT_OK
    finally:
        # Both the connection and the directory are released on every path, including a
        # failure inside connect() itself -- which is why the connect moved inside the try.
        if conn is not None:
            conn.close()
        directory.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
