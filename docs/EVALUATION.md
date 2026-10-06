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

## The four axes

Retrieval quality alone cannot see most of what can go wrong in a memory layer. The four
axes below measure the things recall and MRR are blind to.

Every axis writes a manifest recording its seed, sample size, retrieval depths, search
modes, embedding adapter, backend and data hash, and every axis is gated by an explicit
bound. An axis is either measured or absent; there is no third state.

| axis | what it measures | the gate |
|---|---|---|
| [`eval_isolation`](../eval/RESULTS-isolation.md) | tenant isolation on real retrieval | `leaked_hits == 0` |
| [`eval_cost`](../eval/RESULTS-cost.md) | what one retrieval costs in context budget | `retrieved_chars_mean ≤ baseline × 1.10` |
| [`eval_latency`](../eval/RESULTS-latency.md) | per-chunk retrieval cost | `us_per_chunk(4N) < 1.5 × us_per_chunk(N)` |
| [`eval_grounding`](../eval/RESULTS-grounding.md) | provenance of returned content | `grounded_fraction ≥ min_grounded_fraction` |

Run them individually, or assemble one file with all four plus the retrieval metrics:

```bash
uv run python -m memoratum.eval_results \
  eval/isolation.json eval/cost.json eval/latency.json eval/grounding.json \
  eval/longmemeval.json --out-md eval/RESULTS.md
```

### Why isolation is a separate axis

The two worst defects found during production hardening were a write landing in the global
`NULL` scope, and a typo'd field name being silently accepted (`projct_id` → `201`,
`project_id = None`). **Recall cannot observe either.** A single unscoped corpus reports
perfect recall whether or not the store enforces scope, because recall is monotonically
indifferent to where data landed — so a leakage bug could only ever *improve* the score.

`eval_isolation` therefore ingests each session into N projects and asserts that a scoped
query returns nothing from another project. The gate is an **absolute count**, never a
rate: the correct value is exactly zero, so there is no tolerance to tune and no
false-positive surface. A rate over a variable denominator is exactly the shape that hides
a leak.

```bash
uv run python -m memoratum.eval_isolation --projects 2 --queries 5 --seed 42 \
  --out-md eval/RESULTS-isolation.md --out-json eval/isolation.json
```

`--scope both` additionally issues unscoped queries and records them in a **separate
bucket**. Every scope filter in this codebase is conditional on `is not None`, so
`project_id=null` genuinely means "no scope requested" — counting those queries' cross-project
hits as violations would report a false failure on a correct implementation.

### Cost is measured in characters, not tokens

There is no BPE tokenizer in this repository and adding one would be a runtime dependency,
so the canonical unit is **characters**. The whitespace-token count is reported alongside as
a labelled estimate and never gates.

The chars↔token ratio is **measured from the corpus, never hardcoded**, and recorded in the
manifest. On LongMemEval it measures **6.264**; `research.md` D2 measured 6.27 independently.
Bytes are explicitly rejected as a unit: the corpus is ASCII so they carry no information,
and on non-ASCII they inflate 1.1–3× with nothing visible in the report.

Two checks exist because cost only grows against a baseline, which makes the gate one-sided:

* **`hits_observed`** and **`chunks_ingested`** fail on zero. `ingest.process_one` swallows
  every exception, so a total ingest failure retrieves nothing and needs no typo at all —
  and would otherwise read as an *improvement*.
* A **missing baseline fails** (invariant B3). It never silently passes.

### Latency gates on µs/chunk, and the sample floor is measured

Per-chunk cost *declines* as the corpus grows, so sub-linear growth appears as a **falling**
number and a regression to O(n) as a **rising** one. That direction travels between
machines in a way absolute milliseconds do not, so µs/chunk is the primary gate and
milliseconds are a smoke bound only.

The default ladder is `100,200,400` — entirely below `PREFILTER_MIN_CANDIDATES = 512`.
Crossing that threshold measures an **algorithm switch** (whether the FTS5 leg bounds
candidates) rather than a corpus size; measured, that step produced a 3.38× → 1.09× → 3.55×
wobble. This repository has already shipped one false result from exactly this class.

**The sample floor is 120, and that number is a measurement.** Ten identical runs at 30
samples produced 400-chunk retrieve medians spanning **12.28–26.45 ms (2.15×)** on
unmodified code, the per-chunk ratio exceeded its own bound in 2 runs of 10, and the ms
smoke bound was exceeded 1 run in 6. The previous floor of 20 came from bootstrapping
median-of-n over 2,000 resamples, which estimated *within-run sampling* error and was
structurally blind to *between-run machine* noise — the larger term here. A gate whose noise
floor is wider than its band is not a gate; it just trains operators to ignore it.

Latency baselines **require recorded hardware** (invariant B1). Without it a baseline is
invalid and the gate fails, because an absolute millisecond figure means nothing without the
machine that produced it.

