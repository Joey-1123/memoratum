# Phase 1 Data Model: Production Readiness Hardening

**Feature**: `001-production-ready` | **Date**: 2026-10-03
**Storage**: SQLite (WAL) remains the default and only required store, per
Constitution Principle I. Every entity below is additive; no entity is removed.

Related: [`research.md`](research.md) (decision rationale),
[`contracts/env-vars.md`](contracts/env-vars.md) (new configuration surface).

---

## 1. `jobs` — lease fields (existing entity, extended)

The confirmed P0 lives here: a claimed job carries no timeout, so an uncatchable
worker kill strands it forever.

| Field | Type | Null | Rule |
|---|---|---|---|
| `lease_expires_at` | REAL | no | `NOT NULL DEFAULT 0`. Wall-clock epoch seconds after which the claim is considered dead. `0` means "no lease held". Set by `claim()`, advanced by `heartbeat()`. |
| `heartbeat_at` | REAL | yes | Epoch seconds of the last heartbeat. Null until first heartbeat. Diagnostic only; never used for the liveness decision. |

Existing fields retained: `id`, `kind`, `payload`, `status`, `attempts`,
`result`, `error`, `worker`, `created_at`, `updated_at`, `run_after`.

**Index.** `CREATE INDEX idx_jobs_lease ON jobs(status, lease_expires_at)`. The
reaper's predicate is `status = 'running' AND lease_expires_at < ?`, which this
index serves directly.

### Validation rules

- `lease_expires_at` MUST be strictly greater than `time.time()` at the moment of
  a successful `claim()`.
- A heartbeat MUST NOT shorten a lease. It MAY only extend it, and only for a job
  this worker currently holds.
- `lease_expires_at` MUST NOT be set for a job in `queued`, `done`, `failed`, or
  `cancelled` status. A lease exists only for `running`.
- When a job leaves `running` (to `done`, `failed`, `cancelled`, or back to
  `queued` by the reaper), `lease_expires_at` MUST be reset to `0` and
  `heartbeat_at` set to NULL, so a reaped job cannot inherit a stale deadline.

### State transitions

```
                 claim()                    reaper (lease expired)
   queued ──────────────────> running ──────────────────────────────> queued
     │                          │  ▲
     │                          │  └── heartbeat() extends lease_expires_at
     │                          │
     │   complete()             │  fail()                  cancel()
     ├──────────────────────────┼──────────────────────────┐
     v                          v                          v
   done                     failed                     cancelled
```

| From | Event | To | Guard |
|---|---|---|---|
| `queued` | `claim()` | `running` | `run_after <= now`; inside `BEGIN IMMEDIATE`; sets lease, `attempts += 1` |
| `running` | `reap_stale()` | `queued` | `lease_expires_at < now`; clears lease; does **not** decrement `attempts` |
| `running` | `heartbeat()` | `running` | lease extended forward only; worker still holds the claim |
| `running` | `complete()` | `done` | clears lease |
| `running` | `fail()` | `failed` | `attempts >= MAX_ATTEMPTS` (3); clears lease |
| `running` | requeue on transient error | `queued` | `attempts < MAX_ATTEMPTS`; clears lease |
| `running` | `cancel()` | `cancelled` | **only if** `lease_expires_at < now` — live work is never interrupted |
| `queued` | `cancel()` | `cancelled` | unchanged behaviour |
| `queued` | `requeue()` | `queued` | explicit operator/内部 requeue |

**Invariant that must not regress.** A `running` job whose lease is still in the
future MUST NOT be reclaimed by any other worker. This is the invariant
`tests/test_security_regressions.py:171` protects; its fixture must be updated to
set a future `lease_expires_at`, because a raw `status='running'` with the default
`lease_expires_at = 0` now correctly denotes a *dead* claim.

---

## 2. `retention_policies` — new table (policy registry, not data)

Constitution FR-006 requires every table to carry a documented policy. Storing the
policy as data makes it auditable and testable: a test can assert that the set of
tables in the schema is a subset of the set with a policy, so a future migration
that adds a table without a policy fails the build.

