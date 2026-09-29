# Implementation Plan: Mem0 self-hosted operation parity

**Status:** IMPLEMENTED LOCALLY — self-hosted scope shipped; release handoff remains

## Overview

Extend the current `mem0-self-hosted-v0.1` profile with a canonical memory lifecycle, bounded bulk operations, local organizations/projects, and opt-in outbound webhooks. The work stays self-hosted: SQLite remains the default, provider-neutral vector storage remains supported, asynchronous ingest/reindex jobs remain durable, and all audit/usage data remains local. This is a compatibility shim, not a claim of hosted Mem0 service parity.

The implementation must first introduce a stable memory identity and event/history model. The current compatibility `add` route creates a document and later extracts facts; search can return synthetic `mem_<fact_id>` IDs while list returns raw fact IDs. Update, delete, history, and bulk operations cannot be safely implemented against that mismatch without a canonical memory record first.

## Capability Map

| Module ID | Responsibility | Depends on | First shippable slice |
|---|---|---|---|
| `memory-identity` | Canonical memory records, public IDs, metadata/expiration, async event results, idempotency, and vector/source links | — | Add → search/list returns stable IDs and event results |
| `memory-lifecycle` | Get, partial update, delete, delete-all, history, expiration, linked-memory cleanup | `memory-identity` | One memory can be added, found, updated, deleted, and inspected |
| `bulk-operations` | Atomic bounded batch update/delete and compatibility batch routes | `memory-lifecycle` | A caller can update/delete up to the documented batch limit safely |
| `organizations-projects` | Local organizations, projects, membership roles, project settings, key/identity binding, lifecycle jobs | `memory-identity` | Admin can create a local project and a scoped key can use it |
| `webhook-delivery` | Opt-in project webhooks, signed event envelopes, durable delivery, retry/replay, SSRF controls | `organizations-projects`, `memory-lifecycle` | A configured local endpoint receives a signed add event |
| `client-surface` | Official Python contract coverage, dependency-free TypeScript probe, dashboard lifecycle/event views, docs and evaluation | all preceding modules | Users can operate the new APIs without hidden compatibility gaps |

**Dependency direction:** `memory-identity → memory-lifecycle → bulk-operations`; `memory-identity + organizations-projects → webhook-delivery`; all modules → `client-surface`. No module may depend on a later module.

## Assumptions and proposed defaults

These are decisions to confirm before implementation:

1. **Scope (confirmed):** self-hosted compatibility only for now. No managed billing, quotas-as-a-service, email invitations, or hosted control plane. Hosting remains a later initiative.
2. **Shared core:** native `/v4` routes and Mem0 compatibility routes call one service layer; route handlers do not duplicate lifecycle logic.
3. **Public identity:** use a canonical `mem_<uuid>` ID for compatibility memory records. Accept legacy raw fact IDs during the migration window, but return the canonical ID everywhere after cutover.
4. **Delete:** remove the active content and vector immediately, retain a local history tombstone for audit/history. Whether deleted text is retained in history should be configurable; the default must be documented before implementation.
5. **Update semantics:** partial update of `text`, `metadata`, `timestamp`/event time, and `expiration_date`; scope (`org_id`, `project_id`, entity fields) is immutable and cannot be changed through metadata.
6. **Expiration:** accept Mem0 `YYYY-MM-DD` values and normalize to UTC end-of-day; expired records are hidden by default and remain queryable with `show_expired=true` until the retention job removes them.
7. **Async behavior:** add remains asynchronous and event-based. Update/delete return a compatible success response after the authoritative SQLite mutation; vector reindexing is queued and observable through job status. A failed reindex must not silently restore deleted content.
8. **Organizations (confirmed default):** seed a local default organization/project on first boot, bind the wildcard key to it, and preserve legacy null-organization reads. Local RBAC uses `OWNER`/`READER` compatibility roles plus native admin capabilities; destructive backfill is not part of the first release.
9. **Webhooks:** outbound, explicitly configured, project-scoped notifications only. They are not telemetry and must never be enabled by default. No external analytics, PostHog, or hosted event bus is added.
10. **Retries:** state-changing routes support `Idempotency-Key`; the key is scoped to the caller, request hashes are checked, and retention exceeds the longest retry/replay window.
11. **Bulk (confirmed):** Mem0 batch update/delete routes are bounded and atomic; large native bulk operations use a separate asynchronous job with per-item progress.

