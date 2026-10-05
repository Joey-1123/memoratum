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

**These figures were re-recorded on 2026-10-05 at `--samples 120`.** The earlier rows were
measured at `--samples 30`, and at that sample count the gate was failing on **unmodified
code**: ten identical runs of the 400-chunk retrieve median spanned 12.28–26.45 ms
(**2.15x**), the per-chunk ratio crossed its own bound in 2 runs of 10, and the ms smoke
bound below was exceeded 1 run in 6. At 120 samples the same bound was exceeded 0 runs in 6
and the per-chunk figure spread fell to 1.03x.

The original sample floor of 20 came from bootstrapping median-of-n against the true median
over 2,000 resamples. That estimated *within-run sampling* error and was structurally blind
to *between-run machine* noise — which is the larger term here. The floor is now
`MIN_LATENCY_SAMPLES = 120`, set from the measurement rather than from the bootstrap.

| axis | figure | tolerance | corpus | seed | mode | hardware | command |
|---|---|---|---|---|---|---|---|
| retrieve_median_ms | 13.35 | 0.40 | synthetic ladder 100,200,400 chunks | 42 | documents | Intel(R) Core(TM) i3-7020U CPU @ 2.30GHz / 4c / 3772MB / py3.12.14 | `python -m memoratum.eval_latency --ladder 100,200,400 --samples 120 --warmup 5 --seed 42` |
| us_per_chunk_ratio | 0.93 | 1.22 | synthetic ladder 100,200,400 chunks | 42 | documents | Intel(R) Core(TM) i3-7020U CPU @ 2.30GHz / 4c / 3772MB / py3.12.14 | (same run; ratio of the 400-chunk to the 100-chunk figure) |

`us_per_chunk_ratio` is unchanged at 0.93 across the re-recording, which is the useful
result: the *primary* gate — the one whose direction travels between machines — was stable
while the millisecond figure moved from 11.09 to 13.35. That is exactly the split the axis
was designed around, now demonstrated on this hardware rather than asserted.

## Status

| Phase | Axis | Baseline state |
|---|---|---|
| A | Foundational | n/a |
| B | Isolation | gate is an absolute count — no baseline needed |
| C | Cost | **measured** — see cost table |
| D | Latency | **measured** - see latency table |
| E | Grounding | gate is `grounded_fraction == 1.0` — no baseline needed |
| F | Reproducibility | n/a |