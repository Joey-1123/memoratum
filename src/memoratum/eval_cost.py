# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Token-cost axis: what a memory layer costs in context-window budget per retrieval.

Cost is the metric that makes a memory store comparable against no memory at all, and it
is exactly measurable here. It is also the axis most likely to be quietly wrong, so the
rules below are enforced rather than documented (see ``research.md`` D2 and
``data-model.md`` C1-C6):

* **Characters are canonical; bytes are rejected as a reported unit.** The corpus is pure
  ASCII, so bytes carry no information -- yet on non-ASCII they inflate 1.1-3x with
  nothing visible in the report. Characters are also the unit the codebase already
  spends: ``eval_mc10.py`` truncates with ``context[:12000]``.
* **The chars-per-token ratio is measured, never hardcoded.** A real BPE tokenizer gives
  ~4.0-4.2 chars/token for English prose, so whitespace tokens overcount by ~1.5x. That
  figure is unverifiable here without a tokenizer dependency this project refuses
  (Principle V), so it is a *calibration constant*, not a fact.
* **The token estimate is valid only as a ratio between two runs on the same corpus.**
  "This query costs 1,850 tokens" would be a guess inheriting every tokenizer difference
  the project declines to depend on.
* **Tokenizer presence must not change the gated figure** (C6, FR-007), or the metric
  becomes machine-dependent.
* **The retrieval budget is not inflated.** It existed to feed the *old* ``R@k``, which
  counted distinct sessions. Under the corrected definition ``R@k`` reads the first ``k``
  hits, so the existing budget is merely generous and scaling it would mask a real signal.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from memoratum import db as db_module
from memoratum.embeddings import HashEmbedder
from memoratum.eval_axes import (
    COST_UNITS,
    EXIT_BAD_INPUT,
    EXIT_GATE_FAILED,
    EXIT_OK,
    FAIL,
    Gate,
    ManifestError,
    attribute_hits,
    build_manifest,
    compare_to_baseline,
    embedder_label,
    iter_ints,
    median,
    percentile,
    write_artifacts,
)
from memoratum.ingest import process_all
from memoratum.search import search

RATIO_SAMPLE_RECORDS = 20

TOKEN_ESTIMATE_CAVEAT = (
    "derived from the corpus-measured chars_per_ws_token; valid only as a ratio between "
    "runs on the same corpus, not as an absolute token count"
)


def retrieval_limit(ks: Sequence[int]) -> int:
    """The retrieval budget, in HITS.

    Deliberately the same ``max(2*max(ks), 10)`` that ``eval_longmemeval`` already uses.
    It originally existed so the *old* ``R@k`` would find ``k`` distinct sessions in a
    deep hit list; under the corrected definition ``R@k`` reads the first ``k`` hits, so
    this is merely generous. Scaling it by chunks-per-document would be treating a
    symptom that no longer exists, and would mask a genuine cost regression (C5).
    """
    return max(max(ks, default=0) * 2, 10)


def measure_chars_per_ws_token(texts: Sequence[str]) -> float | None:
    """Measure characters per whitespace token over the corpus in front of us.

    ``None`` when there is nothing to measure -- never ``0.0``, which would read as a
    measurement and make the derived token estimate infinite (M3).
    """
    chars = 0
    tokens = 0
    for text in texts:
        if not text:
            continue
        chars += len(text)
        tokens += len(text.split())
    if chars == 0 or tokens == 0:
        return None
    return chars / tokens


def estimate_tokens(chars: float | None, chars_per_ws_token: float | None) -> float | None:
    """Convert characters to a labelled token estimate. ``None`` on missing input."""
    if chars is None or not chars_per_ws_token or chars_per_ws_token <= 0:
        return None
    return chars / chars_per_ws_token


