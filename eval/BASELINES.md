# Evaluation baselines

Reference figures the cost and latency axes gate against. Populated as each axis
lands; rows are added only from a real recorded run.

**Rules**

- A **latency** row without `hardware` is invalid and fails validation
  (`data-model.md` invariant B1, FR-008). Absolute milliseconds are hardware-bound,
  so only ratios and per-unit costs travel between machines.
- A **missing** baseline is `null` and **fails** the gate (B3). It never silently
  passes.
- Every row records the command that regenerates it (FR-007, SC-003).
- `R@k` values are **not comparable** across the v0 → v1 metric change of
  2026-10-04. See [`MIGRATION-metric-v1.md`](./MIGRATION-metric-v1.md).

## Gate semantics

| Axis | Gated figure | Fails when |
|---|---|---|
| Isolation | `leaked_hits` | `> 0` |
| Cost | `retrieved_chars_mean` | `> baseline x (1 + tolerance)` |
| Latency | `us_per_chunk(4N)` ratio | `>= 1.5x` |
| Latency | `median_ms` | `> baseline x 1.40` |
| Grounding | `grounded_fraction` | `< min_grounded_fraction` |

Recall and MRR are report-only and are **not** gated.

## Cost baselines

Populated by T052 — 2026-10-05.

| axis | figure | tolerance | corpus | seed | mode | hardware | command |
|---|---|---|---|---|---|---|---|
| retrieved_chars_mean | 28469.0 | 0.10 | longmemeval-s (n=4, prefix 20) | 42 | hybrid | n/a | `uv run python -m memoratum.eval_cost --data data/longmemeval_s_cleaned.json --n 4 --k 5,10` |

## Latency baselines

Hardware is mandatory for every row in this table.

_Populated by T069._

| axis | figure | tolerance | corpus | seed | mode | hardware | command |
|---|---|---|---|---|---|---|---|

## Status

| Phase | Axis | Baseline state |
|---|---|---|
| A | Foundational | n/a |
| B | Isolation | gate is an absolute count — no baseline needed |
| C | Cost | **measured** — see cost table |
| D | Latency | not yet measured |
| E | Grounding | gate is `grounded_fraction == 1.0` — no baseline needed |
| F | Reproducibility | n/a |