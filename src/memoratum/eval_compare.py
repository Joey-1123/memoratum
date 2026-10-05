# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Compare local evaluation results, and refuse to compare what is not comparable.

The previous version of this module built a Markdown table from whatever results it was
handed. It had no notion of provenance, so a `HashEmbedder` figure and a hosted-embedding
figure appeared in the same table with no distinction — a reader would take the difference
between them as a result. That is the failure ``research.md`` D8 records, and the reason
this module now **hard-fails** on a manifest mismatch instead of warning.

Three rules, all encoded below:

* **A metric difference is the finding; a manifest difference is the blocker.** A gate that
  fired on an improved score would be unusable, so ``value_deltas`` never sets the exit code.
* **A missing key is a mismatch, not agreement.** Absent provenance is not comparable
  provenance, so ``missing on right`` is reported as a mismatch rather than skipped.
* **A metric present on one side only is reported, never silently dropped** — otherwise a
  regression that removes a metric looks like a clean run.

Nothing here uploads anything (FR-010); the inputs are local result files.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from pathlib import Path
from typing import Any

from memoratum.eval_axes import (
    EXIT_BAD_INPUT,
    EXIT_GATE_FAILED,
    EXIT_OK,
)

#: Manifest keys that change what a number *means*. A difference in any of these makes two
#: figures unrelated rather than comparable, so it blocks.
#:
#: `metric_definition` is compared when present: comparing a pre-2026-10-04 `R@k` figure
#: with a post-fix one is precisely the mistake `eval/MIGRATION-metric-v1.md` exists to
#: prevent, and the two cannot be detected from the other keys.
COMPARED_KEYS = (
    "schema",
    "embedder",
    "vector_store",
    "modes",
    "seed",
    "n",
    "data_sha256",
    "metric_definition",
)

#: The per-axis config blocks. A gate bound is part of what a figure means: two runs
#: differing only in `--min-grounded-fraction` are not comparable.
COMPARED_BLOCKS = ("scope_config", "cost_config", "latency_config", "grounding_config")

#: Manifest keys worth *showing* when they differ, without blocking. `requested_n` differs
#: whenever the sample was padded or truncated, which is a sampling note rather than a
#: change of definition, and blocking on it would turn a footnote into a build failure.
REPORTED_KEYS = ("requested_n", "ks")

#: Config-block settings that are recorded but deliberately **do not** gate (invariant G6:
#: an absent judge MUST NOT fail the run). Blocking on one of these would demand a re-run
#: that cannot change any figure, so a difference is reported instead.
NON_GATING_CONFIG_KEYS = {"judge", "tier2_enabled"}

#: Top-level blocks that hold metric values, keyed by the axis they belong to.
_METRIC_BLOCKS = ("grounding", "cost", "latency", "isolation", "retrieval")

#: Deltas below this are rounded away in the report but not in the JSON. A value rounded
#: to 6dp can hide a small real change, so the report rounds and the artifact does not.
_DELTA_EPSILON = 1e-12


# --------------------------------------------------------------------------- #
# Manifest comparison
# --------------------------------------------------------------------------- #


def _describe(value: Any) -> str:
    """Render a manifest value for a report, without collapsing a missing key to "same"."""
    if value is _ABSENT:
        return "<absent>"
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class _Absent:
    """Sentinel for a key that is not present.

    ``None`` cannot be used: a manifest that legitimately records ``null`` for a bound and
    one that omits the key are different states, and M3 requires the distinction.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<absent>"


_ABSENT = _Absent()


def compare_manifests(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Compare two results' manifests. Returns a structured outcome.

    ``comparable`` is False when any compared key or block differs, **including when a key
    is absent on one side only**. Absent provenance is not comparable provenance, so a
    result file that forgot to record its embedder cannot be quietly compared with one that
    did.
    """
    left_manifest = left.get("manifest") or {}
    right_manifest = right.get("manifest") or {}

    mismatches: dict[str, dict[str, str]] = {}
    notes: dict[str, dict[str, str]] = {}

    for key in COMPARED_KEYS:
        lhs = left_manifest.get(key, _ABSENT)
        rhs = right_manifest.get(key, _ABSENT)
        if lhs is _ABSENT and rhs is _ABSENT:
            continue
        if lhs != rhs:
            mismatches[key] = {"left": _describe(lhs), "right": _describe(rhs)}

    for key in REPORTED_KEYS:
        lhs = left_manifest.get(key, _ABSENT)
        rhs = right_manifest.get(key, _ABSENT)
        if lhs != rhs and not (lhs is _ABSENT and rhs is _ABSENT):
            notes[key] = {"left": _describe(lhs), "right": _describe(rhs)}

    for block in COMPARED_BLOCKS:
        lhs = left_manifest.get(block, _ABSENT)
        rhs = right_manifest.get(block, _ABSENT)
        if lhs is _ABSENT and rhs is _ABSENT:
            continue
        gating_left, gating_right = _without_non_gating(lhs, rhs)
        if gating_left != gating_right:
            mismatches[block] = {"left": _describe(lhs), "right": _describe(rhs)}
            continue
        # Same gate, different non-gating settings: record it so the reader can see the
        # runs were not identical, but do not block.
        for setting in _differing_settings(lhs, rhs):
            notes[f"{block}.{setting}"] = {
                "left": _describe(_setting(lhs, setting)),
                "right": _describe(_setting(rhs, setting)),
            }

    comparable = not mismatches
    return {
        "comparable": comparable,
        "mismatches": mismatches,
        "notes": notes,
        "value_deltas": compare_values(left, right),
        "exit_code": EXIT_OK if comparable else EXIT_GATE_FAILED,
    }


