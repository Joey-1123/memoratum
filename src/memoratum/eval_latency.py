# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Latency axis: where the time goes, per phase, per corpus size.

A slow memory layer and a slow index have different fixes, so ingest, embedding,
indexing and retrieval are timed separately (FR-004). The rules below are measured, not
stylistic -- see ``research.md`` D3 and ``data-model.md`` L1-L8:

* **Microseconds-per-chunk is the primary gate, not milliseconds.** Measured across a 64x
  corpus ladder, per-chunk cost fell 35.6 -> 30.1 -> 8.2 -> 7.3 us. Sub-linear growth
  shows as a *falling* number; a regression to O(n) shows as a *rising* one. That
  direction is meaningful in a way absolute milliseconds are not.
* **The sample floor is 20.** Bootstrapping median-of-n against the true median over
  2,000 resamples: n=5 needs a +69% band and cannot detect anything smaller than a 70%
  regression, which makes the gate theatre; n=30 needs only +7%.
* **Raw samples are returned, never a pre-summarised figure.** p95-of-p95 is not a p95,
  and single-sample relative MAD is 5-16% with max/median 2.11x, so the mean is unusable.
* **The ladder must not cross the prefilter threshold.** Crossing it changes the
  *algorithm* -- whether the FTS5 leg bounds candidates -- rather than the corpus size.
  Measured, that step produced a 3.38x -> 1.09x -> 3.55x wobble, and this repository has
  already shipped one false result from this class.