## Current architecture fit

- `facts` already provides temporal validity and supersession, but it is a graph projection rather than a complete Mem0 memory record.
- `documents`, `chunks`, and `jobs` already provide asynchronous ingest and durable processing.
- `VectorStore` already exposes scoped deletion; update/reindex should use the same provider-neutral contract for SQLite, Qdrant, Chroma, and pgvector.
- `org_id` and scoped keys provide isolation groundwork, but there are no organization/project/member records or role checks.
- `audit_events` and `usage_counters` are local and should record every lifecycle/admin operation; webhook delivery state belongs in separate local tables, not in remote telemetry.
- The current `/v4/memories/{fact_id}` delete route is a fact hard-delete. It must be retained for native callers or deliberately migrated; it must not silently become the new canonical memory delete contract.

## Target contract

### Native lifecycle API

- `POST /v4/memories` — create a memory record through the shared lifecycle service.
- `GET /v4/memories/{memory_id}` — retrieve one scoped memory.
- `PATCH /v4/memories/{memory_id}` — partial update; returns the new version and reindex job ID.
- `DELETE /v4/memories/{memory_id}` — delete one memory, with an explicit `delete_linked` option where supported.
- `DELETE /v4/memories` — delete all memories only with an explicit scope/filter; no accidental global delete.
- `GET /v4/memories/{memory_id}/history` — paginated local history.
- `POST /v4/memories/batch` or `POST /v4/memories/bulk` — native bounded batch operation.

### Mem0 compatibility aliases

- `GET /v1/memories/{memory_id}/`
- `PUT /v1/memories/{memory_id}/` for `text`, `metadata`, `timestamp`, and `expiration_date`
- `DELETE /v1/memories/{memory_id}/?delete_linked=...`
- `DELETE /v1/memories/?...filters` for scoped delete-all
- `GET /v1/memories/{memory_id}/history/`
- `PUT /v1/batch/` and `DELETE /v1/batch/` with `{"memories": [...]}`
- `POST /v3/memories/` for the official `get_all` collection call, distinct from `POST /v3/memories/add/`
- `GET /v1/ping/` returning the local org/project context required by the official SDK
- Mem0-compatible project/member and webhook routes listed in the organization/webhook phases below.

Every new route uses the existing `{"error":{"code","message"}}` envelope, returns 404 for cross-scope IDs, validates all body/query values, supports bounded pagination, and records local audit/usage events.

## Data and migration plan

Use additive, forward-only migrations. Each migration must work with both the previous and new application code before the next migration is applied.

1. `memories`: canonical ID, text, metadata, scope (`container_tag`, `org_id`, `project_id`), expiration, timestamps, version, source document/fact links, hash, and deletion state.
2. `memory_history`: append-only `ADD`/`UPDATE`/`DELETE` records with old/new snapshots or redacted tombstones, version, actor fingerprint, and correlation ID.
3. `idempotency_keys`: unique `(scope, key)` claim, canonical request hash, response/job reference, state, expiry, and conflict detection.
4. `organizations`, `projects`, `organization_members`, and `project_members`: local tenant/project registry and RBAC.
5. Add nullable `project_id` and lifecycle columns to documents, facts, vector points, jobs, audit events, usage counters, and API keys; add indexes for project/scope/history lookup.
6. `domain_events`: transactional local outbox for webhook-eligible changes.
7. `webhooks` and `webhook_deliveries`: endpoint configuration, encrypted/secret-protected signing material, delivery state, attempts, next retry, response metadata, and manual replay references.

