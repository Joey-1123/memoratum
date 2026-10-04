# LongMemEval-S retrieval — n=10

> **Metric definition v0 — superseded, retained as a historical record.**
> `partial-R@k` / `full-R@k` were computed by slicing *after* session deduplication,
> so they measured "are there k distinct sessions anywhere in the retrieved set".
> Corrected to slice *before* deduplication on 2026-10-04; see
> [`MIGRATION-metric-v1.md`](./MIGRATION-metric-v1.md).
>
> These values are **not comparable** to any v1 figure. They are also not
> reproducible on current `main`: MRR here is 0.599, but current retrieval gives
> 0.583 with either metric definition, because feature 001's search stages changed
> what is retrieved. Numbers below are unedited.

## hybrid
- partial-R@5: 0.800 | full-R@5: 0.500
- partial-R@10: 0.800 | full-R@10: 0.500
- MRR: 0.599

## documents
- partial-R@5: 0.800 | full-R@5: 0.500
- partial-R@10: 0.800 | full-R@10: 0.500
- MRR: 0.599

took 3.8s
