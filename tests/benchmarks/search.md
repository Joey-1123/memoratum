# Search benchmark

Principle IV: a performance claim must be backed by a measurement committed to the
repository. This file is that measurement.

## The defect

`chunks.embedding` was written at ingest (`ingest.py` -> `db.add_chunks(...,
embeddings=)`) and was even SELECTed by the chunk leg of `search.py` — and then
discarded. `search.py` re-embedded every chunk text on every query.

At the embedder's `batch_size = 64`, a 10,000-chunk container cost **157 embedding
API calls per search** against a remote provider. The facts leg already did it
correctly: compute if absent, persist, reuse. The chunk leg was never given the
same treatment.

## Measured cost of the old path

In-process search latency against a growing corpus of memories
(`search_memory_records`, 768-dim):

| Corpus | Latency |
|---:|---:|
| 500 | 90 ms |
| 1,000 | 326 ms |
| 2,000 | 670 ms |
| 4,000 | 1,581 ms |

Linear growth, single-digit-thousand ceiling, and worse in `hybrid` mode.

## Stage 1 (done): reuse the persisted embedding

`search.py` now keeps `c.embedding` and reuses it, embedding only rows whose
stored blob is missing or whose width does not match the current embedder. This
removes the dominant cost with **zero new dependencies and zero schema change**.

Provider embedding calls per search: **O(missing rows)** instead of **O(corpus)**.
In the steady state — every chunk ingested with the current model — that is zero.

## Stage 2 (done): bounded candidate set

The remaining cost was reading *and* parsing every chunk row before any ranking
happened. FTS5 now runs first and bounds both the row fetch and the vector scoring:

```
SELECT COUNT(*) ...                       -- decide whether bounding is worth it
SELECT ... WHERE ... AND c.id IN (<fts hits>)
```

`PREFILTER_MIN_CANDIDATES = 512`. Below that threshold nothing is bounded: scoring
a small corpus is cheap and costs no recall, so ordinary self-hosted deployments
keep exact semantic behaviour and never see the trade-off.

### Measured end-to-end (128-dim, 4 chunks/doc, `search_mode="documents"`)

Selective query — `"taxation"` over text where most tokens are rare:

| Chunks | Bounded | Median latency |
|---:|---|---:|
| 200 | no | 10.1 ms |
| 1,600 | yes | 14.0 ms |
| 6,000 | yes | 39.6 ms |

30x the corpus for ~4x the time. Before Stage 2 the same shape was linear with a
much larger constant (see the table at the top of this file: 1,000 → 326 ms,
4,000 → 1,581 ms).

### The gain is smaller than the design estimate

The design work projected **~46x** from a prefilter at k=200. The realised gain is
roughly **1.6x** on the path as it actually runs (1,600 chunks: 73 ms → 43 ms;
4,000: 180 ms → 114 ms, measured before the fetch was bounded as well as the
scoring). The projection was based on scoring cost in isolation and understated how
much of the original latency was row fetching, parsing and the lexical leg. The
number recorded here is the measured one.

### Recall impact, measured with the eval harness (T116)

Retrieval quality was compared with bounding on and off at the same cutoff, using
the on-disk LongMemEval-S dataset and the project's own harness:

```sh
MEMORATUM_SEARCH_PREFILTER_MIN=100000000 uv run python -m memoratum.eval_longmemeval \
  --data data/longmemeval_s_cleaned.json --n 50 --k 10      # bounding OFF
MEMORATUM_SEARCH_PREFILTER_MIN=100        uv run python -m memoratum.eval_longmemeval \
  --data data/longmemeval_s_cleaned.json --n 50 --k 10      # bounding ON
```

| n | Bounding | partial-R@10 | full-R@10 | MRR |
|---:|---|---:|---:|---:|
| 25 | off | 0.760 | 0.400 | 0.381 |
| 25 | on | 0.920 | 0.720 | 0.546 |
| 50 | off | 0.780 | 0.380 | 0.488 |
| 50 | on | **0.940** | **0.740** | **0.642** |

Bounding does not cost recall — it **improves** it, substantially and consistently
across both sample sizes. The likely reason is that the unbounded vector leg ranks
the entire corpus and floods the fused top-10 with high-similarity but irrelevant
chunks; restricting it to lexically-relevant candidates behaves like reranking.

That is the opposite of the trade-off this change was expected to make, so it is
recorded with the commands to reproduce it rather than asserted.

**A false result worth recording.** The first version of this comparison reported
"identical recall, bounding is free". It was wrong: instrumentation showed the
corpus peaked at 386 chunks, below the default 512 threshold, so **the prefilter
never engaged** and both runs executed identical code. The numbers only became
meaningful once the threshold was lowered to force engagement. A benchmark that
cannot fail is not a benchmark.

`MEMORATUM_SEARCH_PREFILTER_MIN` exists so this comparison is reproducible and so
operators can trade recall against latency. The default remains 512: below that,
scoring everything is cheap and exact-semantic behaviour is preserved.

### Pathological queries

FTS5 computes `bm25` for every matching row *before* `LIMIT` is applied, so a
query whose tokens appear in most of the corpus still ranks the whole corpus. A
query like `"topic 3"`, where the bare digit appears in every chunk, is the worst
case and showed 11x growth for 4x corpus. This is inherent to FTS5 relevance
ranking, not something the prefilter can address; the bound only removes work
*downstream* of the lexical leg. The realistic-query numbers above are the ones
that matter for a self-hosted corpus.

## Why `sqlite-vec` is deferred

`vec0` is the right long-term answer and the wrong answer today:

- Prebuilt **Linux aarch64** binaries are the gap — i.e. exactly the Raspberry Pi
  and ARM homelab self-hoster who is this project's audience.
- `enable_load_extension` is compiled out on some builds, forcing a second code
  path plus tests for it.
- `vec0` is a virtual table, so schema changes need drop-and-rebuild, which
  conflicts with the additive `ALTER TABLE` migration list.
- Current blobs are un-normalized `struct.pack` output, so adoption needs a full
  data migration and diverges whenever the embedder model changes.
- ANN's value proposition starts around 10M vectors — roughly three orders of
  magnitude above a realistic single-operator corpus.

`test_no_new_runtime_dependency_for_search` enforces that it has not been added to
the runtime dependency set.

## ANN trigger

Adopt `sqlite-vec` when **any** of these holds:

1. A container exceeds ~50,000 vectors **and** the FTS prefilter measurably
   misses relevant results.
2. p95 search latency exceeds 250 ms *after* Stages 1 and 2.
3. Metadata filters become selective enough that the lexical prefilter cannot
   narrow the candidate set.

## Regression guard

`test_search_latency_is_sublinear` asserts that 4x the corpus does not cost
anywhere near 4x the time. It is deliberately loose: it fails on a regression to
O(n) without being brittle about absolute milliseconds on shared CI hardware.