| Field | Type | Null | Rule |
|---|---|---|---|
| `table_name` | TEXT | no | PRIMARY KEY. Must match a table in `sqlite_master`. |
| `policy_class` | TEXT | no | CHECK in `('retain_indefinitely','cascaded','timed','size_bounded')` |
| `retention_days` | INTEGER | yes | Required when `policy_class='timed'`; NULL otherwise. CHECK `> 0`. |
| `rationale` | TEXT | no | Why this class. Mandatory — an undocumented policy is a failed policy. |

This table is configuration, not tenant data, so it is `retain_indefinitely` by
the same rule it describes.

### Policy assignment for all 19 existing tables

Derived from the measured `DELETE`-statement audit: `memory_history` and
`share_links` have **zero** delete paths today, and `jobs`, `domain_events`, and
`webhook_deliveries` are only removed by an explicit project purge.

| Table | Class | Days | Rationale |
|---|---|---|---|
| `organizations` | retain_indefinitely | — | Top-level tenant; removed only by operator action |
| `projects` | retain_indefinitely | — | Same; `deleting` flag gates purge |
| `project_members` | cascaded | — | `ON DELETE CASCADE` from `projects` |
| `api_keys` | retain_indefinitely | — | Security-relevant; must outlive the key's use |
| `revoked_keys` | timed | 90 | Must outlive any plausible reuse attempt, then age out |
| `webhooks` | retain_indefinitely | — | Operator-configured, low volume |
| `documents` | retain_indefinitely | — | User content; expiry is a user-level concept (`expires_at`) |
| `chunks` | cascaded | — | `ON DELETE CASCADE` from `documents` |
| `memories` | retain_indefinitely | — | User content; `expires_at` governs visibility |
| `memory_history` | timed | 365 | Audit value for one year, then unbounded growth is a liability. **Currently never pruned.** |
| `facts` | retain_indefinitely | — | User content |
| `vector_points` | cascaded | — | Derived; rebuilt from `memories`/`chunks` |
| `jobs` | timed | 30 | Terminal jobs (`done`/`failed`/`cancelled`) only. **Currently never pruned.** |
| `domain_events` | timed | 90 | Terminal after all deliveries resolve. **Currently never pruned.** |
| `webhook_deliveries` | timed | 90 | Only `succeeded`/`dead`; must not be deleted while `queued`. **Currently never pruned.** |
| `idempotency_keys` | timed | 24 | Bounded by `expires_at`; the sweep enforces what the schema only documents |
| `share_links` | timed | 30 | By `expires_at`. **Currently never pruned.** |
| `audit_events` | timed | 365 | `docs/AUDIT_METERING.md` accountability window |
| `usage_counters` | retain_indefinitely | — | Per-key usage is an accounting record, not a log |

**Ordering constraint for the sweep.** Delete from `webhook_deliveries` before
`domain_events`: the former has `event_id REFERENCES domain_events(id)`, so
removing the parent first would either fail or orphan. Likewise `chunks` and
`memory_history` before their parents is unnecessary because they cascade, but the
sweep must never delete a parent that still has live children.

---

## 3. Metrics registry (in-memory, not persisted)

Prometheus text exposition, served by FastAPI. Counters and gauges plus bounded
histograms. Guards:

- All mutation under a `threading.Lock` — sync endpoints run in a threadpool and
  async ones on the event loop, so an unguarded dict increment is a data race.
- **Route labels use the registered path template, never the concrete path.**
  A raw path embeds memory and document ids, which both explodes cardinality and
  leaks identifiers into a metrics store. Unmatched routes use `__unmatched__`.
- **Tenant identifiers are never labels.** `container_tag`, `org_id`, and
  `project_id` are unbounded by construction and would multiply by bucket count.
  Per-tenant accounting already exists at `/v4/usage`; business metrics stay out
  of the metrics endpoint.
- `/metrics` is exempt from its own counters, matching the existing `/health`
  exemption in the rate-limit middleware, so a scrape cannot inflate the series it
  reports.
