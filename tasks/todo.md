# Unsupported operations plan: task list

**Status:** IMPLEMENTED LOCALLY — release handoff and hosted-only work remain out of scope

## Phase 0 — Contract and identity foundation

- [x] **Task 0.1: Approve the capability map and compatibility boundary**
  - Acceptance: `memory-identity`, `memory-lifecycle`, `bulk-operations`, `organizations-projects`, `webhook-delivery`, and `client-surface` have stable IDs, acyclic dependencies, and an explicit build order; self-hosted scope, atomic compatibility bulk semantics, default project seeding, redacted delete history, official webhook event coverage, and the development-only private-network webhook exception are recorded.
  - Verify: human review of `tasks/plan.md`; capability map agrees with `docs/MEM0_COMPATIBILITY.md` and `src/memoratum/mem0_contract.py` after implementation begins.
  - Files: `tasks/plan.md`, `docs/MEM0_COMPATIBILITY.md`
  - Dependencies: none
  - Scope: S

- [x] **Task 0.2: Version the v0.2 request/response fixtures**
  - Acceptance: fixtures cover get/update/delete/delete-all/history, batch update/delete, ping, official project/member routes, webhook management/payloads, and explicit unsupported errors; old v0.1 fixtures remain valid; capability statuses change only when routes are implemented.
  - Verify: `pytest tests/test_mem0_contract.py` and fixture manifest validation.
  - Files: `tests/fixtures/mem0/v0.2/*`, `tests/test_mem0_contract.py`, `src/memoratum/mem0_contract.py`
  - Dependencies: Task 0.1
  - Scope: M

- [x] **Task 0.3: Add canonical memory records and public IDs**
  - Acceptance: a memory record has a stable `mem_<uuid>` ID, text/metadata, immutable scope, timestamps, expiration, version, source links, and deletion state; existing fact/document rows remain readable during migration.
  - Verify: migration tests open a v0.1 database, apply the additive migration, preserve row counts, and round-trip a canonical record.
  - Files: `src/memoratum/db.py`, `src/memoratum/facts.py`, `src/memoratum/mem0_contract.py`, `tests/test_db.py`, `tests/test_migration.py`
  - Dependencies: Task 0.1
  - Scope: L

- [x] **Task 0.4: Add atomic idempotency claims**
  - Acceptance: a unique scoped `Idempotency-Key` claim stores a canonical request hash and response/job reference; same key/hash replays, different hash conflicts, and in-flight duplicates have a documented response.
  - Verify: concurrent SQLite tests prove one claim wins and retries do not enqueue duplicate work.
  - Files: `src/memoratum/db.py`, `src/memoratum/app.py`, `tests/test_idempotency.py`
  - Dependencies: Task 0.3
  - Scope: M

### Checkpoint: Foundation

- [ ] Existing add/search/list behavior remains green.
- [ ] v0.1 database migration and backup/restore checks pass.
- [ ] No legacy fact ID is silently changed.
- [ ] Human review approves the canonical ID and idempotency contract.

## Phase 1 — Single-memory lifecycle

- [x] **Task 1.1: Implement canonical get and scoped list**
  - Acceptance: native and Mem0 `get`/list routes return canonical IDs, memory text, metadata, timestamps, expiration, and version; cross-scope IDs return 404; list pagination is bounded.
  - Verify: route tests with admin, scoped key, OIDC identity, missing scope, and cross-scope IDs.
  - Files: `src/memoratum/app.py`, `src/memoratum/mem0_contract.py`, `src/memoratum/db.py`, `tests/test_mem0_lifecycle.py`
  - Dependencies: Task 0.3, Task 0.4
  - Scope: M

- [x] **Task 1.2: Implement update and durable reindexing**
  - Acceptance: partial text/metadata/timestamp/expiration updates preserve identity and scope, append `UPDATE` history, increment version, enqueue reindexing, and return a stable job reference; invalid/empty updates fail without mutation.
  - Verify: update tests for each field, text re-embedding, provider failure/retry, and SQLite/Qdrant/Chroma/pgvector cleanup/upsert behavior.
  - Files: `src/memoratum/app.py`, `src/memoratum/db.py`, `src/memoratum/worker.py`, `src/memoratum/vectorstore.py`, `tests/test_mem0_lifecycle.py`
  - Dependencies: Task 1.1
  - Scope: L

