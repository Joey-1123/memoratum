# Bridge eval — graphify import → memory recall

Date: 2026-09-20. Corpus: lunee `graphify-out` (4,114 nodes / 8,734 links →
8,716 live facts after exact-duplicate collapse). Vectors: nomic-embed-text
via Ollama (CPU). Method: 10 keyword questions (2 per relation: calls,
imports, references, contains, inherits), `searchMode=memories`, top-5 must
contain both endpoint labels.

## Result: recall@5 = 6/10

Hits: log_context/_get_log_context, App/createVoiceSession,
package/mediapipe + react pairs, .timeout/setter, health/custom_route.
Misses: HologramOrb/updateTheme, createOrbScene/lineMat,
BlocklistViolationError/RuntimeError, UnknownTargetError/ValueError.

## Reading

- Import pipeline is exact (8,734/8,734 upserted, diff-clean on re-run).
- Misses are ranking misses, not missing data — small-corpus exact-overlap
  queries against 8.7k candidates with CPU embeddings. Real levers, in order:
  real reranker, HyDE/query-rewrite for question-style queries, sqlite-vec ANN.
- Baseline for comparison: same 10 questions with HashEmbedder scored 0/10,
  confirming the vector leg (not just keywords) now carries the result.
