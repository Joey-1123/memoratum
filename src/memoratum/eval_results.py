# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One results file showing all four axes alongside recall and MRR (SC-002).

`eval_compare` answers "are these two runs comparable?". This answers a different question:
"what did this evaluation produce, all together?" — and the interesting part is that the
answer is **not one table of six numbers**.

The four axes measure different things and therefore have different manifests: the cost axis
retrieves 20 hits per question while grounding retrieves 10, the isolation axis builds its own
synthetic corpus, and the latency axis builds a size ladder rather than reading one at all.
Printing their figures in one table without saying so would imply a single configuration
produced them all, which is the exact confusion ``eval_compare`` exists to prevent.

So the rollup shows every axis with its own gate and its own provenance, and states per row
whether it is comparable with the others. Two axes that *do* share a manifest are reported as
comparable; that is a fact worth showing, not a blanket disclaimer.

Nothing here uploads anything (FR-010); inputs are local result files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from memoratum.eval_axes import EXIT_BAD_INPUT, EXIT_OK, command

#: Axes the rollup must account for. A missing one is reported as "not run" rather than
#: omitted: silence would read as a pass.
REQUIRED_AXES = ("isolation", "cost", "latency", "grounding")

#: The figure each axis gates on, when the result file does not say so itself. Fallbacks for
#: hand-written fixtures; a real run reports its gate checks and these go unused.
_DEFAULT_GATED_FIGURE = {
    "isolation": "leaked_hits",
    "cost": "retrieved_chars_mean",
    "latency": "us_per_chunk_sublinear",
    "grounding": "grounded_fraction",
}

#: Manifest fields worth showing per row, in this order. Deliberately the same list
#: `eval_compare.COMPARED_KEYS` uses, so a field that blocks a comparison is also visible here.
PROVENANCE_FIELDS = ("embedder", "vector_store", "n", "seed", "data_sha256")

_SHARED_NOTICE = (
    "All rows below were produced by the **same configuration**, so their figures are "
    "comparable with each other."
)

_MIXED_NOTICE = (
    "> ## NOT COMPARABLE\n"
    ">\n"
    "> These figures must not be read against each other. Each axis measures something "
    "different and therefore runs its own configuration: the cost axis retrieves a "
    "different number of hits per question than the grounding axis, the isolation axis "
    "builds a synthetic corpus, and the latency axis builds a size ladder rather than "
    "reading one at all. Every row below carries its own provenance so you can see exactly "
    "which rows may be compared.\n"
    ">\n"
    "> A trustworthy comparison of two runs is `memoratum.eval_compare`, which refuses to "
    "print two figures as a comparison when their manifests disagree."
)

_UNITS_NOTE = (
    "`R@k` is measured in **k retrieval positions**; `MRR` is measured in **distinct "
    "sessions**. They answer different questions and must not be read as the same unit."
)

_V0_NOTE = (
    "> **NOT COMPARABLE — metric definition v0.** These `R@k` values pre-date the "
    "2026-10-04 correction, which slices the hit list before deduplicating. See "
    "[`MIGRATION-metric-v1.md`](../eval/MIGRATION-metric-v1.md)."
)

_NO_RETRIEVAL_NOTE = (
    "**Not available.** No retrieval result file was supplied, so this section claims no "
    "recall figure and no reciprocal-rank figure. Pass `eval_longmemeval.json` (or "
    "LoCoMo / MC10 output) to include them."
)

_FOOTER = (
    "This file is assembled from the axis result files it lists, not measured directly. "
    "Regenerate each axis with the command recorded in its own `RESULTS-*.md`, then re-run "
    "`python -m memoratum.eval_results` over the result JSON files."
)


def _provenance(manifest: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(manifest.get(field, "-")) for field in PROVENANCE_FIELDS)


def _comparable_group(result: dict[str, Any]) -> str:
    """A fingerprint of the configuration, so equal configurations can be shown as equal."""
    return "|".join(_provenance(result.get("manifest") or {}))


def _gated_figure(result: dict[str, Any]) -> str:
    """The figure this axis gates on, taken from its own gate checks when it reports them."""
    checks = (result.get("gate") or {}).get("checks") or []
    if checks:
        return ", ".join(str(check.get("name")) for check in checks)
    return _DEFAULT_GATED_FIGURE.get(str(result.get("axis", "")), "unknown")


def _gated_value(result: dict[str, Any]) -> str:
    checks = (result.get("gate") or {}).get("checks") or []
    values: list[str] = []
    for check in checks:
        value = check.get("value")
        if value is None:
            values.append("-")
        elif isinstance(value, float):
            values.append(f"{value:.4g}")
        else:
            values.append(str(value))
    return ", ".join(values) if values else "-"


