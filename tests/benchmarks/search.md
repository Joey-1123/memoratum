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

## Stage 2 (not done): bounded candidate set

The remaining O(n) work is in-process cosine scoring plus a per-row
`json.loads`. The FTS5 leg is already computed via `db.keyword_search`
(BM25-ranked, external-content `chunks_fts`), so the natural next step is to lift
its top-N ids and score only those, then fuse with the existing RRF machinery.

Reference measurement from the design work, at 10,000 rows:

| Path | 1,000 | 10,000 | 50,000 |
|---|---:|---:|---:|
| brute force (current) | 0.33 s | 3.66 s | ~16 s |
| + reuse stored embedding | ~0.11 s | ~1.0 s | ~5 s |
| + FTS5 prefilter (k=200) | ~0.08 s | **0.079 s** | ~0.09 s (flat) |

~46x at k=200. Stage 2 is deliberately **not** in this change: it alters result
ordering, which needs re-tuning against the eval harness, and that is a separate
decision.

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