# Mem0 compatibility profile

**Contract:** `mem0-self-hosted-v0.2`
**Official SDK target:** `mem0ai==2.2.0` (Python) and the dependency-free
TypeScript route probe
**Status:** explicit self-hosted HTTP profile; not hosted-service parity

Memoratum exposes a versioned compatibility layer for clients that need the
familiar Mem0 request/response shapes. It covers the routes implemented in this
repository and does not claim to implement every hosted Mem0 API, SDK release,
or control-plane feature.

## Capability status

| Operation | Status | Routes |
|---|---|---|
| Add memories | Supported | `POST /v3/memories/add/`, `POST /v1/memories/` |
| Poll an add event | Supported | `GET /v1/event/{event_id}/` |
| Search memories | Supported | `POST /v3/memories/search/`, `POST /v1/memories/search/` |
| List current memories | Supported | `GET /v1/memories/`, `POST /v3/memories/` |
| Get one memory | Supported | `GET /v1/memories/{memory_id}/` |
| Update one memory | Supported | `PUT /v1/memories/{memory_id}/` |
| Delete one memory | Supported | `DELETE /v1/memories/{memory_id}/` |
| Delete all in an entity scope | Supported | `DELETE /v1/memories/` |
| Memory history | Supported | `GET /v1/memories/{memory_id}/history/` |
| Atomic batch update/delete | Supported | `PUT /v1/batch/`, `DELETE /v1/batch/` |
| Native memory lifecycle | Supported | `POST/GET/PATCH/DELETE /v4/memories...` |
| Categorization event | Supported | `POST /v4/memories/{id}/categorize` |
| Native async bulk | Supported | `POST /v4/memories/bulk` (per-item job progress) |
| Ping and project/member routes | Supported | `GET /v1/ping/`, `/api/v1/orgs/...` |
| Project webhooks | Supported | `/api/v1/webhooks/...` |
| Managed billing, quotas, invitations | Out of scope | — |

The machine-readable matrix in `src/memoratum/mem0_contract.py` is the source
of truth for capability status. Versioned fixtures live under
`tests/fixtures/mem0/v0.2/`; the older v0.1 fixtures remain regression tests.

## Authentication and scope

Authentication accepts `Authorization: Token <key>` and the native
`Authorization: Bearer <key>` form. Every add, search, and list request must
identify exactly one supported entity scope: `user_id`, `agent_id`, `app_id`,
or `run_id`. The ID is encoded as `mem0:<entity>:<value>`. Project-scoped keys
additionally restrict reads, writes, events, and webhook management to their
project. New keys default to `READER`; data mutations, project management, and
destructive project operations require an explicit project-bound `OWNER` role. Missing,
malformed, and cross-scope IDs do not reveal another scope's data.

## Canonical memory lifecycle

Memories have stable `mem_<uuid>` IDs, text and metadata, timestamps, version,
expiration, scope links, and deletion state. Updates append history and enqueue
provider reindexing. Deletes remove active content and vectors while retaining a
redacted tombstone and content hash. History is a list-shaped, append-only
compatibility response. Expired records are hidden unless `show_expired` is
requested.

`POST /v3/memories/add/` is asynchronous and returns
`{"event_id":"<job-id>","status":"PENDING"}`. An add with `infer=false` (or no
configured LLM) creates canonical input memories; with an LLM, extracted facts
are projected into canonical memories during the worker dream step.

## Bulk operations

`PUT /v1/batch/` and `DELETE /v1/batch/` accept the official shape:

```json
{"memories":[{"memory_id":"mem_...","text":"updated"}]}
```

Batches contain 1–1000 unique items, validate every item before writing, and
commit atomically for SQLite. The response is message-style, for example
`{"message":"Successfully updated 1 memories"}`. Native large jobs use
`POST /v4/memories/bulk` (aliases `/v4/memories/batch` and `/v4/jobs/bulk`),
return a job ID, and expose per-item progress in the job result. Reindexing
remains asynchronous in the local worker.

## Organizations, projects, and members

The first boot seeds `local-org` and `local-project`. `GET /v1/ping/` returns
that context for a wildcard/admin key and the bound context for a project key.
The official project and member paths are implemented for local management:

- `POST/GET /api/v1/orgs/organizations/{org_id}/projects/`
- `GET/PATCH /api/v1/orgs/organizations/{org_id}/projects/{project_id}/`
- `GET/POST/PUT/DELETE /api/v1/orgs/organizations/{org_id}/projects/{project_id}/members/`

This is local RBAC and configuration only. There are no invitations, remote
organization control plane, hosted billing, or quota service.

## Webhooks

Webhooks are disabled by default. An administrator or authorized project
administrator explicitly creates a project-scoped endpoint with the official
route shape:

```http
POST /api/v1/webhooks/projects/{project_id}/
{"url":"https://example.test/hook","name":"Memory Logger","event_types":["memory_add"]}
```

The create response returns the signing secret once. List/get responses redact
it, and the stored value is encrypted at rest. `PUT` accepts `is_active` to
pause/resume delivery. Supported events are `memory_add`, `memory_update`,
`memory_delete`, `memory_categorize`, and the documented ingest-job events. The
local outbox and worker provide at-least-once delivery, stable delivery IDs,
HMAC-SHA256 signatures, exponential retry, dead-letter state, and authorized
replay.

Webhook URLs require HTTPS and reject private, loopback, link-local,
multicast, reserved, and metadata addresses after DNS resolution. Redirects are
disabled. Local HTTP/private targets require the explicit development-only
settings `MEMORATUM_ENV=development` (or `test`) and
`MEMORATUM_WEBHOOK_ALLOW_PRIVATE_TARGETS=true`.

See [`docs/runbooks/webhooks.md`](runbooks/webhooks.md) for receiver
verification and operations.

## Errors and versioning

Errors use the stable envelope:

```json
{"error":{"code":"VALIDATION_ERROR","message":"..."}}
```

A breaking change to request validation, route mapping, response shape, or scope
isolation increments the contract version and updates fixtures, capability
status, tests, and this document together. Optional additive fields do not
break existing clients. The profile remains self-hosted and adds no telemetry;
audit and usage accounting stay in the local SQLite database.
