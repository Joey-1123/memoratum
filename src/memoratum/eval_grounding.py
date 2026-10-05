# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Grounding axis: is returned content traceable to the ingested corpus?

For a retrieval-backed store the ground truth is its own corpus, so grounding is
checkable deterministically -- no external judge required (FR-005). An external judge may
be attached as an explicitly opt-in, non-gating extra (FR-006).

The rules below are measured, not stylistic -- see ``research.md`` D4 and
``data-model.md`` G1-G6:

* **Provenance by row identity, not text overlap.** Measured: text reassembled from a
  document's own words but absent from it scores token-set containment **1.000** --
  identical to a perfect prefix truncation -- while genuinely unrelated documents reach
  **0.739**, inside any sane 0.8 gate. Set overlap cannot separate grounded from
  fabricated, which eliminates the most commonly proposed design.
* **Grade chunk text against ``chunks.text``, never ``documents.content``.** Over 5,268
  real chunks, exact substring match against the parent document is 99.7%, and the
  failures are *catastrophic, not graceful*: the longest in-document prefix was 68 of
  1,498 characters (5%). ``split_markdown`` rewrites every heading level to ``# {h}``, so
  a per-item containment ratio would score a perfectly grounded chunk at 0.05.
* **One global rule is invalid.** The fact leg renders ``subject predicate object``, which
  is not a substring of the corpus, so a single threshold would score the fact leg 0.0 and
  the document leg 1.0 for *identical evidence*.
* **Tier 2 diagnostics never gate**, and **an absent judge never fails the run**.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import unicodedata
from collections.abc import Sequence
from typing import Any

from memoratum import db as db_module
from memoratum.db import fts_query
from memoratum.embeddings import HashEmbedder
from memoratum.eval_axes import (
    EXIT_BAD_INPUT,
    EXIT_GATE_FAILED,
    EXIT_OK,
    EXIT_SELF_CHECK_FAILED,
    FAIL,
    Gate,
    ManifestError,
    attribute_hits,
    build_manifest,
    command,
    embedder_label,
    iter_ints,
    write_artifacts,
)
from memoratum.eval_datasets import iter_records
from memoratum.ingest import process_all
from memoratum.search import search

#: Recorded in the manifest so a figure is reproducible (FR-007).
NORMALIZATION = "nfkc+whitespace+casefold"

#: The per-kind grounding rules, quoted verbatim in the manifest. Mandatory rather than
#: optional: one global rule is invalid (G2).
RULE_BY_KIND = {
    "chunk": "chunks.text",
    "memory": "memories.text",
    "fact": "subject predicate object",
}

INJECTED_TEXT = "a sentence that appears in no ingested document anywhere"


def normalize_text(text: str) -> str:
    """NFKC, whitespace collapsed, casefolded.

    NFKC expands compatibility forms (the ``fi`` ligature becomes ``fi``), so text that
    differs only in rendering compares equal. Measured to make no difference on this
    corpus, retained as defence against folding differences.
    """
    return " ".join(unicodedata.normalize("NFKC", text or "").split()).casefold()


def _hit_text(hit: dict[str, Any]) -> str:
    """The text the retriever actually returned, regardless of kind."""
    return str(hit.get("chunk") or hit.get("memory") or "")


def ground_hit(hit: dict[str, Any], attributed: Any) -> tuple[Any | None, str | None]:
    """Confirm one hit came from this corpus. Returns ``(attribution, reason)``.

    ``attributed`` is passed in rather than re-derived, so grounding costs exactly one
    keyed lookup per hit however many times it is called.

    Two conditions must both hold, and the second is what makes the first falsifiable:

    1. **Row identity.** The hit id resolves to an ingested row.
    2. **Text identity under that kind's rule.** The text the retriever *returned* is
       exactly the text of that row, compared after normalisation. Comparing the row's
       own text to itself would be tautological -- it would report every fabricated
       passage as grounded, and no test of the axis could fail.

    Only row identity is needed, and no corpus-wide text index: a row that came out of
    the database *is* corpus membership by definition, so a second substring scan over
    the corpus can never fail where the first succeeded. That scan cost 2.56 ms/hit
    median (70.6 ms worst) over 5,268 real chunks for no additional detection, and an
    earlier draft of this module measured a "66x" index speedup that does not hold once
    the redundant scan is removed.

    Grading chunk text against ``documents.content`` instead would be catastrophic
    rather than graceful: ``split_markdown`` rewrites every heading level to ``# {h}``,
    and over this repository's 596 markdown chunks 138 (23.2%) are not exact substrings
    of their parent document, the worst with a 1% in-document prefix (G1).
    """
    if attributed.hit_kind == "unresolvable":
        return None, "the hit id resolves to no ingested row, so it cannot be grounded"

    returned = _hit_text(hit)
    if not normalize_text(returned):
        return None, "the hit carries no text to trace"

    expected = attributed.row_text or ""
    if normalize_text(returned) != normalize_text(expected):
        return (
            None,
            (
                f"the text returned does not match this {attributed.hit_kind} row "
                f"under its rule ({RULE_BY_KIND[attributed.hit_kind]})"
            ),
        )

    return attributed, None