def _setting(block: Any, key: str) -> Any:
    return block.get(key, _ABSENT) if isinstance(block, dict) else _ABSENT


def _without_non_gating(left: Any, right: Any) -> tuple[Any, Any]:
    """Two config blocks with the non-gating settings removed.

    Returned unchanged when either side is not a mapping: a non-mapping block is itself a
    mismatch, and quietly stripping keys from it would hide that.
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        return left, right
    strip = {key for key in NON_GATING_CONFIG_KEYS if key in left or key in right}
    if not strip:
        return left, right
    return (
        {k: v for k, v in left.items() if k not in strip},
        {k: v for k, v in right.items() if k not in strip},
    )


def _differing_settings(left: Any, right: Any) -> list[str]:
    """Non-gating settings that differ between two config blocks."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return []
    return [
        key
        for key in NON_GATING_CONFIG_KEYS
        if key in left or key in right
        if left.get(key, _ABSENT) != right.get(key, _ABSENT)
    ]


# --------------------------------------------------------------------------- #
# Metric values
# --------------------------------------------------------------------------- #


def _metric_values(result: dict[str, Any]) -> dict[str, float]:
    """Every numeric metric in one result, keyed by ``axis.metric``.

    Floats and ints only. Counts such as ``leaked_hits`` are included deliberately: the
    isolation axis's gate is an absolute count, so a change in it is a change in severity.
    Booleans are excluded — they are flags, not measurements, and ``True`` vs ``1`` is not
    a finding.
    """
    values: dict[str, float] = {}
    for block in _METRIC_BLOCKS:
        payload = result.get(block)
        if not isinstance(payload, dict):
            continue
        for metric, value in payload.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            values[f"{block}.{metric}"] = float(value)
    for mode, metrics in (result.get("aggregate") or {}).items():
        if not isinstance(metrics, dict):
            continue
        for metric, value in metrics.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            values[f"{mode}.{metric}"] = float(value)
    for metric in ("accuracy", "balanced_accuracy", "n"):
        value = result.get(metric)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values[metric] = float(value)
    return values