* **No remote embedder inside a timed region.** Network jitter dwarfs everything measured.
* **Harness bookkeeping stays outside the timed region**, or the harness benchmarks itself.
"""

from __future__ import annotations

import argparse
import itertools
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Sequence
from typing import Any

from memoratum import db as db_module
from memoratum.embeddings import HashEmbedder
from memoratum.eval_axes import (
    EXIT_BAD_INPUT,
    EXIT_BASELINE_MISSING,
    EXIT_GATE_FAILED,
    EXIT_OK,
    FAIL,
    Gate,
    ManifestError,
    build_manifest,
    collect_hardware,
    embedder_label,
    iter_ints,
    median,
    percentile,
    retrieval_budget,
    sample_ms,
    us_per_chunk,
    write_artifacts,
)
from memoratum.ingest import process_all
from memoratum.search import prefilter_min_candidates, search

#: The four phases FR-004 requires, timed separately.
PHASES = ("ingest", "embed", "index", "retrieve")

#: Entirely below the default prefilter threshold of 512. A ladder crossing it measures an
#: algorithm switch rather than a corpus size (L7).
DEFAULT_LADDER = (100, 200, 400)
DEFAULT_SAMPLES = 30
DEFAULT_WARMUP = 5

#: The primary gate. Measured sub-linear growth is ~0.85x per doubling, so per-chunk cost
#: should be flat-to-falling; a return to O(n) multiplies it by the corpus growth factor.
US_PER_CHUNK_MAX_RATIO = 1.5

#: The absolute-millisecond smoke bound (L2). +40% against +7% statistical headroom at
#: n=30 leaves ~33% for real drift and hardware variation.
MEDIAN_TOLERANCE = 0.40

#: Phases faster than this are reported but not gated: their statistical noise is larger
#: than any useful band, so a gate there would be theatre (L2).
GATEABLE_MS = 2.0


def sample_phase(fn: Callable[[], Any], *, samples: int, warmup: int) -> list[float]:
    """Time one phase and return RAW millisecond samples.

    Warmup runs first and is discarded; otherwise the initial call pays FTS5 tokenizer
    setup and statement compilation, which would dominate a fast phase (L4).
    """
    return sample_ms(fn, samples=samples, warmup=warmup)


def observe_prefilter(
    conn: sqlite3.Connection, query: str, *, container_tag: str, limit: int = 10
) -> bool:
    """Whether the FTS5 leg bounded the chunk SELECT for this query.

    Observed through ``set_trace_callback`` -- the technique already used in
    ``tests/test_search_perf.py`` -- because ``sqlite3.Connection`` attributes are
    immutable, so the SQL cannot be seen by patching ``execute``.

    True when the chunk SELECT carried a candidate-set bound, which only happens above the
    threshold and with keyword hits. Asserting this is what stops a corpus ladder from
    silently measuring an algorithm switch (L7).
    """
    traced: list[str] = []
    conn.set_trace_callback(lambda stmt: traced.append(" ".join(stmt.split())))
    try:
        search(
            conn,
            HashEmbedder(dims=64),
            query,
            container_tag=container_tag,
            limit=limit,
            search_mode="documents",
        )
    finally:
        conn.set_trace_callback(None)
    selects = [q for q in traced if "FROM chunks c" in q and "COUNT(*)" not in q]
    return any("c.id IN" in q for q in selects)


def _phase_figures(
    timings: Sequence[float],
    *,
    chunks: int = 0,
    prefilter: bool | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    mid = median(timings)
    figures: dict[str, Any] = {
        "samples": len(timings),
        "samples_ms": list(timings),
        "median_ms": mid,
        "p95_ms": percentile(timings, 95),
        "us_per_chunk": us_per_chunk(mid, chunks) if chunks else None,
        "prefilter_engaged": prefilter,
    }
    if note:
        figures["note"] = note
    return figures


def build_corpus(
    chunks_target: int,
) -> tuple[tempfile.TemporaryDirectory, sqlite3.Connection, int, dict[str, Any]]:
    """Ingest a corpus of roughly ``chunks_target`` chunks, timing ingest/embed/index.

    Returns the temporary directory (the caller owns cleanup), the connection, the chunk
    count actually produced, and the three preparation phase timings.

    The chunk count is ASSERTED, not assumed: ``process_one`` swallows every exception, so
    a total ingest failure leaves zero chunks and would otherwise be indistinguishable from
    a fast phase.
    """
    directory = tempfile.TemporaryDirectory(prefix="memoratum-latency-")
    conn = db_module.connect(os.path.join(directory.name, "latency.db"))
    embedder = HashEmbedder(dims=64)

    # One document per markdown section, so a document count near the chunk target is a
    # reasonable starting point; the actual chunk count is read back from the database.
    ingest_ms: list[float] = []
    for n in range(chunks_target):
        body = (
            f"## Line {n}\n\nThe quarterly budget review covers travel and lodging "
            f"for line item {n} of the schedule."
        )
        ingest_ms.extend(
            sample_phase(
                lambda name=f"doc-{n}", text=body: db_module.create_document(
                    conn, container_tag="bench", content=text, custom_id=name
                ),
                samples=1,
                warmup=0,
            )
        )

    queued = conn.execute("SELECT COUNT(*) c FROM documents WHERE status = 'queued'").fetchone()[
        "c"
    ]
    if not queued:
        raise ManifestError(
            f"ingest queued no documents for a {chunks_target}-chunk target, so nothing "
            "can be measured (a swallowed ingest error would look like a fast phase)"
        )

    # process_all performs chunking, embedding and indexing in one pass, so they are timed
    # as a combined preparation figure rather than three fabricated ones.
    embed_index_ms = sample_phase(lambda: process_all(conn, embedder), samples=1, warmup=0)
    chunks = conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
    if chunks == 0:
        raise ManifestError(
            f"ingest produced no chunks for a {chunks_target}-chunk target, so nothing "
            "can be measured (a swallowed ingest error would look like a fast phase)"
        )
    preparation = {
        "ingest": _phase_figures(ingest_ms, note="per document, not resampled"),
        "embed": _phase_figures(
            embed_index_ms, chunks=chunks, note="chunk+embed+index in one pass"
        ),
        "index": _phase_figures(
            embed_index_ms, chunks=chunks, note="same pass as embed; not separately timed"
        ),
    }
    return directory, conn, chunks, preparation


def _time_retrieval(
    conn: sqlite3.Connection, *, ks: Sequence[int], samples: int, warmup: int
) -> list[float]:
    """Time retrieval, and nothing else.

    Extracted so the timed region is a named function rather than a lambda closing over a
    loop variable: the region must contain the ``search`` call and no harness bookkeeping,
    or the harness benchmarks itself (L6).
    """
    return sample_phase(
        lambda: search(
            conn,
            HashEmbedder(dims=64),
            "quarterly budget review",
            container_tag="bench",
            limit=retrieval_budget(list(ks)),
        ),
        samples=samples,
        warmup=warmup,
    )


def evaluate_latency(
    *,
    ladder: Sequence[int] = DEFAULT_LADDER,
    samples: int = DEFAULT_SAMPLES,
    warmup: int = DEFAULT_WARMUP,
    ks: Sequence[int] = (5,),
    seed: int = 42,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Time all four phases at each corpus size and gate on per-chunk cost."""
    # Validate through the manifest so the rules live in one place: the sample floor, the
    # three-size minimum, and the prefilter-straddle check (L2, FR-004, L7).
    build_manifest(
        seed=seed,
        requested_n=len(ladder),
        n=len(ladder),
        ks=list(ks),
        modes=["documents"],
        embedder=embedder_label(HashEmbedder(dims=64)),
        vector_store="sqlite",
        data_sha256="synthetic-latency-corpus",
        latency_config={
            "samples": samples,
            "warmup": warmup,
            "ladder": list(ladder),
            "hardware": collect_hardware(),
            # Explicit null: a reader must be able to see the ladder was checked against
            # the threshold and that no remote embedder sat in a timed region (L5, L7).
            "prefilter_min": None,
            "provider_embedder": None,
        },
    )

    entries: list[dict[str, Any]] = []
    for target in ladder:
        directory, conn, chunks, preparation = build_corpus(target)
        try:
            retrieve_ms = _time_retrieval(conn, ks=ks, samples=samples, warmup=warmup)
            retrieve = _phase_figures(
                retrieve_ms,
                chunks=chunks,
                # Engagement is only meaningful above the threshold; null below it is
                # honest, and a null above it would be a defect.
                prefilter=(
                    observe_prefilter(conn, "quarterly budget review", container_tag="bench")
                    if chunks > prefilter_min_candidates()
                    else None
                ),
            )
            entries.append(
                {
                    "corpus_chunks": chunks,
                    "corpus_target": target,
                    "phases": {**preparation, "retrieve": retrieve},
                }
            )
        finally:
            conn.close()
            directory.cleanup()

    gate = gate_latency(Gate(), ladder=entries, baseline=baseline)

    manifest = build_manifest(
        seed=seed,
        requested_n=len(ladder),
        n=len(entries),
        ks=list(ks),
        modes=["documents"],
        embedder=embedder_label(HashEmbedder(dims=64)),
        vector_store="sqlite",
        data_sha256="synthetic-latency-corpus",
        latency_config={
            "samples": samples,
            "warmup": warmup,
            "ladder": list(ladder),
            "hardware": collect_hardware(),
            "prefilter_min": None,
            "provider_embedder": None,
        },
    )

    return {
        "axis": "latency",
        "schema": "memoratum-eval-axes-v1",
        "manifest": manifest,
        "latency": {
            "ladder": entries,
            "hardware": manifest["latency_config"]["hardware"],
        },
        "gate": gate.as_dict(),
    }


