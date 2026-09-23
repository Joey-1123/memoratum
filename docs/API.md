# Memoratum HTTP API

Base URL autodetects nothing — pass it explicitly (default server: `http://localhost:6767`).
Auth: `Authorization: Bearer <key>`; admin key, container-scoped key, or an
optional `org_id` scope. Error shape everywhere:
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

Response: `{"id": "<hex>", "status": "done"|"failed"}` (ingest runs inline).

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

## `POST /v4/facts` → 201

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

`{facts: [...full rows...], total}` with optional `include_superseded=true`, `memory_type`, `org_id`, and `limit` (default 100). `containerTag` required (422 when missing). Powers diff-sync clients.

## `GET /v4/profile?containerTag=` → 200

`{containerTag, facts: ["subject predicate object", …≤20], stats: {documents, chunks, facts}}` (fact count is exact, sample capped). Pass `org_id` to scope the profile.

## `POST /v4/keys` → 201

Body `{containerTag: string|null, org_id: string|null}` (null values are wildcards). Open mode or admin → `{key: "mm_..."}` (shown once); known non-admin key → 403; otherwise 401.

## `POST /v4/keys/revoke` → 200

Admin only (403 otherwise). Body `{key: "<raw>"}` → `{revoked: true|false}`. Revoked keys 401 everywhere immediately.

## `DELETE /v4/memories/{fact_id}` → 200 | 404

Hard-deletes one fact. Missing or out-of-scope → 404; bad credentials → 401. Response `{deleted: id}`.

## `DELETE /v4/tags/{tag}` → 200

Purges the tag's documents (chunks cascade), facts, and keys. Admin or a scoped key for that tag; otherwise 401/404 (uniform). Idempotent — returns `{facts, documents, keys}` counts (zeros when empty).

## `GET /v4/jobs/{id}` → 200 | 404

`{id, kind, status, attempts, result, error}`. Requires valid credentials;
out-of-scope jobs read as 404 (tag resolved from the payload's document or tag).

## `GET /health` → 200

`{ok: true}`. Unauthenticated by design (load-balancer checks).
