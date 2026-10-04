# Contract: HTTP Surface Changes

**Feature**: `001-production-ready` | **Date**: 2026-10-03

Scope: externally visible HTTP behaviour added or changed by this feature. Internal
worker and lease mechanics are specified in [`../data-model.md`](../data-model.md),
not here.

Backward-compatibility rule for this feature: **no existing endpoint changes its
success response shape**. Additions are new endpoints or new optional parameters.
The two behaviour changes that do affect existing callers are called out
explicitly under [Changed behaviour](#changed-behaviour).

---

## New: `GET /health/live`

Liveness. Answers "is the process running".

**Auth**: none (matches existing `/health`).
**Success**: `200`

```json
{"ok": true}
```

Never fails under normal operation. Used by the container `HEALTHCHECK`, because a
liveness probe that fails on a degraded database causes orchestrators to kill a
process that could still serve traffic.

---

## New: `GET /health/ready`

Readiness. Answers "can this process serve traffic right now".

**Auth**: none.
**Success**: `200` when ready. **Failure**: `503` when not ready.

```json
{"ok": true,  "checks": {"database": "ok", "migrations": "ok"}}
{"ok": false, "checks": {"database": "unavailable", "migrations": "ok"}}
```

| Check | `ok` when | Not-ok value |
|---|---|---|
| `database` | A trivial write and read succeeds | `unavailable`, `readonly` |
| `migrations` | `schema_migrations` version equals the code's expected version | `pending`, `diverged` |

`diverged` means the database records a **higher** version than the running code
expects — the operator has downgraded. This is surfaced distinctly because it is
not fixable by waiting.

The body carries check names and coarse status only. No paths, row counts, tenant
names, or raw exception strings, because this endpoint is unauthenticated.

---

## New: `GET /metrics`

Prometheus text exposition.

**Auth**: none; bound to loopback only, consistent with `/health`.
**Content-Type**: `text/plain; version=0.0.4; charset=utf-8` — set explicitly,
because Starlette's `PlainTextResponse` omits the `version` parameter.

```text
# HELP memoratum_http_requests_total Total HTTP requests handled.
# TYPE memoratum_http_requests_total counter
memoratum_http_requests_total{method="POST",route="/v4/search",status="200"} 42
# HELP memoratum_jobs_queue_depth Jobs currently in each status.
# TYPE memoratum_jobs_queue_depth gauge
memoratum_jobs_queue_depth{status="queued"} 3
```

Escaping rules — the asymmetry between the two contexts is the common bug:

| Context | Escape |
|---|---|
| Label **values** | `\` → `\\`, `"` → `\"`, newline → `\n` |
| `# HELP` **text** | `\` → `\\`, newline → `\n` — do **not** escape `"` |
| Metric **names** | Not escapable. Validate against `[a-zA-Z_:][a-zA-Z0-9_:]*` |
| Label **names** | Validate against `[a-zA-Z_][a-zA-Z0-9_]*` — colons are legal in metric names but **not** in label names |

`# TYPE` is emitted even though it is optional in format 0.0.4: omitting it leaves
the series untyped, and a scraper cannot rate an untyped series.

Histogram buckets are declared as explicit `(le_string, le_float)` pairs rather
than computed floats, because `str(1e-05)` renders as `1e-05` and the emitted
text must be stable across runs.

Full metric list: [`../data-model.md` §3](../data-model.md).

---

## Changed: `POST /v4/jobs/{job_id}/cancel`

The only P0-relevant behaviour change to an existing endpoint.

### Previous behaviour

```http
POST /v4/jobs/{job_id}/cancel
→ 409 {"error": "only queued jobs can be cancelled"}
```

for any job in `running`, **regardless of whether its worker still existed**. This
is why a wedged `purge_project` could never be cancelled.

### New behaviour

Cancellation succeeds when the job is `queued`, **or** when the job is `running`
and its lease has expired (`lease_expires_at < now`), meaning no worker is
actively progressing it.

A `running` job with a live lease is still refused — live work is never
interrupted:

```http
→ 409 {"error": "job is actively progressing"}
```

### Side effect on `purge_project` cancellation

Unchanged and load-bearing: cancelling a `purge_project` job resets
`projects.deleting` to `0`, which is what makes the project writable and
deletable again. With stale-running jobs now cancellable, this is the operator's
route out of the wedge.

### Status codes

| Code | Condition |
|---|---|
| `200` | Cancelled |
| `401` | Auth required and absent |
| `403` | Caller lacks project `OWNER` |
| `404` | Job absent **or** outside the caller's scope (uniform, per Principle II) |
| `409` | Job is `running` with a live lease, or already terminal |

---

## New: `POST /v4/maintenance/prune`

Manual retention sweep.

**Auth**: admin only.
**Request body**: none. Optional query `dry_run=true` reports what would be deleted
without deleting.

```http
POST /v4/maintenance/prune?dry_run=true
→ 200 {"dry_run": true, "would_delete": {"jobs": 118, "domain_events": 402,
                                         "webhook_deliveries": 402}}
→ 200 {"dry_run": false, "deleted": {"jobs": 118, "domain_events": 402,
                                     "webhook_deliveries": 402}}
```

Only **terminal** rows are eligible: `jobs` in `done`/`failed`/`cancelled`,
deliveries in `succeeded`/`dead`. A queued delivery is never deleted by the sweep,
because deleting it would silently drop an obligation.

`webhook_deliveries` is pruned before `domain_events` to respect the foreign key.

---

## New: `POST /v4/maintenance/reap-jobs`

Force the reaper once, synchronously, and report what it recovered.

**Auth**: admin only.

```http
→ 200 {"reaped": [{"job_id": "…", "kind": "purge_project", "project_id": "…"}],
       "wedged_projects": ["…"]}
```

`wedged_projects` lists projects still holding `deleting = 1` with no live purge
job. This exists because the API alone cannot always clear a wedged project: if
the stranded job's lease is somehow still in the future, cancel legitimately
refuses. The operator script (`scripts/recover_stuck_jobs.py`) handles that case
with `--force`.

---

## Changed: all write endpoints — unknown fields rejected

Applies to every request model via the `StrictModel` base.

### Previous behaviour

Unknown fields were silently discarded (Pydantic default). Verified consequences:

| Request | Old result | Effect |
|---|---|---|
| `POST /v4/facts` with `projct_id` (typo) | `201` | Wrote to `project_id = None` |
| `POST /v3/documents` with `projectId` | `201` | Wrote to `project_id = None` |
| `POST /v1/memories/` with `project_id` | `200` | Field not modelled at all; wrote to global NULL scope |

### New behaviour

```http
POST /v4/facts  {"subject":"a","predicate":"b","object":"c","projct_id":"…"}
→ 422 {"detail":[{"type":"extra_forbidden","loc":["body","projct_id"],
                  "msg":"Extra inputs are not permitted"}]}
```

`MEMORATUM_LENIENT_COMPAT=true` restores the old ignore-extras behaviour for
operators whose Mem0-compatible clients send additional fields. The relaxation is
logged at startup so it is never silently active.

### New: `Mem0AddIn` and `Mem0SearchIn` accept scope

`POST /v1/memories/`, `/v3/memories/add/`, `/v3/memories/search/` gain `org_id` and
`project_id`. These routes previously could not be scoped at all — an admin could
only write to global NULL scope. A caller that passes `project_id` now gets the
data stored in that project, which is what the caller asked for.

---

## Changed: error contract for destructive operations

`docs/API.md` gains an explicit recoverability annotation on every destructive
endpoint, satisfying FR-007's "recoverable or explicitly irreversible":

| Operation | Recoverability |
|---|---|
| `DELETE` memory / fact | **Irreversible** for content; `memory_history` retains prior versions for the retention window |
| `DELETE` project (purge) | **Irreversible.** Runs async via `purge_project`; recoverable only until the job starts |
| `DELETE` document + chunks | **Irreversible** for content |
| `POST /v4/jobs/{id}/cancel` | **Reversible** for `purge_project` (resets `deleting`); terminal for other kinds |
| `POST /v4/maintenance/prune` | **Irreversible**; always dry-runnable first |
| Revoke API key | **Irreversible** |