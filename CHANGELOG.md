# Changelog

## [0.36.0] - 2026-10-23

The evaluation-harness release: the server finally measures itself, on four offline
axes with committed figures, gates, and mutations caught in each one.

### Added
- **Evaluation harness (`002-evaluation-harness`, 107 tasks, all closed).** Four
  deterministic, dependency-free axes with mutated-then-caught test suites:
  **scope isolation** (`leaked_hits == 0` as an absolute gate), **token cost**
  (canonical character unit, measured chars↔token ratio, byte units rejected),
  **latency** (µs/chunk scaling as the primary gate — 1.5x per 4x size step — and
  a sample floor of 120 set from measurement after 10 identical 30-sample runs
  spanned 2.15x and failed unmodified code), and **grounding** (provenance by row
  identity, per-kind rules; `chunks.text` is never compared against
  `documents.content`).
- **HotPotQA evidence retrieval (Phase C).** Sentence-level
  `evidence_recall`/`precision`/`f1`/`joint_evidence_rate` over `(title, sent_id)`
  pairs by exact set arithmetic. One sentence is one document, a hop is a document
  not a sentence, `--config distractor` is refused in code, and a test proves the
  answer field cannot influence any figure. The dataset is not vendored (CC BY-SA
  4.0); the committed fixture is synthetic and the axis is CI-gated against a
  committed baseline.
- **Rollup and comparison.** `eval_results` renders all axes plus retrieval metrics
  with per-row provenance and marks rows NOT COMPARABLE when configs differ;
  `eval_compare` refuses to compare two runs whose manifests disagree (absent keys
  count as mismatches).
- **Local observability.** Prometheus-style metrics, split health endpoints
  (`/health/live`, `/health/ready`), and structured logs.
- **Durability.** Job leases with a reaper and P0 stranded-job recovery; a
  documented retention policy for every table with a real sweep.
- **CI ratchets.** A coverage floor and a no-new-suppressions check so the test
  gates cannot be quietly weakened; evaluation axes run as CI gates with five
  negative controls (missing baseline, straddling ladder, 5-sample, mismatched
  eval_compare, missing corpus — plus distractor-refusal and k=0 refusal).

### Changed
- **Search performance, recall held.** The candidate set is bounded by the FTS5
  leg and ingest reuses already-persisted chunk embeddings, both measured against
  recall rather than asserted.
- **Concurrency.** The request lock is released before the endpoint body runs, and
  every migration is transaction-wrapped with the schema verified afterwards.
- **One commit boundary per logical write** across the memory lifecycle.
- **Read-only project roles** (`READER`) are enforced on reads and writes; project
  deletion cleanup is durable.
- **Licence and coverage discipline is mechanical.** Licence-minimality and
  minimal-install are tested; a task that still fails after a ratchet run must be
  fixed, not skipped.
- **Documentation reconciled with behaviour**, enforced by conformance tests.

### Fixed
- **Webhook SSRF.** Delivery targets are pinned to resolved addresses and the
  CGNAT address range is closed; unknown request fields are rejected instead of
  widening scope.
- **`R@k` counted duplicates.** The hit list is now sliced before dedup; the
  pre-correction numbers are marked "metric definition v0" and are not comparable
  (`eval/MIGRATION-metric-v1.md`).
- **The latency gate initially failed on unmodified code** — the sample floor was
  too small for machine noise; fixed by measurement, not by loosening the bound.
- **Packaging.** The published port is reachable (image binds `0.0.0.0`), and the
  eval isolation manifest names its actual vector store (`sqlite`).

### Verification
- `pytest -q`: **964 passed, 1 skipped**; coverage **81.90%** (floor held);
  Ruff clean over 216 files; no-telemetry, suppression, and coverage-floor
  ratchets all pass; zero leaked temp directories. CI: lint/test, TS client,
  dashboard build, eval-axis gates (with negative controls), governance
  dashboard, and dependency scanning green; three evaluation result files
  (`eval/*.json`, `eval/RESULTS.md`) committed with regeneration commands.
- `pip-audit --local` and `npm audit` were not runnable in this workspace.

## [Unreleased]

## [0.35.0] - 2026-09-30
### Added
- Canonical `mem_<uuid>` lifecycle across native and Mem0-compatible routes,
  including native create/get/update/delete/history, categorization, and
  project-scoped asynchronous bulk jobs with per-item progress.
- Local organization/project/member isolation, durable project deletion jobs,
  job cancellation, and explicit `READER`/`OWNER` project roles.
- Encrypted webhook secret storage with Fernet key rotation support, webhook
  activation controls, and complete memory/ingest event coverage.
- Provider-neutral project scope for SQLite, Qdrant, Chroma, and pgvector,
  including additive project-column migration, legacy SQLite vector backfill, and
  the `scripts/reindex_vectors.py` operator rebuild command.

### Changed
- Project management is fail-closed: tag-only keys cannot become control-plane
  credentials, OIDC identities require a local membership row, and project
  writes are blocked while deletion is in progress.
- Open mode now permits local project/management routes consistently with the
  documented no-auth default.
- Search, batch operations, audit, and usage accounting now preserve canonical
  IDs and project scope.

### Fixed
- Webhook replay no longer steals running delivery jobs, duplicate retry jobs
  are coalesced, and transaction handling is safe for nested callers.
- Expired/dangling project rows, metadata identity smuggling, PgVector result
  indices, and partial ingest failures are handled without corrupting state.

### Verification
- The full Python test suite, Ruff, the dependency-free TypeScript probe, dashboard
  build, and the no-telemetry guard pass locally. GitHub release operations are
  not performed from this workspace.

## [0.34.2] - 2026-09-24
### Changed
- Align package and lockfile metadata with the final release tag.

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
