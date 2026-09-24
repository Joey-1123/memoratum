# Mem0 compatibility profile

**Contract:** `mem0-self-hosted-v0.1`
**Official SDK target:** `mem0ai` Python client and `mem0ai` TypeScript client
**Status:** partial compatibility; self-hosted HTTP profile only

Memoratum exposes a small, explicit compatibility layer for migrating clients
that need the familiar memory request/response shape. This profile is a
**versioned contract for the routes implemented in this repository**, not a
claim that Memoratum implements every hosted Mem0 API, SDK behavior, or service
feature.

## SDK contract status

The official Python client (`mem0ai==2.2.0`) sends
`Authorization: Token <key>` and the add and search routes above; its `add()`
payload is the fixture shape used here. CI exercises that client against a local
contract server with `MEM0_TELEMETRY=false`. Its `get_all()` uses a POST
collection route, which this profile does not yet implement. The official
TypeScript package is named `mem0ai` (not `@mem0ai/mem0`); the repository runs
a dependency-free probe against the same route and payload contract. The full
upstream package is intentionally not a client dependency because it ships its
own telemetry code and would violate this repository's zero-telemetry boundary;
consumers may still point it at a local server with `MEM0_TELEMETRY=false`.
This repository does not vendor or claim the upstream SDKs.


| Operation | Status | Routes |
|---|---|---|
| Add memories | Supported | `POST /v3/memories/add/`, `POST /v1/memories/` |
| Poll an add event | Supported | `GET /v1/event/{event_id}/` |
| Search memories | Supported | `POST /v3/memories/search/`, `POST /v1/memories/search/` |
| List current memories | Supported | `GET /v1/memories/` |

Authentication accepts `Authorization: Token <key>` and maps it to the same
scoped-key authorization used by the native API. Every add, search, and list
request must identify exactly one supported entity scope: `user_id`,
`agent_id`, `app_id`, or `run_id`. The selected ID is encoded as an isolated
`mem0:<entity>:<value>` container tag.

## Request and response contract

The canonical machine-readable request and response examples live in
`tests/fixtures/mem0/v0.1/` and are exercised by
`tests/test_mem0_contract.py`. The version directory and manifest are updated
with every breaking contract change. The fixture set includes add, event,
search, list, and error envelopes.

### Add

`POST /v3/memories/add/` accepts:

```json
{
  "messages": [{"role": "user", "content": "The user prefers local search."}],
  "user_id": "user-123",
  "metadata": {"source": "example"},
  "infer": true
}
```

The response is asynchronous:

```json
{"event_id": "<job-id>", "status": "PENDING"}
```

The event is completed by the background worker. Poll it with
`GET /v1/event/{event_id}/`; the status is one of `PENDING`, `SUCCEEDED`, or
`FAILED`.

### Search

`POST /v3/memories/search/` accepts a query, entity filters, and bounded
ranking controls:

```json
{
  "query": "local search",
  "filters": {"user_id": "user-123"},
  "top_k": 5,
  "threshold": 0.0,
  "rerank": false
}
```

The response is:

```json
{"results": [{"id": "<id>", "memory": "...", "score": 0.01, "metadata": {}}]}
```

Search uses Memoratum's hybrid retrieval over the selected entity scope. The
`score` is a ranking score, not a promise of cosine similarity.

### List

`GET /v1/memories/?user_id=user-123&limit=10` returns current facts in the
selected entity scope. The response uses the same `results` array shape as
search, with `created_at` included for each item.

## Capability boundaries

The following capabilities are deliberately **planned** or **out of scope** in
this first profile:

- `update_memory` — planned; native fact management is available through the
  v4 API while compatibility update semantics are being specified.
- `delete_memory` compatibility route — planned; the native v4 delete route is
  available.
- History, bulk operations, organizations, webhooks, and managed billing —
  not part of this self-hosted profile.

See the machine-readable capability matrix in
`src/memoratum/mem0_contract.py` and the versioned fixtures for regression
coverage. The matrix is intentionally explicit so a client can fail fast or
fall back to native routes rather than infer unsupported behavior.

## Contract rules

1. `messages` must be non-empty; each message has a supported `role` and
   non-empty `content`.
2. Add requires an entity scope. An explicit `containerTag` is an escape hatch
   for clients that already manage local tags; it must use the `mem0:` namespace
   and cannot be combined with an entity id.
3. Search requires entity filters; list requires an entity query parameter.
4. `top_k` and list `limit` are bounded to 1–100; search `threshold` is in the
   range 0–1.
5. Errors use the stable envelope `{"error":{"code":"...","message":"..."}}`.
6. Unknown, out-of-scope, or malformed resource IDs must not reveal another
   scope's data.
7. The profile remains self-hosted and adds no telemetry. Local audit and usage
   accounting are available through the native governance endpoints.

## Versioning

A breaking change to request validation, route mapping, response shape, or
entity isolation must increment the contract version and update the fixtures,
capability matrix, tests, and this document together. Additive optional fields
may remain within the same profile only when existing clients remain valid.