def figure_key(phase: str) -> str:
    """The baseline figure name for a phase's median."""
    return f"{phase}_median_ms"


def gate_latency(
    gate: Gate,
    *,
    ladder: Sequence[dict[str, Any]],
    baseline: dict[str, Any] | None = None,
) -> Gate:
    """Add latency checks to ``gate``.

    Two tiers, deliberately. The per-chunk ratio is the real signal and is
    hardware-independent. Absolute milliseconds are a smoke bound that catches a gross
    collapse; they are not a service-level objective, and they are skipped for phases too
    fast for any band to mean anything (L2).
    """
    ordered = sorted(ladder, key=lambda entry: entry["corpus_chunks"])
    for lower, upper in itertools.pairwise(ordered):
        low_value = lower["phases"]["retrieve"].get("us_per_chunk")
        high_value = upper["phases"]["retrieve"].get("us_per_chunk")
        if not low_value or not high_value:
            continue
        ratio = high_value / low_value
        growth = upper["corpus_chunks"] / lower["corpus_chunks"]
        # Per-chunk cost may rise by at most US_PER_CHUNK_MAX_RATIO per 4x of corpus. A
        # doubling gives sqrt of it, so a 2x step is judged on a much tighter band than
        # a 4x one -- otherwise a short step could hide a linear regression.
        bound = US_PER_CHUNK_MAX_RATIO ** (growth / 4)
        gate.check(
            "us_per_chunk_sublinear",
            value=ratio,
            bound=bound,
            comparison=">",
            phase="retrieve",
            delta=ratio - 1.0,
            reason=(
                f"per-chunk cost rose {ratio:.2f}x while the corpus grew {growth:.1f}x "
                f"({lower['corpus_chunks']} -> {upper['corpus_chunks']} chunks); "
                "sub-linear growth keeps this near 1.0 and a return to O(n) pushes it "
                "toward the growth factor (L1)"
            ),
        )

    if baseline is None:
        # No baseline means NO absolute verdict is claimed, and no check is emitted for it.
        # A null-bound check can only ever fail, so adding one would be a constant false
        # negative that masks the ratio gate -- which is the one gate here that needs no
        # baseline, because a per-chunk ratio compares a run against itself. The absence
        # is stated in the report instead.
        return gate

    # A baseline WAS supplied. If it is unusable -- no hardware for a hardware-bound
    # figure, an unparseable cell, a missing value -- that must FAIL rather than silently
    # skip the whole tier, which is B3's exact prohibition.
    from memoratum.eval_axes import load_baseline

    for entry in ladder:
        for phase, figures in entry["phases"].items():
            mid = figures.get("median_ms")
            if mid is None or mid < GATEABLE_MS:
                # Reported, not gated: a sub-2 ms phase carries more statistical noise
                # than any useful band, so gating it would be theatre (L2).
                continue
            key = figure_key(phase)
            expected = baseline.get(key)
            if expected is None:
                continue
            usable = load_baseline(expected)
            if usable is None:
                gate.check(
                    key,
                    value=mid,
                    bound=None,
                    comparison=">",
                    phase=phase,
                    reason=(
                        "the supplied baseline for this figure is unusable -- missing "
                        "hardware for a hardware-bound figure, or an unparseable value. "
                        "An unmeasurable baseline must not silently pass (B1, B3)"
                    ),
                )
                continue
            gate.check(
                key,
                value=mid,
                bound=usable["value"] * (1 + MEDIAN_TOLERANCE),
                comparison=">",
                phase=phase,
                delta=mid - usable["value"],
            )
    return gate