def head_records(path: str | Path, *, limit: int) -> list[dict[str, Any]]:
    """Read the first ``limit`` records without materialising the whole file.

    ``eval_datasets.load_records`` reads a 277 MB corpus as one string and then parses it
    into Python objects, which exhausts a small host. The cost axis only needs a prefix,
    for both the sampled questions and the ratio measurement, so it streams instead.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    source = Path(path)
    if source.suffix.lower() == ".jsonl":
        records: list[dict[str, Any]] = []
        with source.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
                if len(records) >= limit:
                    break
        return records

    decoder = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    with source.open(encoding="utf-8") as handle:
        buffer = handle.read(1 << 20)
        position = 0
        while position < len(buffer) and buffer[position] in " \t\r\n,":
            position += 1
        if position >= len(buffer) or buffer[position] != "[":
            raise ValueError("evaluation data must be a top-level JSON array of objects")
        position += 1
        while len(out) < limit:
            while position < len(buffer) and buffer[position] in " \t\r\n,":
                position += 1
            try:
                obj, end = decoder.raw_decode(buffer, position)
            except ValueError:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                buffer = buffer[position:] + chunk
                position = 0
                continue
            if not isinstance(obj, dict):
                raise TypeError("evaluation data must be a top-level JSON array of objects")
            out.append(obj)
            position = end
            if position > (1 << 19):
                buffer = buffer[position:]
                position = 0
    return out


def _session_texts(records: Sequence[Any]) -> list[str]:
    """Flatten a dataset prefix into the text a retriever would actually see.

    Tolerates both raw records (``haystack_sessions``) and normalised ones (``sessions``),
    because the ratio may be measured before or after normalisation. Normalised sessions
    carry a pre-rendered ``text`` field, which is preferred over re-formatting turns.
    """
    from memoratum.eval_longmemeval import format_session

    texts: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        sessions = record.get("sessions") or record.get("haystack_sessions") or []
        for session in sessions:
            # Raw LongMemEval puts the turn LIST directly in haystack_sessions, with no
            # session_id or date; the normalised form is a dict with `turns`. Both shapes
            # occur, so both are handled -- silently producing no texts would leave the
            # ratio None and the token estimate fabricated.
            if isinstance(session, dict):
                if session.get("text"):
                    texts.append(str(session["text"]))
                    continue
                turns = session.get("turns")
                if turns:
                    texts.append(format_session(turns))
            elif isinstance(session, list):
                texts.append(format_session(session))
    return texts


def _pick(records: Sequence[dict[str, Any]], n: int, seed: int) -> list[dict[str, Any]]:
    """Deterministic sample. Reuses the dataset adapter's own selection so the cost axis
    samples the same questions ``eval_longmemeval`` would (FR-009)."""
    from memoratum.eval_datasets import normalize_longmemeval, sample_records

    normalized = normalize_longmemeval(list(records))
    return sample_records(normalized, n=n, seed=seed)


def gate_cost(
    gate: Gate,
    *,
    cost: dict[str, Any],
    baseline: dict[str, Any] | None,
    figure: str = "retrieved_chars_mean",
) -> None:
    """Add the cost verdict to ``gate``.

    Separated from :func:`evaluate_cost` so the gate can be exercised against a synthetic
    baseline without re-running retrieval, and so the delta (SC-005) has one definition.
    """
    compare_to_baseline(gate, name=figure, value=cost.get(figure), baseline=baseline)


def measure_query(
    conn: sqlite3.Connection,
    question: dict[str, Any],
    *,
    embedder: Any,
    limit: int,
    mode: str,
) -> dict[str, float]:
    """Ingest one question's sessions, retrieve, and measure what came back.

    Extracted so the per-question body stays flat inside the ``with`` block: an earlier
    version nested it five levels deep, and a reindentation slip silently reshaped the
    logic rather than failing loudly.
    """
    for session in question["sessions"]:
        db_module.create_document(
            conn,
            container_tag="bench",
            content=_format_session(session["turns"]),
            custom_id=session["session_id"],
        )
    process_all(conn, embedder)
    hits = search(
        conn,
        embedder,
        question["question"],
        container_tag="bench",
        limit=limit,
        search_mode=mode,
    )
    attributions = attribute_hits(conn, hits)

    # Redundancy: hits beyond the first for each source row, which is what makes a cost
    # difference attributable when recall is unchanged (T038). Keyed on the SOURCE ROW,
    # not the chunk -- `merge_hits` already dedupes by chunk id, so what actually repeats
    # is several chunks drawn from one document. That is the real cost redundancy: one
    # long session occupying four of twenty context slots.
    row_hits: dict[tuple[str, Any], int] = {}
    for attribution in attributions:
        key = (attribution.hit_kind, attribution.document_id)
        row_hits[key] = row_hits.get(key, 0) + 1

    def text_of(hit: dict[str, Any]) -> str:
        return str(hit.get("chunk") or hit.get("memory") or "")

    sessions = question["sessions"]
    return {
        "hits": float(len(hits)),
        "chars": float(sum(len(text_of(hit)) for hit in hits)),
        "ws_tokens": float(sum(len(text_of(hit).split()) for hit in hits)),
        "redundant_hits": float(sum(count - 1 for count in row_hits.values())),
        "duplicate_source_rows": float(sum(1 for count in row_hits.values() if count > 1)),
        "chunks_per_session": conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
        / max(1, len(sessions)),
    }


def evaluate_cost(
    *,
    data: str,
    n: int = 20,
    seed: int = 42,
    ks: Sequence[int] = (5, 10),
    mode: str = "hybrid",
    unit: str = "chars",
    tokenizer: str | None = None,
    baseline: dict[str, Any] | None = None,
    measure_ratio: bool = True,
    ratio_sample: int = RATIO_SAMPLE_RECORDS,
) -> dict[str, Any]:
    """Measure retrieved characters per query and optionally gate against a baseline."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    # Validate the unit up front, so an unusable request fails before any work rather
    # than after a full corpus ingest. Raises the same ManifestError the manifest
    # validator raises, so there is one definition of "this unit is not allowed" (C1).
    if unit not in COST_UNITS:
        raise ManifestError(
            f"cost_config.unit must be one of {sorted(COST_UNITS)}, got {unit!r}; bytes "
            "and unknown units inflate 1.1-3x on non-ASCII (C1)"
        )

    # Prefix for both the sampled questions and the ratio measurement: the full corpus is
    # 277 MB and only a prefix is needed for either (T050).
    records = head_records(data, limit=max(n, ratio_sample if measure_ratio else n))
    if not records:
        raise ValueError(f"no records found in {data}")
    picked = _pick(records, n, seed)
    if not picked:
        raise ValueError(f"no usable records found in {data}")

    ratio: float | None = None
    if measure_ratio:
        ratio = measure_chars_per_ws_token(_session_texts(records[:ratio_sample]))
    if ratio is None:
        # A ratio is mandatory in cost_config (C2): a hardcoded constant would be a
        # fabricated figure in a committed artifact, so fall back to the corpus we have.
        ratio = measure_chars_per_ws_token(_session_texts(picked)) or 0
        if ratio <= 0:
            raise ValueError(
                "could not measure chars_per_ws_token from this corpus; the token estimate "
                "would be fabricated, which the manifest must not record (C2)"
            )

    limit = retrieval_limit(list(ks))
    embedder = HashEmbedder(dims=64)
    per_query_chars: list[float] = []
    per_query_ws_tokens: list[float] = []
    total_hits = 0
    redundant_hits = 0
    duplicate_rows = 0
    chunk_counts: list[int] = []

    for question in picked:
        # TemporaryDirectory as a context manager, matching eval_longmemeval. A bare
        # mkdtemp leaks one directory per question for the life of the process; across a
        # full suite that is enough to fill /tmp and break unrelated tests.
        with tempfile.TemporaryDirectory(prefix="memoratum-cost-") as directory:
            conn = db_module.connect(os.path.join(directory, "cost.db"))
            try:
                figure = measure_query(
                    conn,
                    question,
                    embedder=embedder,
                    limit=limit,
                    mode=mode,
                )
                per_query_chars.append(figure["chars"])
                per_query_ws_tokens.append(figure["ws_tokens"])
                total_hits += figure["hits"]
                redundant_hits += figure["redundant_hits"]
                duplicate_rows += figure["duplicate_source_rows"]
                chunk_counts.append(figure["chunks_per_session"])
            finally:
                conn.close()

    hits_total = total_hits
    chars_mean = (sum(per_query_chars) / len(per_query_chars)) if per_query_chars else None
    ws_mean = (sum(per_query_ws_tokens) / len(per_query_ws_tokens)) if per_query_ws_tokens else None

    # `unit` selects which figure is CANONICAL. Both are always measured, so switching
    # unit cannot change what was retrieved -- only which number is reported as the
    # headline. Whitespace tokens have a 7.5% coefficient of variation, so they are a
    # sound regression proxy; characters are what the codebase actually budgets in.
    canonical = per_query_ws_tokens if unit == "ws_tokens" else per_query_chars
    canonical_mean = (sum(canonical) / len(canonical)) if canonical else None
    canonical_total = sum(canonical)

    cost = {
        "queries": len(per_query_chars),
        "hits": hits_total,
        "returned_hits": hits_total,
        "unit": unit,
        "retrieved_chars_total": int(sum(per_query_chars)),
        "retrieved_chars_mean": chars_mean,
        "retrieved_chars_p95": percentile(per_query_chars, 95),
        "retrieved_chars_median": median(per_query_chars),
        "retrieved_ws_tokens_total": int(sum(per_query_ws_tokens)),
        "retrieved_ws_tokens_mean": ws_mean,
        "retrieved_ws_tokens_p95": percentile(per_query_ws_tokens, 95),
        # The gated figure, whatever unit was requested.
        "retrieved_total": canonical_total,
        "retrieved_mean": canonical_mean,
        "retrieved_p95": percentile(canonical, 95),
        "chars_per_ws_token": ratio,
        "token_estimate_mean": estimate_tokens(chars_mean, ratio),
        "token_estimate_caveat": TOKEN_ESTIMATE_CAVEAT,
        "tokenizer": tokenizer,
        "redundant_hits": redundant_hits,
        "duplicate_source_rows": duplicate_rows,
        "per_query_chars": per_query_chars,
    }

    mean_chunks = median(chunk_counts)

    # The gate always compares like with like: a baseline recorded in one unit must not
    # be compared against a figure measured in another (C1). Both units are recorded, so
    # the mismatch is detectable rather than silent.
    gate_figure = "retrieved_ws_tokens_mean" if unit == "ws_tokens" else "retrieved_chars_mean"

    gate = Gate()
    if baseline is not None:
        gate_cost(gate, cost=cost, baseline=baseline, figure=gate_figure)
    else:
        # No baseline means no verdict. Recording figures is not the same as passing (B3).
        gate.check(
            gate_figure,
            value=cost[gate_figure],
            bound=None,
            comparison=">",
            reason="no baseline supplied, so no cost verdict is claimed",
        )

    manifest = build_manifest(
        seed=seed,
        requested_n=n,
        n=len(picked),
        ks=list(ks),
        modes=[mode],
        embedder=embedder_label(embedder),
        vector_store="SQLiteVectorStore",
        data_sha256=_file_sha256(data),
        cost_config={
            "unit": unit,
            "chars_per_ws_token": ratio,
            "mean_chunks_per_document": mean_chunks if mean_chunks else 0,
            "retrieval_limit": limit,
            "tokenizer": tokenizer,
        },
    )

    return {
        "axis": "cost",
        "schema": "memoratum-eval-axes-v1",
        "manifest": manifest,
        "cost": cost,
        "gate": gate.as_dict(),
    }


