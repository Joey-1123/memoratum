# Contract: Environment Variables

**Feature**: `001-production-ready` | **Date**: 2026-10-03

Scope: new configuration surface added by this feature, plus the reconciliation
required to make `.env.example` complete.

---

## New variables

All are optional. None has a required-without-default state: omitting every one of
them leaves a fully functional SQLite-only server, per Constitution Principle I.

### `MEMORATUM_JOB_LEASE_SECONDS`

| | |
|---|---|
| Type | Integer seconds |
| Default | `300` |
| Range | `30`–`86400`; values outside are clamped and logged |

Duration of a job claim lease. A `running` job whose `lease_expires_at` is in the
past is considered dead and is recovered by the reaper.

Must exceed the longest expected single job between heartbeats. The `bulk_memories`
loop heartbeats on each item, so lease length only needs to exceed the slowest
*single item*, not the whole batch.

### `MEMORATUM_RETENTION_DAYS`

| | |
|---|---|
| Type | Integer days; `0` disables the automatic sweep |
| Default | `0` (manual only) |
| Applies to | Per-table policies in [`../data-model.md` §2](../data-model.md) |

Overrides the per-table `retention_days` for every `timed` table. Defaulting to
`0` means **no automatic deletion happens unless an operator opts in** — the
conservative choice, since silently deleting audit or memory-history data would be
a worse failure than growing a file. `POST /v4/maintenance/prune` remains available
for explicit runs.

### `MEMORATUM_LOG_LEVEL`

| | |
|---|---|
| Type | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| Default | `INFO` |

Applies to the JSON-to-stdout structured logger. Never affects log *content*: scope
identifiers only, never memory text, secrets, API keys, or raw query strings.

### `MEMORATUM_LOG_FORMAT`

| | |
|---|---|
| Type | `json` \| `text` |
| Default | `json` |

`json` for container log drivers; `text` for human reading during development.

### `MEMORATUM_METRICS_ENABLED`

| | |
|---|---|
| Type | Boolean |
| Default | `true` |

`GET /metrics` is loopback-bound either way; this only controls whether the
instrumentation runs. Set `false` to remove per-request metric overhead.

### `MEMORATUM_LENIENT_COMPAT`

| | |
|---|---|
| Type | Boolean |
| Default | `false` |

When `true`, request models revert to ignoring unknown fields, restoring the
pre-existing behaviour for operators whose Mem0-compatible clients send additional
fields. **Logged loudly at startup** so a relaxed deployment is never silent.

Default `false` because silently discarding a misspelled `project_id` writes data
to global scope, which is a tenant-isolation failure that fails open.

---

## Existing variables missing from `.env.example`

Measured: 20 variables are read by `src/` but absent from `.env.example`. Two are
security-relevant. FR-011 requires "every required environment variable is
documented with safe defaults and no undocumented required variable exists".

| Variable | Default | Why it was missing |
|---|---|---|
| `MEMORATUM_WEBHOOK_ENCRYPTION_KEY` | generated file | **Security-critical.** Fernet key; comma-separated for rotation. Undocumented means operators don't know to set it and rely on the auto-generated file. |
| `MEMORATUM_WEBHOOK_ALLOW_PRIVATE_TARGETS` | `false` | **Security-critical.** The development-only switch that permits HTTP and private-range webhook targets. Must be documented as unsafe in production. |
| `MEMORATUM_LLM_PROVIDER` | unset | Determines the chat backend; unset means dreaming is skipped. |
| `MEMORATUM_VECTOR_STORE` | `sqlite` | Selects the vector backend. Undocumented, so the optional-extras story is undiscoverable. |
| `MEMORATUM_VECTOR_STORE_ENDPOINT` | unset | Qdrant/Chroma endpoint. |
| `MEMORATUM_VECTOR_STORE_PATH` | unset | Chroma local path. |
| `MEMORATUM_VECTOR_STORE_COLLECTION` | `memoratum` | Collection name. |
| `MEMORATUM_VECTOR_STORE_DIMS` | `0` (follow embeddings) | Must match the embedder or writes fail. |
| `MEMORATUM_VECTOR_STORE_KEY` | unset | Read from env in `worker.py` and `app.py`, **not** from `config.py` — an inconsistent pattern worth normalising. |
| `MEMORATUM_OIDC_ISSUER` | unset | Enterprise identity. |
| `MEMORATUM_OIDC_AUDIENCE` | unset | Enterprise identity. |
| `MEMORATUM_OIDC_JWKS_URL` | unset | Enterprise identity. |
| `MEMORATUM_ENV` | unset | Environment label. |

Client-only variables (`MEMORATUM_URL`, `MEMORATUM_RERANKER*`,
`MEMORATUM_EVAL_API`) are read outside `src/memoratum/` and belong in the client
SDK documentation, not the server `.env.example`. They must be triaged rather than
silently omitted.

`docker-compose.yml` additionally needs `MEMORATUM_WEBHOOK_ENCRYPTION_KEY` on
**both** services. Today neither sets it and both fall back to the auto-generated
file in the shared `/data` volume, which happens to work — but it means key
rotation requires a volume edit, and the operator has no documented way to supply
their own key.

---

## Behaviour changes to existing variables

None. Every existing variable keeps its current name, type, and default. This
feature is additive on the configuration surface.

The one interaction to document: `MEMORATUM_WEBHOOK_ALLOW_PRIVATE_TARGETS=true`
disables SSRF address validation entirely (including the pinning in D1, since
pinning a private address is not meaningful). It must remain a development-only
setting, and `docs/SECURITY.md` should say so more forcefully than it currently
does.