Do not drop or rename old columns in the same release. Backfill canonical memory IDs in bounded batches, keep a compatibility alias for legacy fact IDs, and verify counts/checksums before making the new ID mandatory.

## Delivery and security controls

- **Authorization:** derive organization/project scope from the verified key or injected OIDC identity; never trust caller-supplied scope fields. Use 404 for out-of-scope memory IDs. Enforce project/organization roles on every read and mutation.
- **Idempotency:** claim keys with a unique constraint in the same transaction as the intent; replay the stored response for the same hash and return 409/422 for a conflicting hash.
- **Webhook SSRF:** require HTTPS by default; resolve and reject loopback, private, link-local, multicast, and metadata-service addresses; disable redirects or revalidate every hop; enforce timeout, response-size, and rate limits. Permit local HTTP only behind an explicit development setting.
- **Webhook signing:** generate a per-endpoint secret, return it only at creation/rotation, sign a canonical timestamped envelope with HMAC-SHA256, and never log the secret or full memory payload.
- **Delivery:** write the domain event in the same SQLite transaction as the state change, deliver asynchronously with at-least-once semantics, exponential backoff, bounded attempts, dead-letter/manual replay, and a stable event/delivery ID for consumer deduplication.
- **Local observability:** correlation IDs, job status, delivery attempts, and audit records stay local. Do not introduce OpenTelemetry exporters, PostHog, or other remote telemetry as part of this work.
- **Privacy:** history retention, deleted-text retention, webhook payload fields, and organization deletion are explicit configuration/retention decisions documented before release.

## Build order and PR slices

Each slice is implemented on a short-lived branch, tested, and committed
atomically. In this workspace the local commits are complete; GitHub PR,
review, merge, and tag operations are intentionally left to the release owner.

1. **Contract and identity foundation** — capability map approval, v0.2 fixtures, canonical memory schema, public IDs, idempotency, and event result shape.
2. **Single-memory lifecycle** — get/update/delete/delete-all/history/expiration with vector cleanup and local audit/usage.
3. **Bulk operations** — bounded atomic update/delete routes, job/result semantics, official Python batch probes.
4. **Organizations and projects** — schema/backfill, local RBAC, key/OIDC binding, ping/project/member compatibility routes, project purge jobs.
5. **Webhooks** — outbox, endpoint security, signing, retry/replay worker, compatibility management routes, delivery UI.
6. **Clients, dashboard, docs, and release** — expanded official SDK contract, TypeScript route probe, memory lifecycle/history UI, webhook event history, migration guide, evaluation publication, and release tag.

## Verification gates

- Python unit/integration suite, including migration, concurrency, scope isolation, expiration, vector cleanup, and idempotency tests.
- Official `mem0ai==2.2.0` Python client contract tests for every supported method, with `MEM0_TELEMETRY=false` and a local contract server.
- Dependency-free TypeScript route/payload probe; do not add the upstream package as a repository dependency because it contains telemetry code.
- Qdrant/Chroma/pgvector adapter tests for update/delete behavior and scoped cleanup.
- Webhook tests for HMAC verification, SSRF rejection, retry/backoff, duplicate delivery IDs, replay authorization, and secret redaction.
- Ruff, dashboard build, OpenCode plugin check, and the repository's no-telemetry/security guards.
- Manual dogfood with local Ollama embeddings; publish separate LongMemEval-S and LoCoMo-MC10 results rather than treating fixture scores as full benchmark results.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Synthetic fact IDs do not represent durable Mem0 memories | High | Land `memory-identity` before lifecycle routes; keep legacy aliases during migration |
| SQLite and external vector stores diverge during update/delete | High | Authoritative SQLite transaction, queued provider reindex, reconciliation job, provider contract tests |
| Scope leakage through metadata or guessed IDs | Critical | Central authorization service, immutable scope fields, 404 probing behavior, adversarial tests |
| Duplicate retries create duplicate jobs/events | High | Unique idempotency claims, request hashes, stable event IDs, at-least-once consumer guidance |
| Webhook SSRF or secret leakage | Critical | URL/IP validation, no redirects by default, encrypted secrets, redacted structured logs, explicit opt-in |
| Organization migration strands existing local data | High | Additive nullable project scope, legacy/default read mode, backup and count/checksum verification before backfill |
| Compatibility surface drifts with upstream SDK | Medium | Versioned fixtures, pinned official SDK, changelog and capability matrix, explicit partial-profile language |
| Package/tag version mismatch | Medium | Reconcile `pyproject.toml`, release tag, and changelog before the next release cut |

