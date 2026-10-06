# Evaluation results — all axes

> ## NOT COMPARABLE
>
> These figures must not be read against each other. Each axis measures something different and therefore runs its own configuration: the cost axis retrieves a different number of hits per question than the grounding axis, the isolation axis builds a synthetic corpus, and the latency axis builds a size ladder rather than reading one at all. Every row below carries its own provenance so you can see exactly which rows may be compared.
>
> A trustworthy comparison of two runs is `memoratum.eval_compare`, which refuses to print two figures as a comparison when their manifests disagree.

## Axis gates

| axis | gate | gated figure | value |
|---|---|---|---|
| isolation | PASS | leaked_hits, scoped_queries_observed | 0, 3 |
| cost | PASS | hits_observed, chunks_ingested, retrieved_chars_mean | 80, 1507, 2.847e+04 |
| latency | PASS | us_per_chunk_sublinear, us_per_chunk_sublinear, retrieve_median_ms, retrieve_median_ms, retrieve_median_ms | 0.8102, 0.9371, 4.394, 7.121, 13.35 |
| grounding | PASS | grounded_fraction | 1 |

## Provenance per row

| axis | embedder | vector_store | n | seed | data_sha256 |
|---|---|---|---|---|---|
| isolation | HashEmbedder:64 | sqlite | 13 | 42 | synthetic-scoped-corpus |
| cost | HashEmbedder:64 | sqlite | 4 | 42 | d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442 |
| latency | HashEmbedder:64 | sqlite | 3 | 42 | synthetic-latency-corpus |
| grounding | HashEmbedder:64 | sqlite | 25 | 42 | d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442 |
| retrieval (longmemeval-s) | HashEmbedder:64 | sqlite | 10 | 42 | d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442 |

## Retrieval metrics

`R@k` is measured in **k retrieval positions**; `MRR` is measured in **distinct sessions**. They answer different questions and must not be read as the same unit.

| mode | metric | value |
|---|---|---:|
| documents | MRR | 0.5833 |
| documents | full-R@10 | 0.5000 |
| documents | full-R@5 | 0.5000 |
| documents | partial-R@10 | 0.8000 |
| documents | partial-R@5 | 0.8000 |
| hybrid | MRR | 0.5833 |
| hybrid | full-R@10 | 0.5000 |
| hybrid | full-R@5 | 0.5000 |
| hybrid | partial-R@10 | 0.8000 |
| hybrid | partial-R@5 | 0.8000 |

## Regenerate this figure

This file is assembled from the axis result files it lists, not measured directly. Regenerate each axis with the command recorded in its own `RESULTS-*.md`, then re-run `python -m memoratum.eval_results` over the result JSON files.

## Regenerate this figure

```bash
python -m memoratum.eval_results eval/isolation.json eval/cost.json eval/latency.json eval/grounding.json eval/longmemeval.json --out-md eval/RESULTS.md
```