def render_rollup(results: list[dict[str, Any]], retrieval: dict[str, Any] | None = None) -> str:
    """Render the combined report.

    ``results`` are the axis payloads in any order; ``retrieval`` is the optional
    LongMemEval/LoCoMo/MC10 runner output. An axis that was never run is reported as
    "NOT RUN" rather than omitted.
    """
    by_axis = {str(result.get("axis", "")): result for result in results}
    present = [by_axis[axis] for axis in REQUIRED_AXES if axis in by_axis]
    extra = [r for r in results if str(r.get("axis", "")) not in REQUIRED_AXES]

    lines: list[str] = ["# Evaluation results — all axes", ""]

    fingerprints = {_comparable_group(r) for r in present}
    if retrieval is not None:
        fingerprints.add(_comparable_group(retrieval))
    shared = len(fingerprints) == 1 and bool(present)
    lines += [(_SHARED_NOTICE if shared else _MIXED_NOTICE), ""]

    lines += [
        "## Axis gates",
        "",
        "| axis | gate | gated figure | value |",
        "|---|---|---|---|",
    ]
    for result in present:
        status = str((result.get("gate") or {}).get("status", "unknown")).upper()
        lines.append(
            f"| {result['axis']} | {status} | {_gated_figure(result)} | {_gated_value(result)} |"
        )
    for axis in REQUIRED_AXES:
        if axis not in by_axis:
            lines.append(f"| {axis} | **NOT RUN** | — | — |")
    for result in extra:
        status = str((result.get("gate") or {}).get("status", "unknown")).upper()
        lines.append(f"| {result.get('axis', '?')} | {status} | {_gated_figure(result)} | — |")

    lines += ["", "## Provenance per row", ""]
    lines.append("| axis | " + " | ".join(PROVENANCE_FIELDS) + " |")
    lines.append("|---|" + "---|" * len(PROVENANCE_FIELDS))
    for result in present:
        lines.append(
            f"| {result['axis']} | " + " | ".join(_provenance(result.get("manifest") or {})) + " |"
        )
    if retrieval is not None:
        lines.append(
            f"| retrieval ({retrieval.get('dataset', 'unknown')}) | "
            + " | ".join(_provenance(retrieval.get("manifest") or {}))
            + " |"
        )
    for axis in REQUIRED_AXES:
        if axis not in by_axis:
            lines.append(f"| {axis} | " + " | ".join(["—"] * len(PROVENANCE_FIELDS)) + " |")

    lines += ["", "## Retrieval metrics", ""]
    if retrieval is None:
        lines.append(_NO_RETRIEVAL_NOTE)
    else:
        lines += [_UNITS_NOTE, ""]
        definition = str(
            (retrieval.get("manifest") or {}).get("metric_definition", "current")
        ).lower()
        if definition == "v0":
            lines += [_V0_NOTE, ""]
        lines += ["| mode | metric | value |", "|---|---|---:|"]
        for mode, metrics in sorted((retrieval.get("aggregate") or {}).items()):
            for metric, value in sorted(metrics.items()):
                shown = f"{value:.4f}" if isinstance(value, float) else str(value)
                lines.append(f"| {mode} | {metric} | {shown} |")

    lines += ["", "## Regenerate this figure", "", _FOOTER]
    return "\n".join(lines) + "\n"


def _command_block(argv: list[str] | None) -> str:
    """The regeneration command, in the shape every other artifact uses (SC-003)."""
    from memoratum.eval_axes import regeneration_block

    return regeneration_block(command(argv, module="memoratum.eval_results")).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show all four evaluation axes and the retrieval metrics together"
    )
    parser.add_argument("results", nargs="+", help="axis and/or retrieval result JSON files")
    parser.add_argument("--out-md", default="")
    args = parser.parse_args(argv)

    if not args.results:
        print("at least one result file is required", file=sys.stderr)
        return EXIT_BAD_INPUT

    payloads: list[dict[str, Any]] = []
    for path in args.results:
        try:
            with open(path, encoding="utf-8") as handle:
                payloads.append(json.load(handle))
        except OSError as exc:
            print(f"could not read {path}: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT
        except json.JSONDecodeError as exc:
            print(f"{path} is not valid JSON: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT

    axes = [p for p in payloads if str(p.get("axis", "")) in REQUIRED_AXES]
    retrieval = next((p for p in payloads if str(p.get("axis", "")) not in REQUIRED_AXES), None)

    report = render_rollup(axes, retrieval)
    print(report)

    if args.out_md:
        try:
            output = Path(args.out_md)
            if str(output.parent):
                os.makedirs(output.parent, exist_ok=True)
            output.write_text(report.rstrip("\n") + "\n\n" + _command_block(argv) + "\n")
        except OSError as exc:
            print(f"could not write {args.out_md}: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
