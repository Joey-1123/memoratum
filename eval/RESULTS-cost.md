# Token cost per retrieval

gate: PASS

## Reported figures (canonical unit: characters)
- queries: 4
- hits: 80.0
- retrieved_chars_mean: 28469.0
- retrieved_chars_p95: 28956.1
- retrieved_chars_total: 113876
- retrieved_ws_tokens_mean: 4630.2 (companion unit)

## Redundancy
- redundant_hits: 21.0
- duplicate_source_rows: 14.0

## Token estimate (derived, never gates)
- chars_per_ws_token: 6.264 (measured on this corpus)
- token_estimate_mean: 4544.7
- derived from the corpus-measured chars_per_ws_token; valid only as a ratio between runs on the same corpus, not as an absolute token count
- tokenizer: none (never changes a gated figure)

## Manifest
- schema: longmemeval-scoped-v2
- seed: 42
- retrieval_limit: 20 hits
- mean_chunks_per_document: 7.299
- data_sha256: d6f21ea9d60a0d56