def compare_values(left: dict[str, Any], right: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Numeric deltas for metrics present on both sides.

    A metric present on only one side is reported with ``present_on: one side`` and no
    delta, so a removed metric is visible rather than invisible.
    """
    lhs = _metric_values(left)
    rhs = _metric_values(right)
    deltas: dict[str, dict[str, Any]] = {}
    for metric in sorted(set(lhs) | set(rhs)):
        if metric not in lhs or metric not in rhs:
            deltas[metric] = {
                "present_on": "left" if metric in lhs else "right",
                "change": None,
                "direction": "missing",
            }
            continue
        change = rhs[metric] - lhs[metric]
        if abs(change) <= _DELTA_EPSILON:
            direction = "same"
        else:
            direction = "up" if change > 0 else "down"
        deltas[metric] = {
            "before": lhs[metric],
            "after": rhs[metric],
            "change": change,
            "direction": direction,
        }
    return deltas


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def render_report(outcome: dict[str, Any], results: list[dict[str, Any]]) -> str:
    """Render the comparison. The verdict comes first, deliberately.

    A reader scanning a table of numbers is the failure mode this module exists to prevent,
    so "NOT COMPARABLE" and the offending keys appear before any figure.
    """
    lines: list[str] = []
    if outcome["comparable"]:
        lines += ["# Evaluation results", "", "manifests comparable: **yes**", ""]
    else:
        lines += [
            "# Evaluation results",
            "",
            "## NOT COMPARABLE — these figures must not be read against each other",
            "",
            "The manifests below differ. The metric values are printed for reference only;",
            "a difference between them is not a result. Regenerate both runs from the same",
            "configuration, or compare them against a recorded baseline.",
            "",
            "| key | left | right |",
            "|---|---|---|",
        ]
        for key in sorted(outcome["mismatches"]):
            entry = outcome["mismatches"][key]
            lines.append(f"| `{key}` | `{entry['left']}` | `{entry['right']}` |")

    if outcome["notes"]:
        lines += ["", "### Differences that do not block", ""]
        for key in sorted(outcome["notes"]):
            entry = outcome["notes"][key]
            lines.append(f"- `{key}`: `{entry['left']}` vs `{entry['right']}`")

    lines += ["", "## Figures", "", "| axis | dataset | n | gate |", "|---|---|---:|---|"]
    for result in results:
        gate = (result.get("gate") or {}).get("status", "n/a")
        lines.append(
            f"| {result.get('axis', 'unknown')} | {result.get('dataset', 'unknown')} "
            f"| {result.get('n', 'n/a')} | {gate} |"
        )

    deltas = outcome["value_deltas"]
    changed = {k: v for k, v in deltas.items() if v["direction"] != "same"}
    if changed:
        lines += [
            "",
            "## Value deltas",
            "",
            "| metric | before | after | change | direction |",
            "|---|---:|---:|---:|---|",
        ]
        for metric in sorted(changed):
            entry = changed[metric]
            if entry["direction"] == "missing":
                lines.append(f"| `{metric}` | — | — | — | only on the {entry['present_on']} side |")
                continue
            lines.append(
                f"| `{metric}` | {entry['before']:.6f} | {entry['after']:.6f} "
                f"| {entry['change']:+.6f} | {entry['direction']} |"
            )
    else:
        lines += ["", "## Value deltas", "", "none — every shared metric is unchanged."]

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare local evaluation results; fail on a manifest mismatch"
    )
    parser.add_argument("results", nargs="+")
    parser.add_argument("--out-md", default="")
    parser.add_argument("--out-json", default="")
    parser.add_argument(
        "--allow-mismatch",
        action="store_true",
        help="report a mismatch but exit 0 (for documenting a known, deliberate difference)",
    )
    args = parser.parse_args(argv)

    loaded: list[dict[str, Any]] = []
    for path in args.results:
        try:
            with open(path, encoding="utf-8") as handle:
                loaded.append(json.load(handle))
        except OSError as exc:
            print(f"could not read {path}: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT
        except json.JSONDecodeError as exc:
            print(f"{path} is not valid JSON: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT

    # Each adjacent pair is compared, so a three-file run reports every mismatch rather
    # than only the first.
    outcomes = [compare_manifests(a, b) for a, b in itertools.pairwise(loaded)]
    mismatches = {
        key: entry for outcome in outcomes for key, entry in outcome["mismatches"].items()
    }
    notes = {key: entry for outcome in outcomes for key, entry in outcome["notes"].items()}
    combined = {
        "comparable": not mismatches,
        "mismatches": mismatches,
        "notes": notes,
        "value_deltas": {},
        "exit_code": EXIT_OK if not mismatches else EXIT_GATE_FAILED,
    }
    for outcome in outcomes:
        for metric, entry in outcome["value_deltas"].items():
            combined["value_deltas"].setdefault(metric, entry)

    report = render_report(combined, loaded)
    print(report)

    if args.out_md:
        try:
            output = Path(args.out_md)
            if output.parent and str(output.parent):
                os.makedirs(output.parent, exist_ok=True)
            output.write_text(report)
        except OSError as exc:
            print(f"could not write {args.out_md}: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT
    if args.out_json:
        try:
            output = Path(args.out_json)
            if output.parent and str(output.parent):
                os.makedirs(output.parent, exist_ok=True)
            output.write_text(json.dumps(combined, indent=2, ensure_ascii=False))
        except OSError as exc:
            print(f"could not write {args.out_json}: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT

    if not combined["comparable"] and not args.allow_mismatch:
        return EXIT_GATE_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