- [x] **Task 1.3: Implement delete, linked deletion, and tombstones**
  - Acceptance: delete removes active content and vectors, records a scoped redacted `DELETE` tombstone by default, supports `delete_linked` without deleting unrelated scopes, returns `cascade_count` when linked deletion is requested, and returns uniform 404/401 behavior.
  - Verify: delete tests cover current-only, linked chain, repeated delete, expired records, all vector backends, and provider outage behavior.
  - Files: `src/memoratum/app.py`, `src/memoratum/db.py`, `src/memoratum/vectorstore.py`, `tests/test_mem0_lifecycle.py`, `tests/test_vector_cleanup.py`
  - Dependencies: Task 1.2
  - Scope: L

- [x] **Task 1.4: Implement delete-all, history, and expiration**
  - Acceptance: delete-all requires at least one explicit entity/project filter and returns an asynchronous `event_id`; history is a list-shaped, append-only compatibility response with native pagination available separately; deleted text is redacted by default; expired records are hidden by default and visible with `show_expired`.
  - Verify: lifecycle contract fixtures, retention tests, authorization tests, and event polling after add/update/delete.
  - Files: `src/memoratum/app.py`, `src/memoratum/db.py`, `src/memoratum/worker.py`, `src/memoratum/mem0_contract.py`, `tests/test_mem0_lifecycle.py`
  - Dependencies: Task 1.3
  - Scope: L

### Checkpoint: Lifecycle

- [ ] Add → get → update → history → delete works end-to-end.
- [ ] Search never returns deleted or cross-scope records.
- [ ] Vector stores converge after every update/delete.
- [ ] Audit and usage records remain local and contain no secrets.

## Phase 2 — Bulk operations

- [x] **Task 2.1: Define atomic batch contracts and limits**
  - Acceptance: `PUT /v1/batch/` and `DELETE /v1/batch/` accept the official `{"memories": [...]}` shape, enforce the official maximum of 1000 items, validate every item before writing, and return the official message-style result.
  - Verify: contract fixture and validation tests for empty, duplicate, missing, oversized, and conflicting items.
  - Files: `src/memoratum/mem0_contract.py`, `tests/test_mem0_contract.py`, `tests/fixtures/mem0/v0.2/*`
  - Dependencies: Task 1.4
  - Scope: M

- [x] **Task 2.2: Implement transactional batch update/delete**
  - Acceptance: all items commit or none commit; each successful item gets history, audit, usage, vector cleanup/reindex, and stable result IDs; idempotent retries do not duplicate changes.
  - Verify: concurrent batch tests, injected failure between items, vector adapter tests, and local official-client replay.
  - Files: `src/memoratum/app.py`, `src/memoratum/db.py`, `src/memoratum/worker.py`, `tests/test_mem0_bulk.py`
  - Dependencies: Task 2.1, Task 0.4
  - Scope: L

- [x] **Task 2.3: Add native async bulk jobs**
  - Acceptance: native callers can submit a large bounded batch, poll a job, inspect per-item progress, and replay safely; compatibility routes remain synchronous/atomic.
  - Verify: worker restart, retry, partial provider failure, and job authorization tests.
  - Files: `src/memoratum/jobs.py`, `src/memoratum/worker.py`, `src/memoratum/app.py`, `tests/test_jobs.py`, `tests/test_mem0_bulk.py`
  - Dependencies: Task 2.2
  - Scope: L

### Checkpoint: Bulk

- [ ] Official Python `batch_update` and `batch_delete` calls pass against a local server.
- [ ] Atomicity and idempotency are proven under concurrent retries.
- [ ] Large native batches have observable job state and bounded memory use.

## Phase 3 — Organizations and projects

- [x] **Task 3.1: Add organization/project/member schema and additive migration**
  - Acceptance: local organizations, projects, memberships, roles, settings, and nullable project scope can be created without destructive changes to existing rows; indexes support scoped reads and purge.
  - Verify: fresh and v0.1 migration tests, foreign-key checks, backup/restore, and legacy/default read-mode tests.
  - Files: `src/memoratum/db.py`, `tests/test_org.py`, `tests/test_migration.py`
  - Dependencies: Task 0.3
  - Scope: L