def tier2_diagnostics(conn: sqlite3.Connection, query: str, *, container_tag: str) -> dict:
    """Diagnostic-only evidence. NEVER gates (G3).

    FTS5 ``snippet()`` through stdlib sqlite3 is the best available zero-dependency proof
    that tokens reached the index -- it marks the matched tokens in the stored text, which
    is exactly the "did these tokens make it into the index" evidence a grounding report
    should carry. ``matchinfo()`` does not work in a SELECT context; ``bm25()``/``rank``
    do.

    The MATCH expression comes from ``db.fts_query``, the same sanitiser ``keyword_search``
    uses. Handing FTS5 the raw question is a syntax error the moment it contains punctuation
    or a reserved word -- measured, ``MATCH 'what is scheduled for tuesday?'`` raises
    ``fts5: syntax error near "?"`` and this diagnostic silently reported null.
    """
    diagnostics: dict[str, Any] = {}
    match = fts_query(query)
    diagnostics["fts5_query"] = match
    if match is None:
        diagnostics["fts5_snippet"] = None
        return diagnostics
    # No try/except: `db.connect` creates `chunks_fts` unconditionally, so an FTS5
    # failure here is a real defect, and swallowing it into a null would report a broken
    # index as a diagnostic that simply found nothing.
    row = conn.execute(
        "SELECT snippet(chunks_fts, 0, '[', ']', '...', 8) AS s"
        " FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT 1",
        (match,),
    ).fetchone()
    diagnostics["fts5_snippet"] = row["s"] if row and row["s"] else None
    return diagnostics


def evaluate_grounding(
    conn: sqlite3.Connection,
    *,
    hits: Sequence[dict[str, Any]],
    container_tag: str,
    project_id: str | None = None,
    tier2: bool = False,
    judge: str | None = None,
    query: str = "quarterly budget review",
    min_grounded_fraction: float = 1.0,
) -> dict[str, Any]:
    """Report the fraction of hits traceable to the ingested corpus (FR-005).

    ``min_grounded_fraction`` defaults to 1.0 because every hit SHOULD be traceable; a
    grounded answer built from the wrong documents is caught by the isolation and recall
    axes, not here (G5).

    ``judge`` accepts ``None``/``""``/``"none"`` as "no judge", normalized here rather
    than only in the CLI so a programmatic caller cannot install an external judge by
    passing the CLI's own sentinel (G6).
    """
    if judge in ("", "none", "None"):
        judge = None

    attributions = attribute_hits(conn, hits)
    # The three declared kinds are always present, so an unexercised one reports
    # checked 0 / fraction null rather than vanishing from the report (M3).
    per_kind: dict[str, dict[str, int]] = {
        kind: {"checked": 0, "grounded": 0} for kind in RULE_BY_KIND
    }
    ungrounded: list[dict[str, Any]] = []
    unresolvable = 0
    grounded_hits = 0

    for rank, (hit, attributed) in enumerate(zip(hits, attributions, strict=True)):
        kind = attributed.hit_kind
        counts = per_kind.setdefault(kind, {"checked": 0, "grounded": 0})
        counts["checked"] += 1

        resolved, reason = ground_hit(hit, attributed)
        if resolved is not None:
            counts["grounded"] += 1
            grounded_hits += 1
            continue

        if kind == "unresolvable":
            unresolvable += 1
        ungrounded.append(
            {
                "hit_id": str(hit.get("id", "")),
                "hit_kind": kind,
                "rank": rank,
                "reason": reason,
                "row_text_excerpt": _excerpt(hit),
            }
        )

    checked = len(hits)
    fraction = (grounded_hits / checked) if checked else None

    by_kind = {
        kind: {
            "checked": counts["checked"],
            "grounded": counts["grounded"],
            # null, never 0.0: a kind that was not exercised is not a passing kind (M3).
            "fraction": (counts["grounded"] / counts["checked"]) if counts["checked"] else None,
        }
        for kind, counts in per_kind.items()
    }

    grounding = {
        "hits_checked": checked,
        "grounded_hits": grounded_hits,
        "ungrounded_hits": checked - grounded_hits,
        "unresolvable_hits": unresolvable,
        "grounded_fraction": fraction,
        "by_kind": by_kind,
        "ungrounded": ungrounded,
        "tier2_diagnostics": tier2_diagnostics(conn, query, container_tag=container_tag)
        if tier2
        else None,
        # An absent judge MUST NOT fail the run (FR-006, G6). When present it is reported
        # here and never enters the gate.
        "judge": judge,
        # Rows actually resolved and compared, so a report showing "0 rows" cannot be
        # mistaken for a clean run.
        "rows_resolved": sum(c["checked"] for c in per_kind.values()) - unresolvable,
    }

    gate = Gate()
    gate.check(
        "grounded_fraction",
        value=fraction,
        bound=min_grounded_fraction,
        comparison="<",
        reason=(
            "every hit must trace to an ingested row; a hit that cannot be attributed or "
            "matched is ungrounded (FR-005)"
        ),
    )

    manifest = build_manifest(
        seed=42,
        requested_n=1,
        n=checked,
        ks=list(iter_ints("5,10")),
        modes=["scoped-probe"],
        embedder=embedder_label(HashEmbedder(dims=64)),
        vector_store="sqlite",
        # Overwritten by the CLI with the real corpus hash. The programmatic default is
        # labelled so a caller who forgets cannot produce a hash-looking placeholder that
        # a later comparison would treat as provenance (M2).
        data_sha256="unavailable: evaluate_grounding() called directly",
        grounding_config={
            "normalization": NORMALIZATION,
            "rule_by_kind": dict(RULE_BY_KIND),
            "tier2_enabled": tier2,
            "judge": judge,
            "min_grounded_fraction": min_grounded_fraction,
        },
    )

    return {
        "axis": "grounding",
        "schema": "memoratum-eval-axes-v1",
        "manifest": manifest,
        "grounding": grounding,
        "gate": gate.as_dict(),
    }


