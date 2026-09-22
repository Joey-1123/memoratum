# Changelog

## [Unreleased]

## [0.7.0] - 2026-09-22
### Added
- Relevance rerank (heuristic default, cross-encoder optional), query
  rewrite via LLM with merge, stopword-aware keyword overlap
- Per-IP rate limiting (120/min, health/dashboard exempt, loopback bypass,
  `Retry-After` + SDK backoff), input size caps (422)
- Bridge eval harness evidence (`eval/RESULTS-bridge.md`, recall@5 10/10)
### Fixed
- Multi-valued relations coexist; re-assertion revives superseded facts
- Thread-migrating SQLite connections (serialized requests + regression test)
- Batched embedding calls; per-tag fact vector cache

## [0.6.0] - 2026-09-20
### Added
- Dashboard console (Vite+React, served at `/dashboard`): tag cards, Sigma
  2D graph with Jarvis inspector (AI-view/relations/history/provenance),
  Three.js 3D presentation mode, time scrubber, command palette, search view,
  graph import view, Obsidian-style vault export view
- Server-side graph import endpoint (`POST /v4/import`) and fact
  write/list endpoints (`POST/GET /v4/facts`)
- Embedding request batching; per-tag fact vector cache

## [0.5.0] - 2026-09-20
### Added
- First-boot admin keygen (secure by default, zero-config kept)
- Forget API: fact delete, tag purge, key revocation
- Working wildcard keys (lookup refactor)
### Fixed
- Dreaming scoped per tag, re-dream on content upsert, windowed oversized docs
- Full-row fact returns, uniform 404, env/dims guards

## [0.4.0] - 2026-09-20
### Added
- Python SDK (`memoratum.client.Client`: add/search/profile, retries, errors)
- MCP server (`python -m memoratum.mcp`: stdio JSON-RPC `remember`/`recall`)
- OpenCode V2 plugin (`clients/opencode/memoratum.js`)
- TypeScript SDK (`clients/ts`, zero-dep, `npm test`)

## [0.3.0] - 2026-09-20
### Added
- Metadata on documents/facts with AND-equality `filters` on search
- `GET /v4/profile` (fact sample + per-tag stats)
- `rerank` flag (recency-blended rescoring of top candidates)
- `POST /v4/keys` scoped key issuance (admin-gated; 401/403 semantics)

## [0.2.0] - 2026-09-20
### Added
- Dreaming: LLM fact extraction (`instant` per-doc, `dynamic` tag-bundle),
  `dreaming` param on document ingest, temporal fact graph with contradiction
  supersede (history preserved, never deleted)
- `searchMode` on search: `memories` (facts), `documents` (chunks), `hybrid`
- `MEMORATUM_LLM_ENDPOINT/MODEL/KEY` settings (OpenAI-compatible)
- FTS5 input sanitizing (punctuation no longer breaks keyword search)

## [0.1.0] - 2026-09-20
### Added
- Phase 1 core: `POST /v3/documents`, `GET /v3/documents/{id}`, `POST /v4/search`
  (Supermemory-compatible shapes), Bearer auth with container-scoped keys
- SQLite store (WAL, FTS5, forward-only migrations), customId idempotent upserts
- Recursive + Markdown-aware chunking; embedder interface (deterministic hash
  offline default, OpenAI-compatible API provider)
- Hybrid retrieval (cosine + FTS5 fused with RRF, mandatory containerTag filter)
- `python -m memoratum` server on `:6767`, Dockerfile + docker-compose
