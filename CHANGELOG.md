# Changelog

## [Unreleased]

## [0.34.0] - 2026-09-24
### Changed
- Polished LoCoMo report output and synchronized package metadata with the
  final v0.34.0 release.

## [0.33.0] - 2026-09-24
### Added
- Fact pagination with exact totals and short-lived dashboard request caching.
- Scheduled Python and dashboard dependency security audits.
- Local dogfood evidence for repository docs and Ollama embeddings.
### Changed
- Package metadata and lockfile now track the v0.33.0 release.

## [0.30.0] - 2026-09-24
### Added
- Provider-neutral LLM, embedding, reranker, and vector-store adapters with
  optional local/gateway integrations and local fallbacks.
- Scoped vector indexing during ingest, backend-aware search, and vector-point
  lifecycle cleanup.
- LongMemEval-S, LoCoMo, and LoCoMo-MC10 local evaluation harnesses with
  deterministic manifests, result comparison, and scheduled smoke fixtures.
- Revocable, expiring document share links; Mem0-compatible route shim;
  administrator governance tables in the dashboard.
- SQLite backup/restore CLI and an injectable OIDC identity-provider seam.
### Security
- CI now rejects application/client/dashboard telemetry and analytics code;
  local audit and usage accounting remain explicitly local.
### Fixed
- Vector points are removed on purge, expiry, and re-index; OIDC identities
  retain local scope and audit attribution.

## [0.12.0] - 2026-09-23
### Added
- Memory orbit hero (ported orbiting-rings concept onto our tokens: particle
  globe core, 3 counter-rotating concept rings, lazy chunk) as the Tags
  empty state; Tailwind v4 mapped to app theme variables

## [0.11.0] - 2026-09-23
### Changed
- Dashboard theme rebuilt on a public note-app variable architecture, with
  full dark + light themes, toggle, and size/radius/type scales

## [0.10.0] - 2026-09-23
### Changed
- Dashboard de-slopped: SVG ribbon icons, button press/hover physics, refined
  focus rings, restrained community palette (goodbye rainbow), degree sizing,
  predicate-colored edges with legend, hover neighbor focus, type scale with
  tabular numbers, dense inspector rows, guided empty states

## [0.9.0] - 2026-09-23
### Added
- Console shell: ribbon, explorer tree, note tabs, live
  status bar, vault tag switcher
- Note pane with wikilinks, backlinks, history, and hover preview cards
- Graph selection opens notes; shared selection state across views

## [0.8.0] - 2026-09-22
### Added
- Background worker (`python -m memoratum.worker`, compose service):
  durable jobs, atomic claims, retries with poison-pill handling, dream
  chaining, job status endpoint (`GET /v4/jobs/{id}`)
### Changed
- Ingest is async-only: `POST /v3/documents` returns `queued` + `job_id`;
  poll the document or job for completion

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
  graph import view, vault export view
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
  (stable versioned shapes), Bearer auth with container-scoped keys
- SQLite store (WAL, FTS5, forward-only migrations), customId idempotent upserts
- Recursive + Markdown-aware chunking; embedder interface (deterministic hash
  offline default, OpenAI-compatible API provider)
- Hybrid retrieval (cosine + FTS5 fused with RRF, mandatory containerTag filter)
- `python -m memoratum` server on `:6767`, Dockerfile + docker-compose
