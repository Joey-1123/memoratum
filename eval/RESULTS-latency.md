# Latency per phase

gate: PASS

| chunks | phase | median ms | p95 ms | us/chunk | prefilter |
|---|---|---|---|---|---|
| 100 | ingest | 0.091 | 0.163 | n/a | None |
| 100 | embed | 30.88 | 30.88 | 309 | None |
| 100 | index | 30.88 | 30.88 | 309 | None |
| 100 | retrieve | 2.99 | 3.27 | 29.86 | None |
| 200 | ingest | 0.085 | 0.128 | n/a | None |
| 200 | embed | 62.20 | 62.20 | 311 | None |
| 200 | index | 62.20 | 62.20 | 311 | None |
| 200 | retrieve | 5.71 | 7.42 | 28.56 | None |
| 400 | ingest | 0.084 | 0.127 | n/a | None |
| 400 | embed | 135 | 135 | 337 | None |
| 400 | index | 135 | 135 | 337 | None |
| 400 | retrieve | 11.09 | 20.75 | 27.72 | None |

- samples per phase: 30 (warmup 5 discarded)
- ladder: [100, 200, 400]
- prefilter threshold: 512 (ladder stays below it, so no step measures an algorithm switch)

us/chunk is the primary gate: sub-linear growth makes it fall, a regression to
O(n) makes it rise. Absolute milliseconds are a smoke bound only, and phases under
2.0 ms are reported rather than gated.

## Gate

- us_per_chunk_sublinear: pass (value 0.9566944577070244, bound 1.224744871391589)
- us_per_chunk_sublinear: pass (value 0.9703248062061509, bound 1.224744871391589)
- overall: PASS

## Hardware (required; without it these numbers do not travel)
- cpu: Intel(R) Core(TM) i3-7020U CPU @ 2.30GHz
- cores: 4 | ram_mb: 3772
- python: 3.12.14
- platform: Linux-7.2.7-arch1-1-x86_64-with-glibc2.44
