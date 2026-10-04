# Quickstart: Validating Production Readiness Hardening

**Feature**: `001-production-ready` | **Date**: 2026-10-03

A runnable guide for confirming this feature works end-to-end. Each scenario states
how to run it and what outcome proves it. This is a **validation guide** —
implementation detail lives in [`plan.md`](plan.md) and `tasks.md`.

Related: [`contracts/http-api.md`](contracts/http-api.md),
[`contracts/env-vars.md`](contracts/env-vars.md),
[`data-model.md`](data-model.md).

---

## Prerequisites

```sh
cd /home/joey/projects/memoratum
uv sync --group dev
```

All server commands below run with an isolated data directory so they cannot touch
a real deployment:

```sh
export MEMORATUM_DATA_DIR=$(mktemp -d)
export MEMORATUM_API_KEY=admin-key
```

---

## Scenario 1 — Worker killed mid-job leaves recoverable work (WS-A)

**Proves**: FR-002, SC-007. The P0 outage is closed.

Enqueue an ingest job, let a worker claim it, then kill the worker uncatchably
before it completes. Verify the job is recovered rather than stranded.

```sh
# Terminal 1 — start the worker
MEMORATUM_DATA_DIR=$MEMORATUM_DATA_DIR uv run python -m memoratum.worker &

# Terminal 2 — submit work, then SIGKILL the worker mid-flight
curl -s -X POST localhost:6767/v3/documents \
  -H 'Authorization: Token admin-key' -H 'Content-Type: application/json' \
  -d '{"content":"kill me mid job","containerTag":"probe:kill"}'
kill -9 %1        # uncatchable: no cleanup, no lease release
```

Then confirm recovery. With the default 300 s lease, shorten it for the test:

```sh
MEMORATUM_JOB_LEASE_SECONDS=30 uv run python -m memoratum.worker &
sleep 35
curl -s localhost:6767/v4/jobs/<job_id> -H 'Authorization: Token admin-key'
```

**Expected**: the job reaches `done` after the lease expires and a reaper pass
recovers it.

**Fails if**: the job remains `running` indefinitely, or `claim()` never returns it.

**Regression test**: `tests/test_failure_injection.py` — kill-worker-midjob.

---

## Scenario 2 — A wedged project can be unblocked (WS-A)

**Proves**: FR-002, SC-001. The specific outage from the review is gone.

Simulate the wedge: a `purge_project` job in `running` with an expired lease, and
the project still flagged `deleting`.

```sh
# Strand a purge job, then let its lease expire
curl -s -X DELETE localhost:6767/api/v1/orgs/organizations/local-org/projects/<pid>/ \
  -H 'Authorization: Token admin-key'
# …kill the worker before the purge runs, then wait out the lease…

# The recovery path an operator actually has:
curl -s -X POST localhost:6767/v4/jobs/<job_id>/cancel \
  -H 'Authorization: Token admin-key'
```

**Expected**: `200` with `{"status": "cancelled"}`, and the project's `deleting`
flag resets to `0` so writes succeed again.

**Fails if**: `409 only queued jobs can be cancelled`, or the project keeps
returning `409 project deletion is in progress`.

**Also verify**: cancelling a job whose lease is **still live** returns
`409 job is actively progressing`. Live work must never be interrupted.

**Operator fallback** (when a lease is somehow still in the future):

```sh
uv run python scripts/recover_stuck_jobs.py --dry-run
uv run python scripts/recover_stuck_jobs.py --force
```

---

## Scenario 3 — DNS rebinding cannot reach an internal address (WS-B)

**Proves**: FR-001. The P0 SSRF window is closed.

This is the security assertion that matters most, so it is asserted structurally:
**`getaddrinfo` must be called exactly once per delivery, and zero times during
connect.**

```sh
uv run pytest tests/test_failure_injection.py -k rebinding -q
```

The test installs a resolver returning a public IP on the first lookup and a
link-local address on any later lookup, then asserts the connecting socket targets
the **first** address.

**Expected**: delivery succeeds against the validated address; the second lookup
never happens.

**Fails if**: any DNS resolution occurs between validation and connect.

---

## Scenario 4 — CGNAT addresses are rejected (WS-B)

**Proves**: FR-001 defect 1b. Independent of rebinding.

```sh
uv run pytest tests/test_webhooks_security.py -k "cgnat or 100_64" -q
```

**Expected**: `100.64.0.0/10` is rejected as an unsafe target.

**Fails if**: a webhook can be configured against `100.64.x.x`, which is
Tailscale and cloud/container internal address space. Confirmed reachable in the
current code before this fix.

---

## Scenario 5 — One logical write, one commit (WS-A)

**Proves**: FR-003, SC-001. No partial mutations.

```sh
uv run pytest tests/test_failure_injection.py -k commits -q
```

The test injects a failure at each commit point inside a single logical add and
re-opens the database afterwards.

**Expected**: at every injection point the store contains either the complete
operation (including its enqueued job) or none of it. Commit count per logical
request drops from the measured 5 to 1.

---

## Scenario 6 — Concurrent requests are no longer serialised (WS-A)

**Proves**: FR-003, SC-007.

```sh
uv run pytest tests/test_concurrency.py -q --benchmark
```

**Expected**: 4 concurrent requests complete in approximately one round-trip.
Before this change the same measurement was 1.48 s wall with per-request durations
of 0.57/0.88/1.18/1.48 s — fully serialised.

The measured result is committed to the repository, per Constitution Principle IV.

---

## Scenario 7 — Health, metrics, and logs (WS-B)

**Proves**: FR-005, US3.

```sh
curl -s localhost:6767/health/live
curl -s localhost:6767/health/ready
curl -s localhost:6767/metrics
```