def summarize(result: dict[str, Any]) -> str:
    config = result["manifest"]["latency_config"]
    hardware = config["hardware"]
    lines = [
        "# Latency per phase",
        "",
        f"gate: {result['gate']['status'].upper()}",
        "",
        "| chunks | phase | median ms | p95 ms | us/chunk | prefilter |",
        "|---|---|---|---|---|---|",
    ]
    for entry in result["latency"]["ladder"]:
        for phase in PHASES:
            figures = entry["phases"][phase]
            lines.append(
                f"| {entry['corpus_chunks']} | {phase} | "
                f"{_fmt(figures.get('median_ms'))} | {_fmt(figures.get('p95_ms'))} | "
                f"{_fmt(figures.get('us_per_chunk'))} | "
                f"{figures.get('prefilter_engaged')} |"
            )
    lines += [
        "",
        f"- samples per phase: {config['samples']} (warmup {config['warmup']} discarded)",
        f"- ladder: {config['ladder']}",
        (
            f"- prefilter threshold: {prefilter_min_candidates()} (ladder stays below it, "
            "so no step measures an algorithm switch)"
        ),
        "",
        "us/chunk is the primary gate: sub-linear growth makes it fall, a regression to",
        "O(n) makes it rise. Absolute milliseconds are a smoke bound only, and phases under",
        f"{GATEABLE_MS} ms are reported rather than gated.",
        "",
        "## Gate",
        "",
    ]
    for check in result["gate"]["checks"]:
        lines.append(
            f"- {check['name']}: {check['status']} (value {check['value']}, bound {check['bound']})"
        )
    lines += [
        f"- overall: {result['gate']['status'].upper()}",
        "",
        "## Hardware (required; without it these numbers do not travel)",
        f"- cpu: {hardware['cpu']}",
        f"- cores: {hardware['cores']} | ram_mb: {hardware['ram_mb']}",
        f"- python: {hardware['python']}",
        f"- platform: {hardware['platform']}",
        "",
    ]
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 1:
        return f"{value:.2f}"
    return f"{value:.3f}"


def load_baselines(path: str) -> dict[str, dict[str, Any]]:
    """Read every latency row from an ``eval/BASELINES.md`` table, keyed by figure name.

    Every figure is loaded, not just one, because ``gate_latency`` asks for a different
    key per phase. A row the axis does not recognise is left in place so a typo shows up
    as a missing baseline rather than being silently dropped.
    """
    from memoratum.eval_cost import _load_baseline_rows

    if not path or not os.path.exists(path):
        return {}
    return _load_baseline_rows(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Latency axis")
    parser.add_argument("--ladder", default=",".join(str(n) for n in DEFAULT_LADDER))
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--k", default="5,10")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--baseline", default="")
    parser.add_argument("--out-md", default="")
    parser.add_argument("--out-json", default="")
    args = parser.parse_args(argv)

    baselines = load_baselines(args.baseline)
    if args.baseline and not baselines:
        # A named-but-empty baseline is an error, not a silent pass (B3).
        print(
            f"no usable baseline rows in {args.baseline}; a missing baseline must not "
            "read as a pass (B3)",
            file=sys.stderr,
        )
        return EXIT_BASELINE_MISSING

    try:
        result = evaluate_latency(
            ladder=iter_ints(args.ladder),
            samples=args.samples,
            warmup=args.warmup,
            ks=iter_ints(args.k),
            seed=args.seed,
            baseline=baselines or None,
        )
    except (ManifestError, ValueError, TypeError, OSError) as exc:
        # Never exit 1 from here: the contract reserves that for a latency regression, and
        # an on-call would otherwise read a typo as a performance regression.
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        write_artifacts(result, summarize(result), out_md=args.out_md, out_json=args.out_json)
    except OSError as exc:
        print(f"could not write results: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    return EXIT_GATE_FAILED if result["gate"]["status"] == FAIL else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
