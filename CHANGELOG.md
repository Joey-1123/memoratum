# Changelog

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
