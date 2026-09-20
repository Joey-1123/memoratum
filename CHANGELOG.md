# Changelog

## [0.1.0] - 2026-09-20
### Added
- Phase 1 core: `POST /v3/documents`, `GET /v3/documents/{id}`, `POST /v4/search`
  (Supermemory-compatible shapes), Bearer auth with container-scoped keys
- SQLite store (WAL, FTS5, forward-only migrations), customId idempotent upserts
- Recursive + Markdown-aware chunking; embedder interface (deterministic hash
  offline default, OpenAI-compatible API provider)
- Hybrid retrieval (cosine + FTS5 fused with RRF, mandatory containerTag filter)
- `python -m memoratum` server on `:6767`, Dockerfile + docker-compose
