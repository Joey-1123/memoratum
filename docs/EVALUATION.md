# Evaluation runs

The local evaluation tools never upload benchmark data. They read a JSON/JSONL
file, build a temporary SQLite database per question, and report retrieval
metrics plus a reproducibility manifest.

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

References:

- https://github.com/xiaowu0162/LongMemEval
- https://github.com/snap-research/locomo
- https://huggingface.co/datasets/Percena/locomo-mc10