def _format_session(turns: list[dict[str, Any]]) -> str:
    from memoratum.eval_longmemeval import format_session

    return format_session(turns)


def _file_sha256(path: str) -> str:
    from memoratum.eval_datasets import file_sha256

    return file_sha256(path) if os.path.exists(path) else "unavailable"


def summarize(result: dict[str, Any]) -> str:
    cost = result["cost"]
    config = result["manifest"]["cost_config"]
    checks = result.get("gate", {}).get("checks", [])
    ungated = any(c.get("reason", "").startswith("no baseline") for c in checks)
    headline = (
        "NO VERDICT (no baseline supplied)"
        if ungated
        else f"gate: {result['gate']['status'].upper()}"
    )
    lines = [
        "# Token cost per retrieval",
        "",
        headline,
        "",
    ]
    if ungated:
        # B3: a missing baseline fails rather than passing, but "fails" here means
        # "unproven", not "regressed". Saying so prevents an operator reading a fresh
        # baseline-less run as a cost regression.
        lines += [
            (
                "No baseline was supplied, so no pass/fail is claimed. A missing baseline "
                "fails rather than passes (B3), which here means unproven rather than "
                "regressed."
            ),
            "",
        ]
    lines += [
        "## Reported figures (canonical unit: characters)",
        f"- queries: {cost['queries']}",
        f"- hits: {cost['hits']}",
        f"- retrieved_chars_mean: {_fmt(cost['retrieved_chars_mean'])}",
        f"- retrieved_chars_p95: {_fmt(cost['retrieved_chars_p95'])}",
        f"- retrieved_chars_total: {cost['retrieved_chars_total']}",
        f"- retrieved_ws_tokens_mean: {_fmt(cost['retrieved_ws_tokens_mean'])} (companion unit)",
        "",
        "## Redundancy",
        f"- redundant_hits: {cost['redundant_hits']}",
        f"- duplicate_source_rows: {cost['duplicate_source_rows']}",
        "",
        "## Token estimate (derived, never gates)",
        f"- chars_per_ws_token: {_fmt(cost['chars_per_ws_token'])} (measured on this corpus)",
        f"- token_estimate_mean: {_fmt(cost['token_estimate_mean'])}",
        f"- {cost['token_estimate_caveat']}",
        f"- tokenizer: {cost['tokenizer'] or 'none'} (never changes a gated figure)",
        "",
        "## Manifest",
        f"- schema: {result['manifest']['schema']}",
        f"- seed: {result['manifest']['seed']}",
        f"- retrieval_limit: {config['retrieval_limit']} hits",
        f"- mean_chunks_per_document: {_fmt(config['mean_chunks_per_document'])}",
        f"- data_sha256: {result['manifest']['data_sha256'][:16]}",
        "",
    ]
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 100:
        return f"{value:.1f}"
    return f"{value:.3f}"