**Expected**:

```json
{"ok": true}
{"ok": true, "checks": {"database": "ok", "migrations": "ok"}}
```

```text
# HELP memoratum_http_requests_total Total HTTP requests handled.
# TYPE memoratum_http_requests_total counter
memoratum_http_requests_total{method="POST",route="/v4/search",status="200"} 42
# TYPE memoratum_jobs_queue_depth gauge
memoratum_jobs_queue_depth{status="queued"} 0
```

**Also verify**:
- `Content-Type` is exactly `text/plain; version=0.0.4; charset=utf-8`.
- Route labels are path **templates**, never concrete ids.
- No `container_tag`, `org_id`, or `project_id` appears in any label.
- Scraping `/metrics` does not increment its own counters.
- Log lines are JSON on stdout and contain no memory text, secrets, or API keys.

**Fails if**: `/health/ready` returns `200` while the database is unwritable, or
the exposition body has malformed sample lines.

---

## Scenario 8 — Retention policy covers every table (WS-B)

**Proves**: FR-006, SC-001.

```sh
uv run pytest tests/test_retention.py -q
```

**Expected**: every table in `sqlite_master` has a row in `retention_policies`
with a non-empty rationale. A test fails if a future migration adds a table
without a policy.

```sh
curl -s -X POST 'localhost:6767/v4/maintenance/prune?dry_run=true' \
  -H 'Authorization: Token admin-key'
```

**Expected**: a per-table count of what *would* be deleted, with nothing deleted.
Only terminal rows (`done`/`failed`/`cancelled` jobs, `succeeded`/`dead`
deliveries) are eligible.

**Fails if**: the sweep would delete a `queued` delivery, or any table is
unclassified.

---

## Scenario 9 — Unknown fields fail closed (WS-B)

**Proves**: FR-013, Constitution Principle II.

```sh
# Typo in the scope field
curl -s -X POST localhost:6767/v4/facts -H 'Authorization: Token admin-key' \
  -H 'Content-Type: application/json' \
  -d '{"subject":"a","predicate":"b","object":"c","projct_id":"p1"}'
```

**Expected**: `422` with `extra_forbidden`. Before this fix it returned `201` and
wrote to `project_id = None` — global scope.

```sh
# Explicit scope on the Mem0 route now works
curl -s -X POST localhost:6767/v1/memories/ -H 'Authorization: Token admin-key' \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"scoped"}],"user_id":"u",
       "infer":false,"project_id":"p1"}'
```

**Expected**: the memory is stored in `p1`. Previously the field was not modelled
and the data silently landed in global NULL scope.

---

## Scenario 10 — Interrupted migration is recoverable (WS-A)

**Proves**: FR-007, SC-004.

```sh
uv run pytest tests/test_migrations.py -q
```

The test applies a migration whose second statement fails, then retries.

**Expected**: the failed migration rolls back whole, the schema stays at the
previous version, and the retry succeeds. Before this fix the first statement
persisted and the retry hard-failed with `duplicate column name`, permanently
blocking startup.

**Upgrade path**: restore a copy of a v0.9.0 database, start the new server, and
confirm all records survive (row counts plus `PRAGMA integrity_check`).

---

## Scenario 11 — Docs execute verbatim (WS-B)

**Proves**: FR-008, SC-003.

Follow `README.md` quickstart, `docs/OPERATIONS.md` backup/restore, and
`docs/SECURITY.md` webhook setup **exactly as written**, in a clean checkout.

**Expected**: zero undocumented steps. Two corrections are part of this feature:

- `docs/SECURITY.md` must no longer claim webhook endpoints are safe merely
  because they are "validated at creation and delivery" — describe address pinning.
- `docs/OPERATIONS.md` must state that the pre-change server serialised *all*
  traffic, not just writes.

**Fails if**: any documented procedure needs a step not in the docs.

---

## Scenario 12 — Full gate set from a clean checkout (WS-B)

**Proves**: SC-002, SC-006, FR-009.

```sh
uv run python scripts/check_no_telemetry.py
MEM0_TELEMETRY=false uv run pytest \
  tests/test_mem0_official_sdk.py tests/test_mem0_official_lifecycle.py \
  tests/test_mem0_webhooks.py -q
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
npm test --prefix clients/ts
node --check clients/opencode/memoratum.js
npm ci --prefix dashboard && npm run build --prefix dashboard
pip-audit --local
npm audit --audit-level=high --prefix dashboard
npm audit --audit-level=high --prefix clients/ts
```

**Expected**: all pass. `uv run pytest -q` additionally enforces the coverage floor
via `addopts`, with no CI change needed.

Baseline to hold: **75.5% branch-aware** (measured on `main` at `2dddbb4`), gate
set to `floor(75.5) - 1 = 74`.

---

## Scenario 13 — Packaging and installability (WS-B)

**Proves**: FR-011, SC-005, SC-006.

```sh
docker build -t memoratum:test . && docker run --rm -p 6767:6767 memoratum:test
```

**Expected**: the container starts and serves traffic with SQLite only, no
provider configured.

```sh
uv pip install .            # base package, minimal deps only
uv pip install '.[qdrant]'  # provider behind an extra
docker compose up --build   # both services, webhook key supplied on both
```

**Expected**: correct licence metadata (AGPL server, MIT clients), and no
undocumented required environment variable. See
[`contracts/env-vars.md`](contracts/env-vars.md) for the 20 variables that must
appear in `.env.example`.

---

## Final gate

The feature is done when all thirteen scenarios pass and `tasks.md` is complete.
Report the measured numbers — coverage, search latency at 500/1,000/2,000/4,000,
concurrency wall time, and migration retry — rather than asserting success.