def _excerpt(hit: dict[str, Any], limit: int = 120) -> str:
    text = str(hit.get("chunk") or hit.get("memory") or "")
    return text[:limit]


# --------------------------------------------------------------------------- #
# Falsifiability self-check
# --------------------------------------------------------------------------- #


def _injected_hit(conn: sqlite3.Connection) -> dict[str, Any]:
    """A hit under a REAL row id whose text exists in no ingested row.

    Using an unresolvable id would let the row-identity check catch the injection, which
    would prove nothing about text grounding. Using a real id means the ONLY thing that
    can fail is the text comparison, which is what SC-007 is about.
    """
    row = conn.execute("SELECT id FROM chunks ORDER BY id LIMIT 1").fetchone()
    if row is None:
        row = conn.execute("SELECT id FROM memories ORDER BY id LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("cannot run the grounding self-check against an empty corpus")
        return {"id": row["id"], "memory": INJECTED_TEXT, "similarity": 1.0}
    return {"id": f"chunk_{row['id']}", "chunk": INJECTED_TEXT, "similarity": 1.0}


def run_self_check(
    conn: sqlite3.Connection,
    *,
    container_tag: str,
    project_id: str | None = None,
    query: str = "quarterly budget review",
    force_clean: bool = False,
    force_noisy: bool = False,
) -> Any:
    """Inject text in no ingested row and prove the metric reports it (SC-007).

    Two failure modes must both be caught, and each needs its own control:

    ``force_clean``
        A detector that reports everything grounded. This is the dangerous one: it would
        report success forever.
    ``force_noisy``
        A detector that reports everything ungrounded. This is why a self-check must not
        merely confirm detection -- a metric that fails everything teaches operators to
        ignore the gate, and would take a correct implementation down with it.
    """
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Outcome:
        detected: bool
        injected_ungrounded: int
        clean_ungrounded: int
        exit_code: int
        detail: str

    injected_hit = _injected_hit(conn)
    if force_clean or force_noisy:
        # Simulate a broken detector. Without these controls the self-check cannot be shown
        # to FAIL in either direction, so a neutered metric would pass CI forever.
        original = ground_hit
        if force_clean:
            globals()["ground_hit"] = lambda _hit, attributed: (attributed, None)
        else:
            globals()["ground_hit"] = lambda _hit, attributed: (
                None,
                "this detector reports every hit as ungrounded",
            )
        try:
            injected = evaluate_grounding(
                conn, hits=[injected_hit], container_tag=container_tag, project_id=project_id
            )
            clean = evaluate_grounding(
                conn,
                hits=search(
                    conn,
                    HashEmbedder(dims=64),
                    query,
                    container_tag=container_tag,
                    project_id=project_id,
                    limit=5,
                ),
                container_tag=container_tag,
                project_id=project_id,
            )
        finally:
            globals()["ground_hit"] = original
    else:
        injected = evaluate_grounding(
            conn, hits=[injected_hit], container_tag=container_tag, project_id=project_id
        )
        clean = evaluate_grounding(
            conn,
            hits=search(
                conn,
                HashEmbedder(dims=64),
                query,
                container_tag=container_tag,
                project_id=project_id,
                limit=5,
            ),
            container_tag=container_tag,
            project_id=project_id,
        )

    injected_count = injected["grounding"]["ungrounded_hits"]
    clean_count = clean["grounding"]["ungrounded_hits"]

    if injected_count <= 0:
        return Outcome(
            detected=False,
            injected_ungrounded=0,
            clean_ungrounded=clean_count,
            exit_code=EXIT_SELF_CHECK_FAILED,
            detail=(
                "text present in no ingested row was reported as grounded, so a clean "
                "result in this run means nothing (SC-007)"
            ),
        )
    if clean_count != 0:
        return Outcome(
            detected=True,
            injected_ungrounded=injected_count,
            clean_ungrounded=clean_count,
            exit_code=EXIT_SELF_CHECK_FAILED,
            detail=(
                f"{clean_count} correctly grounded hits were reported ungrounded, so the "
                "metric would fail every real run (false positives)"
            ),
        )
    return Outcome(
        detected=True,
        injected_ungrounded=injected_count,
        clean_ungrounded=0,
        exit_code=EXIT_OK,
        detail=(
            f"detection proven: {injected_count} injected ungrounded hit(s) found, 0 false "
            "positives on real hits"
        ),
    )


# --------------------------------------------------------------------------- #
# Report and CLI
# --------------------------------------------------------------------------- #


def summarize(result: dict[str, Any], *, self_check: Any | None = None) -> str:
    grounding = result["grounding"]
    lines = [
        "# Grounding",
        "",
        f"gate: {result['gate']['status'].upper()}",
        "",
        f"- questions evaluated: {result.get('questions_evaluated', 1)}",
        f"- hits_checked: {grounding['hits_checked']}",
        f"- grounded_hits: {grounding['grounded_hits']}",
        f"- ungrounded_hits: {grounding['ungrounded_hits']}",
        f"- unresolvable_hits: {grounding['unresolvable_hits']}",
        f"- grounded_fraction: {_fmt(grounding['grounded_fraction'])}",
        f"- rows resolved: {grounding['rows_resolved']} (one keyed lookup per hit)",
        "",
        "## Per-kind rules",
        "",
        f"- normalization: {result['manifest']['grounding_config']['normalization']}",
    ]
    for kind, rule in sorted(RULE_BY_KIND.items()):
        counts = grounding["by_kind"].get(kind, {})
        lines.append(
            f"- {kind}: {rule} | checked {counts.get('checked', 0)}, "
            f"grounded {counts.get('grounded', 0)}, fraction {_fmt(counts.get('fraction'))}"
        )
    if grounding["ungrounded"]:
        lines += ["", "## Ungrounded hits", ""]
        for item in grounding["ungrounded"][:20]:
            lines.append(
                f"- rank {item['rank']}: {item['hit_id']} ({item['hit_kind']}) — {item['reason']}"
            )
            if item["row_text_excerpt"]:
                lines.append(f"  text: {item['row_text_excerpt']!r}")
        if len(grounding["ungrounded"]) > 20:
            lines.append(f"- ... and {len(grounding['ungrounded']) - 20} more")
    if grounding["tier2_diagnostics"]:
        lines += [
            "",
            "## Tier 2 diagnostics (never gate)",
            f"- fts5_snippet: {grounding['tier2_diagnostics'].get('fts5_snippet')!r}",
        ]
    lines += [
        "",
        "Grounding proves **provenance only** — that a hit came from this corpus. It never",
        "establishes correctness or relevance; recall and MRR own those, and a grounded",
        "answer built from the wrong document is caught by the isolation and recall axes.",
        f"- external judge: {grounding['judge'] or 'none (optional, non-gating)'}",
    ]
    if self_check is not None:
        lines += [
            "",
            "## Falsifiability self-check",
            f"- detected injected ungrounded text: {self_check.detected}",
            f"- injected_ungrounded: {self_check.injected_ungrounded}",
            f"- false positives on real hits: {self_check.clean_ungrounded}",
            f"- {self_check.detail}",
        ]
    return "\n".join(lines) + "\n"


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _command(argv: list[str] | None) -> list[str]:
    return command(argv, module="memoratum.eval_grounding")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grounding axis")
    parser.add_argument("--data", required=True)
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--k", default="5,10")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--judge", default="none")
    parser.add_argument("--tier2", action="store_true")
    parser.add_argument("--inject-ungrounded", action="store_true")
    parser.add_argument("--min-grounded-fraction", type=float, default=1.0)
    parser.add_argument("--out-md", default="")
    parser.add_argument("--out-json", default="")
    args = parser.parse_args(argv)

    if not os.path.exists(args.data):
        print(f"corpus not found: {args.data}", file=sys.stderr)
        return EXIT_BAD_INPUT
    if args.n < 1:
        print("--n must be >= 1", file=sys.stderr)
        return EXIT_BAD_INPUT

    judge = None if args.judge in ("", "none", None) else args.judge

    try:
        records = iter_records(args.data, limit=args.n)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_INPUT
    if not records:
        print(f"no records found in {args.data}", file=sys.stderr)
        return EXIT_BAD_INPUT

    from memoratum.eval_datasets import normalize_longmemeval

    try:
        questions = normalize_longmemeval(records)
    except (ValueError, TypeError, AttributeError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_INPUT
    # No separate "no usable questions" check: normalize_longmemeval appends one entry per
    # record or raises, so an empty result here can only mean empty `records`, already
    # handled above. A second branch could never fire.

    directory = tempfile.TemporaryDirectory(prefix="memoratum-grounding-")
    conn = None
    try:
        from memoratum.eval_longmemeval import format_session

        conn = db_module.connect(os.path.join(directory.name, "grounding.db"))
        embedder = HashEmbedder(dims=64)

        # Every requested question's sessions go into ONE corpus, so a hit from question 3
        # is resolved against a corpus that also contains question 1. Grounding a hit
        # against only its own question's haystack would make the metric trivially true.
        evaluated = list(questions)
        for index, question in enumerate(evaluated):
            for session in question["sessions"]:
                db_module.create_document(
                    conn,
                    container_tag="bench",
                    content=format_session(session["turns"]),
                    custom_id=f"q{index}-{session['session_id']}",
                )
        process_all(conn, embedder)

        ks = iter_ints(args.k)
        limit = max(ks, default=10)
        all_hits: list[dict[str, Any]] = []
        first_query = evaluated[0]["question"]
        for question in evaluated:
            all_hits.extend(
                search(
                    conn,
                    embedder,
                    question["question"],
                    container_tag="bench",
                    limit=limit,
                )
            )

        result = evaluate_grounding(
            conn,
            hits=all_hits,
            container_tag="bench",
            tier2=args.tier2,
            judge=judge,
            query=first_query,
            min_grounded_fraction=args.min_grounded_fraction,
        )
        from memoratum.eval_datasets import file_sha256

        result["manifest"]["seed"] = args.seed
        result["manifest"]["ks"] = ks
        result["manifest"]["data_sha256"] = file_sha256(args.data)
        result["manifest"]["requested_n"] = args.n
        result["manifest"]["n"] = len(evaluated)
        result["questions_evaluated"] = len(evaluated)
        result["hit_limit"] = limit

        self_check = None
        if args.inject_ungrounded:
            self_check = run_self_check(conn, container_tag="bench", query=first_query)
            result["self_check"] = {
                "detected": self_check.detected,
                "injected_ungrounded": self_check.injected_ungrounded,
                "clean_ungrounded": self_check.clean_ungrounded,
                "detail": self_check.detail,
            }

        try:
            write_artifacts(
                result,
                summarize(result, self_check=self_check),
                out_md=args.out_md,
                out_json=args.out_json,
                argv=_command(argv),
            )
        except OSError as exc:
            print(f"could not write results: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT

        # A broken metric outranks a real ungrounded hit (SC-007).
        if self_check is not None and self_check.exit_code != EXIT_OK:
            return self_check.exit_code
        return EXIT_GATE_FAILED if result["gate"]["status"] == FAIL else EXIT_OK
    except (ManifestError, ValueError, TypeError, OSError, AttributeError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_INPUT
    finally:
        if conn is not None:
            conn.close()
        directory.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
