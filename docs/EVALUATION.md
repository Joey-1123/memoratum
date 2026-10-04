# Evaluation runs

The local evaluation tools never upload benchmark data. They read a JSON/JSONL
file, build a temporary SQLite database per question, and report retrieval
metrics plus a reproducibility manifest.

## Metric definitions

`recall@k` and `mrr` deliberately use **different units**, and mixing them up is the
mistake that produced the v0 results files.

`ranked` is one session id **per retrieved hit**, in rank order.

| metric | unit | question answered |
|---|---|---|
| `partial-R@k` / `full-R@k` | **k retrieval positions** | "was a gold session among the first `k` hits?" |
| `MRR` | **distinct sessions** | "how many distinct sessions precede the first gold one?" |

`recall@k` slices the hit list **first**, then deduplicates. It previously
deduplicated **first**, which made it ask "are there `k` distinct sessions anywhere
in the retrieved set?" On a corpus averaging ~7.6 chunks per session a 20-hit
budget yields only ~2.6 distinct sessions, so the metric was crediting gold
sessions found well past the `k`-th hit.

Two consequences:

- **Scores can only decrease** after the fix — the visible session set under the
  corrected definition is always a subset. A drop is a correction, not a retrieval
  regression, and must never be "fixed" by re-inflating the retrieval budget.
- **`R@k` figures before and after 2026-10-04 are not comparable.** Files marked
  *metric definition v0* are retained unedited as a historical record; the
  comparison is in [`eval/MIGRATION-metric-v1.md`](../eval/MIGRATION-metric-v1.md).

`mrr` was deliberately left unchanged — it has no `k` and is a standard session-space
metric.

## Reproducibility

A committed result must be re-runnable against the code that ships it. Two committed
files currently fail that check for a reason unrelated to the metric definition:
`MRR` in `eval/RESULTS-n10.md` records 0.599 where current retrieval gives 0.583 under
either definition, because the Stage 1 / Stage 2 search work changed what is
retrieved. New results must record the command, and baselines must state the
hardware (latency and cost figures are hardware-bound).

## LongMemEval-S

```bash
uv run python -m memoratum.eval_longmemeval \
  --data data/longmemeval_s_cleaned.json \
  --full --k 5,10 --modes hybrid,documents \
  --out-json eval/longmemeval.json --out-md eval/longmemeval.md
```

Use `--n 20` for a deterministic sample. The manifest records the data hash,
seed, requested sample size, retrieval depths, modes, embedding adapter, and
backend label; it never records credentials or provider responses.

## LoCoMo

The released LoCoMo JSON can be used directly. Dialog IDs in QA evidence are
mapped back to their sessions, and observation/session-summary databases are
supported:

```bash
uv run python -m memoratum.eval_locomo \
  --data data/locomo10.json --full --source dialogs \
  --out-json eval/locomo.json
```

Use `--source observations` or `--source summaries` for the alternate local
retrieval databases. The harness reports session-level partial recall, full
recall, and MRR; answer-generation quality remains a separate model-dependent
evaluation.

## LoCoMo-MC10

The flat 1,986-item multiple-choice export is supported directly. The runner
indexes each item's sessions, retrieves the top `k` sessions, and reports plain
accuracy, balanced accuracy, and per-question-type accuracy. The default
answerer is deterministic and offline; an explicit chat answerer can be passed
by library callers.

```bash
uv run python -m memoratum.eval_mc10 \
  --data data/locomo_mc10.json --full --k 5 \
  --out-json eval/locomo-mc10.json
```

Compare result JSON files locally without uploading them:

```bash
uv run python -m memoratum.eval_compare eval/longmemeval.json eval/locomo-mc10.json \
  --out-md eval/RESULTS.md
```

A small scheduled workflow runs both harnesses against committed synthetic
fixtures. Full benchmark files remain local and are never fetched or uploaded by
the default CI job.

References:

- https://github.com/xiaowu0162/LongMemEval
- https://github.com/snap-research/locomo
- https://huggingface.co/datasets/Percena/locomo-mc10