## Finalized decisions from research

The open questions are resolved as follows:

1. **Deleted history:** default to redacted tombstones for `DELETE` history. Store the memory ID, event type, actor fingerprint, timestamp, version, and content hash; do not retain deleted text by default. Provide an explicit, documented retention mode for operators who require full forensic history.
2. **Initial webhook events:** support the official memory event set `memory_add`, `memory_update`, `memory_delete`, and `memory_categorize`, plus the four documented ingest-job events: `ingest_job_completed`, `ingest_job_partially_completed`, `ingest_job_failed`, and `ingest_job_cancelled`. Organization/member events are deferred.
3. **Webhook network policy:** private, loopback, link-local, multicast, and metadata-service targets are rejected by default. Local/private targets are available only when an explicit development-only configuration flag is enabled; the flag must be impossible to enable accidentally in production and must emit a clear security warning.
4. **Dashboard scope:** the first organizations slice includes basic organization/project selection and membership/project settings. Full organization administration is included only where it is needed to manage local projects and keys; memory lifecycle and webhook delivery remain separate views.
5. **Delete-all safety:** require at least one explicit filter; support the documented wildcard form only for an explicitly authorized administrative/project operation, never as an accidental no-filter delete.
6. **Compatibility responses:** preserve the official route shapes and response envelopes, including paginated `get_all`, `event_id` for asynchronous delete-all, `cascade_count` for linked deletion, and list-shaped history. Unsupported hosted-only fields remain explicit validation errors or documented omissions rather than fabricated success responses.

## Source anchors

The route shapes and lifecycle semantics above were checked against the pinned upstream source, not inferred from the current partial shim:

- Official API reference overview and product boundary: https://docs.mem0.ai/llms.txt and https://docs.mem0.ai/api-reference
- Get memory and paginated get-all contracts: https://docs.mem0.ai/api-reference/memory/get-memory and https://docs.mem0.ai/api-reference/memory/get-memories
- Update/delete/delete-all/history contracts: https://docs.mem0.ai/api-reference/memory/update-memory, https://docs.mem0.ai/api-reference/memory/delete-memory, https://docs.mem0.ai/api-reference/memory/delete-memories, and https://docs.mem0.ai/api-reference/memory/history-memory
- Batch update/delete limits and payloads: https://docs.mem0.ai/api-reference/memory/batch-update and https://docs.mem0.ai/api-reference/memory/batch-delete
- Organization/project context and roles: https://docs.mem0.ai/api-reference/organizations-projects and https://docs.mem0.ai/api-reference/project/get-project
- Official webhook event types and payload shape: https://docs.mem0.ai/platform/webhooks and https://docs.mem0.ai/api-reference/webhook/create-webhook
- Official SDK route implementations: https://github.com/mem0ai/mem0/blob/v2.2.0/mem0/client/main.py#L463-L742, https://github.com/mem0ai/mem0/blob/v2.2.0/mem0/client/project.py#L330-L620, and https://github.com/mem0ai/mem0/blob/v2.2.0/mem0/client/main.py#L1093-L1206
- Upstream history semantics and storage: https://github.com/mem0ai/mem0/blob/v2.2.0/mem0/memory/main.py#L1815-L1948 and https://github.com/mem0ai/mem0/blob/v2.2.0/mem0/memory/storage.py#L1-L240
- SSRF controls for user-configured webhook URLs: https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html

This plan is research-backed and finalized for the self-hosted scope. Implementation should still proceed through the existing human-reviewed, commit → PR → review → merge → release workflow.
