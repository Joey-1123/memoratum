# Memoratum — memory API parity spec

Target: full functional parity with a stable versioned memory API surface, self-hosted,
AGPL-3.0. Derived from public memory-API documentation and an existing open-source
client implementation. Phase-gated: each phase lands tested and runnable before the next begins.

## API surface (parity targets)

- `POST /v3/documents` — `{content, containerTag, customId?, dreaming?}` → async ingest,
  statuses `queued → extracting → chunking → embedding → indexing → done | failed`
- `POST /v4/memories` — memory add (containerTag, metadata)
- `POST /v4/search` — `{q, containerTag?, searchMode: hybrid|memories|documents, limit?, threshold?, rerank?, rewriteQuery?, filters?, include?}` → `{results: [{id, memory?|chunk?, similarity, metadata, updatedAt, version}], timing, total}`
- `GET /v4/profile` — static + dynamic summary per containerTag
- Auth: `Authorization: Bearer <key>`; scoped keys bound to a containerTag
- Isolation: `containerTag` = hard boundary (user/tenant/project); metadata = soft filters

## Pipeline semantics

- Ingest: type-aware extraction → contextual chunking → embeddings → index (dense + FTS + graph)
- Dreaming: second-phase memory consolidation; `dynamic` (default, groups related docs) vs
  `instant` (+1 op, immediate). Phase 1 may ship `instant`-only with `dynamic` queued.
- `customId` = stable identity for re-ingest + diff billing of already-seen tokens

## Phases

1. **Core service** — FastAPI + SQLite (WAL, FTS5) + background worker; documents + search
   (`memories`/`hybrid` via embeddings provider, configurable incl. local); containerTags;
   Bearer auth; docker-compose one-command run. Gate: ingest→search roundtrip test.
2. **Dreaming/graph** — LLM extraction pass → fact graph (subject–predicate–object +
   timestamps, supersede on contradiction); `dynamic`/`instant` modes. Gate: contradiction
   supersede test + LongMemEval-style spot checks.
3. **Profiles/filters/extras** — profile endpoint, metadata filters, rerank/rewrite flags,
   customId upserts, scoped keys. Gate: per-feature tests.
4. **Clients** — OpenCode V2 plugin (reference port exists), MCP server, Python/TS SDKs.
5. **Release** — README quickstart, CI (ruff + pytest), versioning, changelog.

## Non-goals

- Multi-modal OCR/transcription at parity on day one (text/markdown/code first)
- Managed cloud offering (self-host only; others may host under AGPL terms)
