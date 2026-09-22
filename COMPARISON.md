# Memoratum competitive standing — temporary working note

> Untacked by design: lives in /tmp, not the repo. Do not commit.
> Date: 2026-09-20. Memoratum v0.7.0.

## Scorecard

| Dimension | Supermemory | Graphify | Obsidian | Memoratum |
|---|---|---|---|---|
| Ingest API | full + connectors | n/a (CLI scan) | manual | compat routes |
| Hybrid retrieval | managed | n/a (graph queries) | manual search | RRF + FTS5, eval-gated |
| Temporal facts | proprietary models | static snapshot | human edits | dreaming + validity windows |
| Codebase import | — | AST-native | — | bridge (8,734 facts, 6/10 recall) |
| Human-readable export | — | --obsidian | native | vault export view |
| Interactive graph UI | cloud dashboard | local HTML viz | graph view | 2D + 3D console |
| Auth/isolation | scoped keys | n/a | vault = folder | scoped keys + revocation + keygen |
| Eval evidence | vendor benchmarks | n/a | n/a | LongMemEval-S 0.78, bridge 6/10 |
| Ops | managed / 1-binary | zero (static) | zero (app) | single server, no HA |
| Extraction quality | proprietary models | deterministic | human | bring-your-own LLM |

## Reading

1. Feature parity on breadth, not depth. Every Supermemory box is ticked, but
   their models/connectors/scale run deeper. Ours are real, tested, younger.
2. Graphify is now an ingestion source (deterministic AST edges in, temporal
   reasoning out) — complementary by design, not a rival.
3. Moat unchanged: self-hosted + API-compatible + agent-native clients, AGPL,
   with eval numbers attached. Nothing else in the table ships all three.

## Rank-moving next steps (proposed, unapproved)

- Reranker + query-rewrite → bridge 6/10 to 8+/10 (biggest visible quality win)
- sqlite-vec ANN → removes brute-force corpus ceiling
- Background worker → removes sync-request ceiling
- Connectors (Drive/Notion/Gmail) → only Supermemory feature with no OSS answer here