- No content, secrets, or API keys appear in any label value.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `memoratum_http_requests_total` | counter | `method`, `status`, `route` | Requests handled |
| `memoratum_http_request_duration_seconds` | histogram | `method`, `route` | Latency distribution |
| `memoratum_jobs_queue_depth` | gauge | `status` | Jobs per status |
| `memoratum_jobs_oldest_queued_age_seconds` | gauge | — | Backlog age |
| `memoratum_jobs_reaped_total` | counter | `kind` | Leases recovered |
| `memoratum_webhook_deliveries_total` | counter | `status` | Delivery outcomes |
| `memoratum_build_info` | gauge | `version` | Build identity |

`memoratum_jobs_queue_depth` and `..._oldest_queued_age_seconds` are computed as
one indexed aggregate per scrape (`GROUP BY status` plus `MIN(created_at)` against
`idx_jobs_status`), so no cache is needed and no scan is introduced.

---

## 4. Health model

The existing `GET /health` returns `{"ok": true}` unconditionally, and the
Dockerfile `HEALTHCHECK` probes exactly that endpoint — so a wedged database or a
dead worker still reports healthy. Split into two signals:

| Endpoint | Semantics | Fails when |
|---|---|---|
| `GET /health/live` | Process is running | Never, short of a hung event loop. Used by the container `HEALTHCHECK`. |
| `GET /health/ready` | Process can serve traffic | Database unreachable or not writable; migrations incomplete |

`GET /health` is retained as an alias for `/health/live` so the existing
Dockerfile `HEALTHCHECK` and any operator tooling keep working — a breaking change
here would break every existing deployment's health monitoring.

Response shape for `/health/ready`:

```json
{"ok": false, "checks": {"database": "unavailable", "migrations": "pending"}}
```

`503` when not ready, `200` when ready. The body carries check names and coarse
status only — never paths, counts of tenant data, or error strings that could
leak internals to an unauthenticated probe.

---

## 5. Request-model strictness (no new entity)

Introduces a `StrictModel` base carrying `model_config = ConfigDict(extra="forbid")`,
inherited by all ten request models. `Mem0AddIn` and `Mem0SearchIn` gain explicit
`org_id` and `project_id` fields.

Validation rule: an unknown field MUST return `422` rather than being silently
dropped. `MEMORATUM_LENIENT_COMPAT=true` relaxes this to the current
ignore-extras behaviour for operators whose Mem0-compatible clients send
additional fields; the relaxation is logged at startup so its presence is never
silent.

---

## 6. Migration safety (mechanism, not entity)

Replaces `db.executescript()` with an explicit transaction per migration plus a
post-apply verification step. Recorded here because it changes the meaning of
`schema_migrations`, not because it adds a table.

Rules:
- Each migration applies inside a single transaction. On failure it rolls back
  whole, leaving the schema at the previous version.
- `schema_migrations.version` is written **in the same transaction** as the DDL,
  so version and schema cannot disagree.
- After applying, `PRAGMA integrity_check` and `PRAGMA foreign_key_check` must
  both pass, or startup fails loudly.
- Migrations containing `PRAGMA foreign_keys=OFF` cannot be wrapped naively,
  because that pragma is a no-op inside a transaction. Those require an explicit
  disable/enable-around-transaction form and must be identified in `tasks.md`
  before any migration work begins.
- Migration 28 (the job lease columns) is the first migration shipped under the
  new regime and therefore its own acceptance test.

---

## Entity summary

| Entity | Change | Driver |
|---|---|---|
| `jobs` | +2 columns, +1 index | FR-002, P0 stranded-job wedge |
| `retention_policies` | new table | FR-006 |
| Metrics registry | new, in-memory | FR-005 |
| Health checks | split live/ready | FR-005 |
| Request models | `extra="forbid"` | FR-013 (promoted from review) |
| Migration runner | transaction + verify | FR-007 |
| Webhook connection | pinned IP | FR-001, P0 SSRF |
| `db.transaction()` | new context manager | FR-003 |
| `chunks.embedding` | populated already; now **read** | Performance ceiling |