def _load_baseline(path: str, figure: str) -> dict[str, Any] | None:
    """Read one row from an ``eval/BASELINES.md`` cost table."""
    if not path or not os.path.exists(path):
        return None
    header: list[str] | None = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            header = None
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0] == "axis":
            header = cells
            continue
        if not header or len(cells) != len(header):
            continue
        row = dict(zip(header, cells))
        if row.get("axis") == figure:
            return row
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Token cost axis")
    parser.add_argument("--data", required=True)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--k", default="5,10")
    parser.add_argument("--mode", default="hybrid")
    # No argparse `choices`: the contract requires a domain error naming the allowed
    # units and why bytes is excluded, and argparse's own message says neither.
    parser.add_argument("--cost-unit", default="chars")
    parser.add_argument("--baseline", default="")
    parser.add_argument("--measure-ratio", action="store_true", default=True)
    parser.add_argument("--no-measure-ratio", dest="measure_ratio", action="store_false")
    parser.add_argument("--out-md", default="")
    parser.add_argument("--out-json", default="")
    args = parser.parse_args(argv)

    if not os.path.exists(args.data):
        print(f"corpus not found: {args.data}", file=sys.stderr)
        return EXIT_BAD_INPUT
    if args.n < 1:
        print("--n must be >= 1", file=sys.stderr)
        return EXIT_BAD_INPUT

    baseline = _load_baseline(args.baseline, "retrieved_chars_mean") if args.baseline else None
    if args.baseline and baseline is None:
        print(
            f"no row for 'retrieved_chars_mean' in {args.baseline}; a missing baseline "
            "fails the run rather than passing (B3)",
            file=sys.stderr,
        )
        return EXIT_BAD_INPUT

    try:
        result = evaluate_cost(
            data=args.data,
            n=args.n,
            seed=args.seed,
            ks=iter_ints(args.k),
            mode=args.mode,
            unit=args.cost_unit,
            baseline=baseline,
            measure_ratio=args.measure_ratio,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_INPUT

    write_artifacts(result, summarize(result), out_md=args.out_md, out_json=args.out_json)
    if baseline is None:
        return EXIT_OK
    return EXIT_GATE_FAILED if result["gate"]["status"] == FAIL else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
