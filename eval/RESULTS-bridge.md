# Bridge eval — graphify import → memory recall

Date: 2026-09-22 (re-run with real vectors + coexistence fix).

## Setup

Corpus: lunee `graphify-out` (5,780 nodes / 13,890 links → 13,837 live facts
after exact-duplicate collapse). Vectors: nomic-embed-text via Ollama (CPU),
persisted per fact. Method: 10 keyword questions (2 per relation: calls,
imports, references, contains, inherits), `searchMode=memories`, top-5 must
contain both endpoint labels.

## Result: recall@5 = 10/10 (plain and reranked)

## History (why this took three attempts)

- Attempt 1 (HashEmbedder): 0/10 — vector leg pure noise, keyword leg drowned.
- Attempt 2 (nomic, supersede bug live): 6/10 — blind multi-valued supersede
  had silently killed ~4.7k facts.
- Attempt 3 (nomic + coexistence + stopwords + persisted vectors): 10/10.

## Reading

Import pipeline is exact (13,890/13,890 upserted, diff-clean on re-run) and
fast (2.5 min with `skipEmbedding`, backfill ~1h CPU one-time, persistent
thereafter). Remaining levers for harder queries (question-style rather than
keyword): query-rewrite (shipped, unmeasured here) and a real cross-encoder.
