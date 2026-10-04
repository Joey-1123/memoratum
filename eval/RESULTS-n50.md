# LongMemEval-S retrieval — n=50

> **Metric definition v0 — superseded, retained as a historical record.**
> `partial-R@k` / `full-R@k` were computed by slicing *after* session deduplication.
> Corrected to slice *before* deduplication on 2026-10-04; see
> [`MIGRATION-metric-v1.md`](./MIGRATION-metric-v1.md).
>
> The `partial-R@5` → `partial-R@10` climb below (0.780 → 0.880) was **partly an
> artefact**: v0 credited a gold session found beyond the 5th hit. Under v1 both
> read 0.740. The numbers here are unedited, but they are **not comparable** to any
> v1 figure and are not reproducible on current `main` (MRR 0.509 here vs 0.489
> today under either definition, because feature 001's search stages changed what
> is retrieved).

## hybrid
- partial-R@5: 0.780 | full-R@5: 0.360
- partial-R@10: 0.880 | full-R@10: 0.640
- MRR: 0.509

## documents
- partial-R@5: 0.780 | full-R@5: 0.360
- partial-R@10: 0.880 | full-R@10: 0.640
- MRR: 0.509

took 20.7s
