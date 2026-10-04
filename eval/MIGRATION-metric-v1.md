# Metric migration: retrieval recall v0 → v1

**Date**: 2026-10-04 | **Branch**: `002-evaluation-harness`
**Spec**: [`specs/002-evaluation-harness/research.md`](../specs/002-evaluation-harness/research.md) (D7)

## What changed

`partial_recall` and `full_recall` in `src/memoratum/eval_metrics.py` applied the
depth slice **after** deduplication:

```python
top = set(session_ids_of(ranked)[:k])  # v0 — slice AFTER dedup
```

They now apply it **before**:

```python
top = set(session_ids_of(ranked[:k]))  # v1 — slice BEFORE dedup
```

`ranked` is one session id per retrieved **hit**, in rank order. So:

| | Question actually answered |
|---|---|
| **v0** | "are there `k` distinct sessions anywhere in the retrieved set?" |
| **v1** | "was a gold session among the first `k` retrieval positions?" |

`mrr` is **unchanged** — it has no `k` and remains a session-space metric. That is
deliberate, and `tests/test_eval_metrics.py::test_mrr_still_ranks_over_distinct_sessions`
pins it.

## Why the comparison has three columns, not two

While verifying the fix I found that the committed results **do not reproduce
against current `main`**, for a reason unrelated to this fix.

`MRR` is untouched by this change. Measured across runs:

| n | committed MRR | MRR on `main` today | MRR with v0 code restored |
|---|---:|---:|---:|
| 10 | 0.599 | 0.583 | 0.583 |
| 50 | 0.509 | 0.489 | 0.489 |

Restoring the v0 metric code reproduces today's MRR exactly, so the metric fix is
provably not the cause. The gap comes from **feature 001's search work** — Stage 1
(reuse persisted embeddings) and Stage 2 (FTS5 bounds the SELECT and vector leg) —
which landed after these files were generated and changed what gets retrieved.

A two-column "old vs new" table would silently attribute that search regression to
the metric fix. It is therefore split three ways.

## Measurements

LongMemEval-S, `--seed 42`, `--k 5,10`, mode `hybrid`, full harness aggregates
(not recomputed subsets), corpus `data/longmemeval_s_cleaned.json`.

### n = 10

| metric | v0 metric + old search *(committed)* | v0 metric + current search | **v1 metric + current search** | metric Δ | search Δ |
|---|---:|---:|---:|---:|---:|
| partial-R@5 | 0.800 | 0.800 | **0.800** | 0.000 | 0.000 |
| full-R@5 | 0.500 | 0.500 | **0.500** | 0.000 | 0.000 |
| partial-R@10 | 0.800 | 0.800 | **0.800** | 0.000 | 0.000 |
| full-R@10 | 0.500 | 0.500 | **0.500** | 0.000 | 0.000 |
| MRR | 0.599 | 0.583 | **0.583** | 0.000 | −0.016 |

### n = 50

| metric | v0 metric + old search *(committed)* | v0 metric + current search | **v1 metric + current search** | metric Δ | search Δ |
|---|---:|---:|---:|---:|---:|
| partial-R@5 | 0.780 | 0.740 | **0.740** | 0.000 | −0.040 |
| full-R@5 | 0.360 | 0.320 | **0.311** | −0.009 | −0.040 |
| partial-R@10 | 0.880 | 0.780 | **0.740** | −0.040 | −0.100 |
| full-R@10 | 0.640 | 0.480 | **0.467** | −0.013 | −0.160 |
| MRR | 0.509 | 0.489 | **0.489** | 0.000 | −0.020 |

## What this actually shows, stated plainly

**The metric fix has a small effect on values and a large effect on meaning.**

- At **n=10 the fix changes nothing at all** — every delta is 0.000.
- At **n=50** the largest metric-only change is **−0.040** (`partial-R@10`).
- The **search change is the larger effect**, reaching **−0.160** on `full-R@10`.

The fix is still correct — v0 was measuring the wrong thing — but it is not the
large-value correction it might appear to be.

### Correcting my own earlier evidence

I previously cited `eval/RESULTS-n10.md` showing `partial-R@5 == partial-R@10` as the
*symptom* of this defect. **That was wrong.** Under v1 the equality persists at
n=10 (0.800 / 0.800), so the equality is not caused by the deduplication defect. The
ranking simply has a sharp head on this corpus: gold sessions are either within the
first few chunks or absent from all 20 retrieved.

The genuine signature of the defect is different, and it shows up at n=50:

> **v0 `partial-R@10` (0.780) exceeded v0 `partial-R@5` (0.740) by crediting a gold
> session found *beyond* the 5th hit.**

That gap is an artefact. v1 removes it: `partial-R@5` and `partial-R@10` are both
0.740, because at this depth limit nothing new enters between hits 5 and 10.

The committed n=50 file's apparent `0.780 → 0.880` climb was therefore **partly
this artefact** — `+0.100` of reported improvement, of which `0.040` was
unearned.

### Scores can only move down

The set of sessions visible to `recall@k` under v1 is always a **subset** of the set
visible under v0: v1 sees only the first `k` hits, so it can reach fewer distinct
sessions. v0 reaching further is exactly the bug. Therefore a **decrease after this
change is a correction, not a retrieval regression**, and it must never be
"fixed" by re-inflating the retrieval budget.

Pinned by `tests/test_eval_metrics.py::test_recall_at_k_can_only_decrease_versus_the_old_definition`.

## Reproducing these numbers

```bash
# v1 (current code)
uv run python -m memoratum.eval_longmemeval \
  --data data/longmemeval_s_cleaned.json --n 50 --seed 42 --k 5,10 --modes hybrid

# v0 metric, current search
git stash push src/memoratum/eval_metrics.py
uv run python -m memoratum.eval_longmemeval \
  --data data/longmemeval_s_cleaned.json --n 50 --seed 42 --k 5,10 --modes hybrid
git stash pop
```

The v0 + old search column can only be read from the committed files — it is not
regenerable on `main`, which is the reproducibility gap noted above.

## Consequences

1. **Committed results are marked v0** and retained verbatim as a historical
   record. Their numbers were not edited.
2. **`R@k` values before and after this change are not comparable.** Any trend,
   baseline or gate written against a v0 number is invalid.
3. **New baselines must be recorded against v1**, with the corpus, seed, mode and
   hardware stated, per FR-008.
4. **A committed evaluation result must be re-run against the code that ships it.**
   The staleness found here was invisible because nothing checked it. The four-axis
   harness's reproducibility requirement (SC-003) exists to catch exactly this, and
   it is why the retrieval budget question in D7 turned out to be the smaller half
   of the problem.