### Grounding proves provenance, not correctness

For a retrieval-backed store the ground truth is its own corpus, so grounding is checkable
deterministically with no external judge. A hit is grounded when its id resolves to an
ingested row **and** the text the retriever *returned* is exactly that row's text, under that
kind's rule, after NFKC + whitespace + casefold.

Each hit kind has its own rule, and a single global threshold is invalid: the fact leg renders
`"subject predicate object"`, which is by construction absent from the corpus, so one
threshold would score `memories` mode 0.0 and `documents` mode 1.0 for *identical evidence*.

Chunk text is graded against `chunks.text`, **never** `documents.content`. `split_markdown`
rewrites every heading level to `# {h}`, so parent-document containment is catastrophic
rather than graceful: over this repository's own markdown, **138 of 596 chunks (23.2%)** are
not exact substrings of their parent document and the worst in-document prefix is **1%**.

Grounding proves **provenance only**. It does not establish correctness or relevance — recall
and MRR own those, and a grounded answer built from the wrong document is caught by the
isolation and recall axes.

An external judge (`--judge external`) is available and **never gates**; its absence must
not fail a run (invariant G6).

### The self-checks are the point

Every axis can prove it still detects. `--inject-ungrounded` and `--require-clean` run the
axis against a deliberately broken input and **exit 2** when the detector stops detecting.
Without them a neutered metric would pass CI forever while reporting success.

A self-check must fail in **both** directions. A detector that reports everything ungrounded
is as broken as one that reports everything grounded, and is worse in practice: it would take
every correct run down with it and teach operators to ignore the gate.

## Comparing two runs

`eval_compare` **refuses to print two figures as a comparison** when their manifests
disagree. A confident number across mismatched manifests is worse than no number.

```bash
uv run python -m memoratum.eval_compare eval/grounding.json eval/latency.json
```

A difference in `embedder`, `vector_store`, `modes`, `seed`, `n`, `data_sha256`,
`metric_definition` or any per-axis config block is a hard failure. A key **absent** on one
side is also a mismatch: absent provenance is not comparable provenance. A **metric**
difference, by contrast, is the finding and does not fail the gate.

`eval/RESULTS.md` states this per row, because the four axes legitimately run different
configurations and a table implying otherwise would be misleading.

## Reproducibility

A committed result must be re-runnable against the code that ships it. Every emitted results
file now embeds the command that produced it:

```bash
python -m memoratum.eval_latency --ladder 100,200,400 --samples 120 --warmup 5 --seed 42 ...
```

Two committed files still fail the reproduction check for a reason unrelated to the metric
definition: `MRR` in `eval/RESULTS-n10.md` records 0.599 where current retrieval gives 0.583
under either definition, because the Stage 1 / Stage 2 search work changed what is
retrieved. Baselines must state their hardware, and latency figures are hardware-bound.

The LongMemEval runner supports the scope flags that make the isolation axis runnable against
a real dataset:

```bash
uv run python -m memoratum.eval_longmemeval \
  --data data/longmemeval_s_cleaned.json --n 10 --seed 42 \
  --project-count 2 --scope both \
  --latency-baseline eval/BASELINES.md --cost-baseline eval/BASELINES.md \
  --out-json eval/longmemeval.json
```

A scope leak exits non-zero, so CI cannot stay green while tenant isolation is broken.

## Metric change of 2026-10-04

`R@k` was **corrected on 2026-10-04**: it now slices the hit list before deduplicating,
rather than deduplicating first and asking "are there `k` distinct sessions anywhere in the
retrieved set".

- **Scores can only decrease.** The visible session set under the corrected definition is a
  subset of the old one, so a drop is a **correction, not a retrieval regression**, and must
  never be "fixed" by re-inflating the retrieval budget.
- **`R@k` figures before and after 2026-10-04 are not comparable.** Files marked *metric
  definition v0* are retained unedited as a historical record. The full comparison, including
  what did and did not change, is in
  [`eval/MIGRATION-metric-v1.md`](../eval/MIGRATION-metric-v1.md).

`eval_results` flags a v0 manifest in its output rather than printing the figure as current.
`mrr` was deliberately left unchanged — it has no `k` and is a standard session-space metric.

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
  --out-md eval/RESULTS-compared.md
```

Two different datasets are **not** a manifest mismatch — `dataset` is deliberately not a
compared key — but a difference in `n`, `seed` or `data_sha256` is, and the comparison will
refuse to print them as one table.

A small scheduled workflow runs the dataset harnesses against committed synthetic
fixtures. Full benchmark files remain local and are never fetched or uploaded by
the default CI job. The four axes run on every push and pull request against the
committed fixtures, with negative controls proving each gate can still fail
(`.github/workflows/eval-axes.yml`).

References:

- https://github.com/xiaowu0162/LongMemEval
- https://github.com/snap-research/locomo
- https://huggingface.co/datasets/Percena/locomo-mc10
