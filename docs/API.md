# Memoratum HTTP API

Base URL autodetects nothing — pass it explicitly (default server: `http://localhost:6767`).
Auth: `Authorization: Bearer <key>`; admin key, container-scoped key, or an
optional `org_id`/`project_id` scope. Error shape everywhere:
`{"error": {"code": "...", "message": "..."}}`. All write/read routes validate bodies (422 on invalid).

## `POST /v3/documents` → 201

| Field | Type | Required | Notes |
|---|---|---|---|
| `content` | string | yes | min length 1, no max (see abuse limits in SECURITY.md) |
| `containerTag` | string | no | default `"default"` — hard isolation boundary |
| `customId` | string \| null | no | stable identity → idempotent re-ingest (clears chunks + dreamed state) |
| `dreaming` | `"dynamic"` \| `"instant"` | no | default `"dynamic"`; skipped entirely when no LLM is configured |
| `metadata` | object \| null | no | stored as JSON, usable in `filters` |
| `org_id` | string \| null | no | optional organization scope; scoped keys may only use their own value |
| `project_id` | string \| null | no | optional project scope; project-bound keys may only use their own value |

Response: `{"id": "<hex>", "status": "queued", "job_id": "<job-id>"}`. Ingest and dreaming run asynchronously in the local worker; poll `GET /v4/jobs/{job_id}` or the document route.

## `GET /v3/documents/{id}` → 200 | 404

Returns `{id, containerTag, status}`. Missing *or* out-of-scope ids read as 404 (no cross-tag probing); missing/invalid credentials → 401.

## `PATCH /v3/documents/{id}` → 200 | 404

Replaces `content` and/or `metadata`, clears derived chunks, and queues a fresh ingest.
Out-of-scope documents use the same uniform 404 behavior as reads.

## `GET /v3/documents?containerTag=&org_id=&limit=&offset=` → 200

Lists documents in one container and organization scope. `limit` is capped at 100;
the response contains `documents` and `total` for pagination.

## `POST /v4/search` → 200

| Field | Type | Default | Notes |
|---|---|---|---|
| `q` | string | required | min length 1; FTS leg sanitizes punctuation |
| `containerTag` | string | `"default"` | mandatory filter, always applied |
| `org_id` | string \| null | `null` | optional organization filter; scoped keys default to their own scope |
| `searchMode` | `"hybrid"` \| `"memories"` \| `"documents"` | `"hybrid"` | facts vs chunks vs both |
| `limit` | 1–100 | `10` | |
| `threshold` | ≥ 0 | `0.0` | applied to fused RRF scores (small numbers — see ARCHITECTURE.md) |
| `filters` | object \| null | `null` | AND-equality over metadata |
| `rerank` | bool | `false` | recency-blended rescoring of top candidates |

Response: `{results: [{id, memory?|chunk, similarity}], timing: <ms>, total: <n>}`.

## Native memory lifecycle

The native `/v4` lifecycle uses the same canonical `mem_<uuid>` records as the
compatibility routes:

- `POST /v4/memories` — create a memory with `text`, `containerTag`, optional
  `metadata`, `expires_at`, `org_id`, and `project_id`; returns the record and a
  reindex `job_id`.
- `GET /v4/memories/{memory_id}` — retrieve one scoped record.
- `PUT|PATCH /v4/memories/{memory_id}` — partial text/metadata/timestamp/
  expiration update; returns the new version and reindex job.
- `DELETE /v4/memories/{memory_id}` — soft-delete with a redacted tombstone and
  vector cleanup. Legacy fact IDs are resolved during the migration window.
- `GET /v4/memories/{memory_id}/history` — append-only local history.
- `DELETE /v4/memories?user_id=...` — explicit-filter asynchronous delete-all.

Cross-scope IDs return the same 404 shape as missing IDs. Updates cannot change
`containerTag`, organization, or project through metadata.

## Native asynchronous bulk

`POST /v4/memories/bulk` (aliases: `/v4/memories/batch`, `/v4/jobs/bulk`)
accepts 1–1000 unique operations and returns `202`:

```json
{"containerTag":"mem0:user_id:alice","operations":[
  {"memory_id":"mem_...","action":"update","text":"new text"},
  {"memory_id":"mem_...","action":"delete"}
]}
```

Poll the returned job. Its `result` contains `total`, `completed`, `failed`, and
per-item `status`/`error` fields. SQLite mutations are committed per item so a
provider failure cannot roll back already-authoritative work; failed vector
work is reindexed by a follow-up job. Compatibility `PUT/DELETE /v1/batch/`
remain synchronous and atomic.


| Field | Type | Default | Notes |
|---|---|---|---|
| `subject`/`predicate`/`object` | string | required | min length 1 |
| `containerTag` | string | `"default"` | auth-scoped like everything else |
| `metadata` | object \| null | `null` | stored as JSON |
| `supersede` | bool | `true` | `false` for multi-valued relations (calls/contains/imports) so siblings coexist |
| `memory_type` | string | `"semantic"` | memory classification (procedural, episodic, semantic, or an application-defined label) |
| `org_id` | string \| null | `null` | optional organization scope; scoped keys default to their own scope |

Same `(subject, predicate, object)` re-asserts (reviving a superseded row) instead of duplicating. Returns the full fact row.

## `GET /v4/facts?containerTag=` → 200 | 422

`{facts: [...full rows...], total}` with optional `include_superseded=true`, `memory_type`, `org_id`, `limit` (default 100, capped at 500), and `offset`. `containerTag` required (422 when missing). Powers diff-sync clients.

## `GET /v4/profile?containerTag=` → 200