- [x] **Task 3.2: Bind API keys and OIDC identities to projects**
  - Acceptance: admin/wildcard, organization owner/admin/member, and project owner/reader permissions are enforced; scope cannot be changed through request metadata; a default project context can be returned by `ping` according to the approved migration decision.
  - Verify: RBAC matrix tests, cross-org/project 404 tests, OIDC verifier seam tests, and key issuance/revocation tests.
  - Files: `src/memoratum/auth.py`, `src/memoratum/app.py`, `src/memoratum/db.py`, `tests/test_org.py`, `tests/test_hardening.py`
  - Dependencies: Task 3.1
  - Scope: L

- [x] **Task 3.3: Implement native organization/project/member APIs**
  - Acceptance: admin can manage organizations/projects/members and project settings; destructive project/org operations enqueue auditable purge jobs and never leak cross-scope data.
  - Verify: route contract tests, role tests, purge restart/idempotency tests, and local audit/usage assertions.
  - Files: `src/memoratum/app.py`, `src/memoratum/db.py`, `src/memoratum/worker.py`, `tests/test_org.py`
  - Dependencies: Task 3.2
  - Scope: L

- [x] **Task 3.4: Implement Mem0 ping/project/member compatibility routes**
  - Acceptance: `GET /v1/ping/` resolves the default seeded organization/project, and the official project/member route shapes work with the pinned Python client; `READER`/`OWNER` roles are enforced; unsupported managed fields return a clear validation error rather than a fake success.
  - Verify: expanded official Python contract server tests and FastAPI route replay for project get/update/delete/member operations.
  - Files: `src/memoratum/app.py`, `src/memoratum/mem0_contract.py`, `tests/test_mem0_organizations.py`, `tests/test_mem0_official_sdk.py`
  - Dependencies: Task 3.3
  - Scope: L

### Checkpoint: Organizations

- [ ] Existing local data remains accessible under the approved legacy/default policy.
- [ ] RBAC and key isolation pass an adversarial matrix.
- [ ] No invitations, billing, or remote organization control plane was introduced.

## Phase 4 — Webhooks and local delivery

- [x] **Task 4.1: Add domain-event outbox and webhook schema**
  - Acceptance: memory add/update/delete and approved event types are recorded transactionally with project scope, event ID, payload version, and retention metadata; endpoint secrets are protected and never returned after creation/rotation.
  - Verify: migration tests, transaction rollback tests, secret-at-rest inspection, and audit redaction tests.
  - Files: `src/memoratum/db.py`, `src/memoratum/webhooks.py`, `tests/test_webhooks.py`
  - Dependencies: Task 1.4, Task 3.3
  - Scope: L

- [x] **Task 4.2: Implement safe webhook delivery worker**
  - Acceptance: HTTPS/target validation, SSRF defenses, HMAC signatures, timeout/size/rate limits, exponential retry, dead-letter state, stable delivery IDs, and authorized replay are enforced; private-network targets work only with the explicit development-only flag.
  - Verify: local HTTP test server with signature verification, DNS/IP rejection cases, retry timing, duplicate processing, replay, and provider outage tests.
  - Files: `src/memoratum/webhooks.py`, `src/memoratum/worker.py`, `src/memoratum/jobs.py`, `tests/test_webhooks.py`, `tests/test_worker.py`
  - Dependencies: Task 4.1
  - Scope: L

- [x] **Task 4.3: Implement native and Mem0 webhook management routes**
  - Acceptance: project admins can list/create/update/delete webhooks; subscriptions support the four official memory events plus the four documented ingest-job events; compatibility routes use the official project/webhook path shapes and return redacted secrets.
  - Verify: official Python client route replay, FastAPI RBAC tests, CSRF/SSRF/secret-leak tests, and contract fixtures.
  - Files: `src/memoratum/app.py`, `src/memoratum/webhooks.py`, `tests/test_webhooks.py`, `tests/test_mem0_webhooks.py`
  - Dependencies: Task 4.2
  - Scope: L

- [x] **Task 4.4: Add local delivery history and dashboard view**
  - Acceptance: authorized users can inspect event IDs, attempt counts, next retry, terminal status, and replay action without seeing secrets or unredacted sensitive payloads.
  - Verify: dashboard build, keyboard/screen-reader review, API authorization tests, and empty/error/loading states.
  - Files: `dashboard/src/api.ts`, `dashboard/src/types.ts`, `dashboard/src/views/*`, `tests/test_dashboard.py`
  - Dependencies: Task 4.3
  - Scope: L

### Checkpoint: Webhooks

