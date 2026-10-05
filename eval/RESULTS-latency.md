# Latency per phase

gate: PASS

| chunks | phase | median ms | p95 ms | us/chunk | prefilter |
|---|---|---|---|---|---|
| 100 | ingest | 0.088 | 0.232 | n/a | None |
| 100 | embed | 36.64 | 36.64 | 366 | None |
| 100 | index | 36.64 | 36.64 | 366 | None |
| 100 | retrieve | 4.39 | 7.96 | 43.94 | None |
| 200 | ingest | 0.121 | 0.308 | n/a | None |
| 200 | embed | 92.78 | 92.78 | 464 | None |
| 200 | index | 92.78 | 92.78 | 464 | None |
| 200 | retrieve | 7.12 | 14.60 | 35.60 | None |
| 400 | ingest | 0.091 | 0.185 | n/a | None |
| 400 | embed | 199 | 199 | 497 | None |
| 400 | index | 199 | 199 | 497 | None |
| 400 | retrieve | 13.35 | 25.43 | 33.37 | None |

- samples per phase: 120 (warmup 5 discarded)
- ladder: [100, 200, 400]
- prefilter threshold: 512 (ladder stays below it, so no step measures an algorithm switch)

us/chunk is the primary gate: sub-linear growth makes it fall, a regression to
O(n) makes it rise. Absolute milliseconds are a smoke bound only, and phases under
2.0 ms are reported rather than gated.

## Gate

- us_per_chunk_sublinear: pass (value 0.8102484418484767, bound 1.224744871391589)
- us_per_chunk_sublinear: pass (value 0.9371245333262183, bound 1.224744871391589)
- retrieve_median_ms: pass (value 4.39426300181367, bound 15.525999999999998)
- retrieve_median_ms: pass (value 7.120889500583871, bound 15.525999999999998)
- retrieve_median_ms: pass (value 13.346320500204456, bound 15.525999999999998)
- overall: PASS

## Hardware (required; without it these numbers do not travel)
- cpu: Intel(R) Core(TM) i3-7020U CPU @ 2.30GHz
- cores: 4 | ram_mb: 3772
- python: 3.12.14
- platform: Linux-7.2.7-arch1-1-x86_64-with-glibc2.44

## Regenerate this figure

```bash
python -m memoratum.eval_latency --ladder 100,200,400 --samples 120 --warmup 5 --seed 42 --baseline eval/BASELINES.md --out-md eval/RESULTS-latency.md --out-json eval/latency.json
```