`{containerTag, facts: ["subject predicate object", …≤20], stats: {documents, chunks, facts}}` (fact count is exact, sample capped). Pass `org_id` to scope the profile.

## `POST /v4/keys` → 201

Body `{containerTag: string|null, org_id: string|null, project_id: string|null, role: "OWNER"|"READER"}`. New keys default to `READER`; project data mutations and management require an explicitly project-bound `OWNER` key. Open mode or admin → `{key: "mm_..."}` (shown once); known non-admin key → 403; otherwise 401.

## `POST /v4/keys/revoke` → 200

Admin only (403 otherwise). Body `{key: "<raw>"}` → `{revoked: true|false}`. Revoked keys 401 everywhere immediately.

## `DELETE /v4/memories/{fact_id}` → 200 | 404

Hard-deletes one fact. Missing or out-of-scope → 404; bad credentials → 401. Response `{deleted: id}`.

## `DELETE /v4/tags/{tag}` → 200

Purges the tag's memories, documents (chunks cascade), facts, vectors, and related webhook event payloads within the caller's project/organization scope. A legacy tag-only key can purge only rows with no organization/project. It does not revoke API keys. Admin or a scoped key for that tag; otherwise 401/404 (uniform). Idempotent — returns `{memories, facts, documents, keys}` counts (keys is always `0`; revocation is explicit).

## Categorization

`POST /v4/memories/{memory_id}/categorize` (also available at the `/v1` alias)
with `{"category":"preference"}` updates the category metadata, appends
history, emits `memory_categorize`, and returns a reindex `job_id`.

## `GET /v4/jobs/{id}` → 200 | 404

`{id, kind, status, attempts, result, error}`. Requires valid credentials;
out-of-scope jobs read as 404 (tag/project resolved from the payload or source
record). `POST /v4/jobs/{id}/cancel` cancels queued jobs; project owners/admins
may cancel, ingest cancellation also marks the document cancelled, and a queued
project purge cancellation clears the project's deletion flag.

## `GET /v4/audit?containerTag=&org_id=&limit=&offset=` → 200 | 401 | 403

Administrator-only, paginated local accountability events. Events contain
fingerprints and resource identifiers, never raw keys, content, request bodies,
or IP addresses.

## `GET /v4/usage?containerTag=&org_id=&limit=&offset=` → 200 | 401 | 403

Administrator-only cumulative counters for each local key fingerprint and
scope: requests, searches, document writes, fact writes, and input characters.

## `POST /v4/share-links` → 201

Body `{document_id: string, expires_in?: 60..31536000}`. Creates a read-only,
single-document link and returns `{id, token, url, expiresAt}` once. Only an
actor who can read the document may create or revoke its links; tokens are
stored hashed and can be revoked with `DELETE /v4/share-links/{id}`.

## `GET /v1/share/{token}` → 200 | 404

Public, unauthenticated read-only access to the shared document. Expired,
revoked, unknown, and deleted-document tokens all return the same 404 shape.
The response contains document content and timestamps, never API keys or
metadata.

## Mem0-compatible routes

For client migrations, the server accepts `Authorization: Token <key>` and maps
these routes:

- `POST /v3/memories/add/` (also `/v1/memories/`) → `{event_id, status}`;
  poll `GET /v1/event/{event_id}/`.
- `POST /v3/memories/search/` (also `/v1/memories/search/`) with entity filters
  → `{results: [{id, memory, score, metadata}]}`.
- `GET /v1/memories/` with `user_id`, `agent_id`, `app_id`, or `run_id` → the
  scoped memory list; `POST /v3/memories/` supports bounded pagination and
  `show_expired`.
- `GET/PUT/DELETE /v1/memories/{memory_id}/` → canonical get, partial update,
  and redacted delete; `GET /v1/memories/{memory_id}/history/` returns history.
- `DELETE /v1/memories/` with explicit entity filters → asynchronous
  `event_id`.
- `PUT /v1/batch/` and `DELETE /v1/batch/` → atomic official-shaped batches of
  at most 1000 unique memories.

Entity IDs become isolated `mem0:<entity>:<value>` container tags. The exact
versioned request/response rules, capability matrix, and regression fixtures are
specified in [`MEM0_COMPATIBILITY.md`](MEM0_COMPATIBILITY.md). Hosted billing,
quotas, invitations, and a remote organization control plane remain
intentionally out of scope.

## Project and webhook routes

`GET /v1/ping/` returns the seeded `org_id` and `project_id`. Local project and
member management is available under
`/api/v1/orgs/organizations/{org_id}/projects/`. Project deletion is an
asynchronous `202` job; the project is marked deleting immediately, writes are
rejected while the job runs, and the default `local-project` cannot be deleted.
When auth is disabled, local management routes are intentionally open like the
rest of the self-hosted instance.

Webhook management is opt-in and project-scoped. `PUT` accepts `name`, `url`,
`event_types`, and `is_active`; inactive endpoints receive no new deliveries.
Create/rotate return the signing secret once, while list/get responses redact
it. Secrets are encrypted at rest in SQLite. Delivery uses HMAC-SHA256
signatures, bounded response reads, at-least-once retry, and local delivery
history. See [`runbooks/webhooks.md`](runbooks/webhooks.md).

- `POST/GET /api/v1/webhooks/projects/{project_id}/`
- `GET/PUT/DELETE /api/v1/webhooks/{webhook_id}/`
- `POST /v4/webhooks/{webhook_id}/rotate-secret`
- `GET /v4/projects/{project_id}/webhooks/deliveries`
- `POST /v4/webhooks/deliveries/{delivery_id}/replay`

## `GET /health` → 200

`{ok: true}`. Unauthenticated by design (load-balancer checks).