- [ ] No webhook is created or contacted without explicit opt-in.
- [ ] A local endpoint can verify signatures and deduplicate retries.
- [ ] SSRF, secret leakage, replay authorization, and redacted logs are covered by tests.
- [ ] No telemetry SDK or remote exporter appears in the dependency or build graph.

## Phase 5 — Clients, dashboard, docs, and release

- [x] **Task 5.1: Expand official Python SDK contract coverage**
  - Acceptance: pinned `mem0ai==2.2.0` tests cover get, update, delete, delete-all, history, batch update/delete, entity listing/deletion, ping, project/member, and webhook methods that are claimed supported; the test server reproduces the documented request paths, methods, payloads, and response envelopes.
  - Verify: `pytest tests/test_mem0_official_sdk.py` with `MEM0_TELEMETRY=false`; every unsupported method remains explicitly marked.
  - Files: `tests/test_mem0_official_sdk.py`, `tests/fixtures/mem0/v0.2/*`, `pyproject.toml`
  - Dependencies: Tasks 1.4, 2.2, 3.4, 4.3
  - Scope: M

- [x] **Task 5.2: Extend the dependency-free TypeScript probe and client helpers**
  - Acceptance: route/header/payload probes cover all claimed compatibility routes; no upstream telemetry package is added as a dependency; client code remains MIT.
  - Verify: `node --experimental-strip-types clients/ts/test.ts`, TypeScript build, license check, and telemetry guard.
  - Files: `clients/ts/mem0-contract.ts`, `clients/ts/test.ts`, `clients/ts/index.ts`
  - Dependencies: Tasks 1.4, 2.2, 3.4, 4.3
  - Scope: M

- [x] **Task 5.3: Add dashboard memory lifecycle and history views**
  - Acceptance: authorized users can inspect, edit, delete, and view history for a memory; bulk selection and job progress have accessible loading/error/empty states.
  - Verify: dashboard build, focused UI tests, keyboard navigation, focus restoration, and responsive checks.
  - Files: `dashboard/src/api.ts`, `dashboard/src/types.ts`, `dashboard/src/views/*`, `dashboard/src/App.tsx`
  - Dependencies: Tasks 1.4, 2.2
  - Scope: L

- [x] **Task 5.4: Publish migration/operations documentation**
  - Acceptance: `docs/MEM0_COMPATIBILITY.md`, `docs/API.md`, `docs/PARITY.md`, webhook runbook, retention guidance, and a clear self-hosted-versus-hosted boundary describe only shipped behavior.
  - Verify: docs link check, command examples against a local server, and review against the capability matrix.
  - Files: `docs/MEM0_COMPATIBILITY.md`, `docs/API.md`, `docs/PARITY.md`, `docs/SECURITY.md`, `docs/runbooks/webhooks.md`
  - Dependencies: Tasks 5.1–5.3
  - Scope: M

- [ ] **Task 5.5: Run final quality, security, evaluation, and release gates**
  - Acceptance: Python/TypeScript/dashboard tests, Ruff, no-telemetry guard, plugin check, security checks, local Ollama dogfood, and published evaluation status all pass; version/tag/changelog agree.
  - Verify: `pytest`, `ruff check .`, `ruff format --check .`, dashboard build, CI workflow, release tag verification, and PR review record.
  - Files: `.github/workflows/ci.yml`, `pyproject.toml`, `CHANGELOG.md`, `docs/EVALUATION.md`
  - Dependencies: Tasks 5.1–5.4
  - Scope: M

### Final checkpoint

- [x] Capability matrix has no route marked supported without contract and integration coverage.
- [x] All new writes are scoped, auditable, idempotent where documented, and safe under retries.
- [x] SQLite remains the default and all supported vector adapters pass lifecycle cleanup tests.
- [x] Official Python and TypeScript compatibility claims match the shipped routes.
- [x] Webhooks are opt-in, signed, SSRF-hardened, encrypted at rest, and locally observable; private-network targets require the explicit development-only setting.
- [ ] Release is merged, reviewed, tagged, and reproducible from the tag.

## Local completion evidence (2026-09-30)

The implementation and verification tasks above are complete on
`feature/memory-identity`. The full Python suite, Ruff, TypeScript probe,
dashboard build, and no-telemetry guard are run locally. GitHub release,
pull-request, and tag operations are intentionally not performed in this
self-hosted workspace.
