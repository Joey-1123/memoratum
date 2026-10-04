---

description: "Task list for 001-production-ready — Production Readiness Hardening"
---

# Tasks: Production Readiness Hardening

**Input**: Design documents from `specs/001-production-ready/`
(plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md)

**Prerequisites**: all present.

**Tests**: REQUIRED for this feature, not optional. FR-010 and Constitution
Principle IV both mandate it. Every bug-fix task lists its regression test
immediately before it, and the test task is marked *(fail first)*.

**Organization**: Grouped by the two workstreams from plan.md, then by user story.
**WS-A (Durability and Recovery) is sequenced first and is implemented
task-by-task with review** — it changes transaction boundaries, the migration
runner, and the job reaper, which is exactly the work an unattended
`scripts/prod-loop.sh` pass must not touch.

## Format: `[ID] [P?] [US#] Description`

- **[P]**: different files, no dependency on another task — safe to parallelise
- **[US#]**: user story from spec.md, for traceability
- Story labels: US1 security/recovery · US2 atomicity/concurrency · US3
  observability · US4 migrations · US5 docs · US6 CI · US7 packaging

---

## Phase 1: Setup (shared, WS-A + WS-B)

- [X] T001 Add `pytest-cov>=6` to the `dev` dependency group in `pyproject.toml` (dev-only; never runtime — Principle V)
- [X] T002 Add `[tool.coverage.run]` with `branch = true`, `source_pkgs = ["memoratum"]`, `omit = ["*/__main__.py"]` to `pyproject.toml`
- [X] T003 Add `[tool.coverage.report]` with `fail_under = 74`, `show_missing = true`, `skip_covered = false`, `precision = 1` to `pyproject.toml`; comment the measured baseline (75.5%) beside it
- [X] T004 Add `addopts = "--cov --cov-report=term-missing"` to `[tool.pytest.ini_options]` so existing `uv run pytest -q` in `.github/workflows/ci.yml` enforces the gate with no CI change
- [X] T005 [P] Create `tests/test_failure_injection.py` with the four harnesses: `crash_between_commits`, `kill_worker_midjob`, `rebinding_resolver`, `reopen_db_after_crash` *(blocking — see Phase 2)*

**Checkpoint**: `uv run pytest -q` reports coverage and stays green at 241 passed.

---

## Phase 2: WS-A Foundation (blocking — no WS-A fix starts without this)

**⚠️ CRITICAL**: Every task below depends on T005. A durability fix with no
failure-injection test is how the P0 shipped the first time.

- [X] T006 [P] Add a `conn.commit()` counter helper to `tests/helpers.py` using `sqlite3.Connection.set_trace_callback` (verified working; the `commit` attribute is immutable so it cannot be monkeypatched)
- [X] T007 [P] Add a `temp_data_dir` fixture to `tests/conftest.py` isolating `MEMORATUM_DATA_DIR` per test
- [X] T008 [P] Add a `CountingClock` fixture to `tests/conftest.py` for deterministic lease-expiry tests without sleeping

**Checkpoint**: `crash_between_commits` can inject a failure at commit point *k* and
re-open the database. Verified before any WS-A fix begins.

---

## Phase 3: WS-A1 — Job durability (P0 outage) [US1]

Goal: no worker death can permanently strand work; a wedged project is always recoverable.

### Tests *(write first, confirm they fail)*

- [X] T009 [P] [US1] `test_failure_injection.py::test_killed_worker_job_is_reaped` — claim a job, never complete it, expire the lease, assert the reaper returns it to `queued` *(fail first)*
- [X] T010 [P] [US1] `test_failure_injection.py::test_live_lease_is_never_stolen` — a `running` job with a future lease must not be reclaimed *(fail first)*
- [X] T011 [P] [US1] `tests/test_jobs_lease.py::test_stale_running_job_is_cancellable` — `cancel()` succeeds when the lease has expired *(fail first)*
- [X] T012 [P] [US1] `tests/test_jobs_lease.py::test_claim_is_atomic` — two concurrent claims yield one job, not two *(fail first)*
- [X] T013 [P] [US1] `tests/test_security_regressions.py` — **EDIT the fixture at line 171** to set a future `lease_expires_at` so it models a *live* claim. The invariant it protects is preserved; only the fixture changes.
- [X] T014 [P] [US1] `tests/test_worker_resilience.py::test_unexpected_error_does_not_kill_worker` *(fail first)*
- [X] T015 [P] [US1] `tests/test_recovery.py::test_wedged_project_can_be_unblocked` — stranded `purge_project` → cancel resets `deleting=0` and the project accepts writes again *(fail first)*

### Implementation

- [X] T016 [US1] Add migration 28 to `_MIGRATIONS` in `src/memoratum/db.py`: `jobs.lease_expires_at REAL NOT NULL DEFAULT 0`, `jobs.heartbeat_at REAL`, and `CREATE INDEX idx_jobs_lease ON jobs(status, lease_expires_at)`
- [X] T017 [US1] Add `MEMORATUM_JOB_LEASE_SECONDS` (default 300, clamped 30–86400) to `src/memoratum/config.py`
- [X] T018 [US1] Wrap `claim()` in `BEGIN IMMEDIATE` in `src/memoratum/jobs.py`; set `lease_expires_at`, clear it on every exit path
- [X] T019 [US1] Add `heartbeat(conn, job_id)` to `src/memoratum/jobs.py` — extends the lease forward only
- [X] T020 [US1] Add `reap_stale(conn, ttl)` to `src/memoratum/jobs.py` — expired-lease `running` → `queued`, lease cleared, `attempts` not decremented
- [X] T021 [US1] Relax `cancel()` in `src/memoratum/jobs.py` to accept `running` **only** when `lease_expires_at < now`
- [X] T022 [US1] Heartbeat on entry to dispatch and piggyback on per-item `update_result` in the `bulk_memories` loop in `src/memoratum/worker.py`
- [X] T023 [US1] Call `jobs.reap_stale()` once per loop iteration in `main()` in `src/memoratum/worker.py`
- [X] T024 [US1] Wrap the whole loop body of `main()` in `except Exception` → log → capped exponential backoff → continue, in `src/memoratum/worker.py`. Never exit on a runtime error.
- [X] T025 [US1] Move `build_vector_store()` inside that guard in `src/memoratum/worker.py` so a provider blip cannot kill the process
- [X] T026 [US1] Update the cancel path in `cancel_job` in `src/memoratum/app.py` so a stale-running `purge_project` still resets `projects.deleting = 0`, and return `409 job is actively progressing` for a live lease
- [X] T027 [US1] In `delete_project` in `src/memoratum/app.py`, stop re-returning a stranded job id; distinguish queued (fine) from running-with-expired-lease (wedged)
- [X] T028 [US1] Add `POST /v4/maintenance/reap-jobs` (admin) to `src/memoratum/app.py`, reporting reaped jobs and `wedged_projects`
- [X] T029 [US1] Create `scripts/recover_stuck_jobs.py` with `--dry-run` / `--force`

**Checkpoint**: quickstart Scenarios 1 and 2 pass. A killed worker's jobs are 100%
recoverable; a wedged project is unblockable via API or script.

---

## Phase 4: WS-A2 — Migration safety [US4]

Goal: an interrupted migration rolls back whole and is retryable.

### Tests *(write first)*

- [X] T030 [P] [US4] `tests/test_migrations.py::test_failed_migration_rolls_back_whole` *(fail first — currently leaves partial DDL)*
- [X] T031 [P] [US4] `tests/test_migrations.py::test_migration_retry_succeeds` *(fail first — currently hard-fails with `duplicate column name`)*
- [X] T032 [P] [US4] `tests/test_migrations.py::test_migrations_containing_foreign_keys_pragma` — identify and cover every existing migration using `PRAGMA foreign_keys=OFF`, which is a no-op inside a transaction
- [X] T033 [P] [US4] `tests/test_migrations.py::test_post_apply_verification` — `integrity_check` and `foreign_key_check` must pass after apply

### Implementation

- [X] T034 [US4] Audit all 27 existing entries of `_MIGRATIONS` in `src/memoratum/db.py`; list every one containing `PRAGMA foreign_keys=OFF` in the PR description before touching the runner
- [X] T035 [US4] Replace `executescript()` in `connect()` in `src/memoratum/db.py` with an explicit transaction per migration; write `schema_migrations.version` in the same transaction as the DDL
- [X] T036 [US4] Add post-apply verification to `connect()` in `src/memoratum/db.py`; fail startup loudly on mismatch
- [X] T037 [US4] Add a disable/enable-around-transaction form for the `PRAGMA foreign_keys` migrations identified in T034

**Checkpoint**: quickstart Scenario 10 passes; an upgrade from a v0.9.0 database
preserves all records.

---

## Phase 5: WS-A3 — Transaction boundary and atomicity [US2]

Goal: one logical operation, one commit; enqueue shares the transaction with its state change.

### Tests *(write first)*

- [X] T038 [P] [US2] `test_failure_injection.py::test_add_is_atomic_at_every_commit_point` — inject a failure at each commit index, re-open, assert all-or-nothing *(fail first — currently 5 commits, yields `docs=1 mem=1 jobs=0`)*
- [X] T039 [P] [US2] `test_failure_injection.py::test_no_orphan_document_without_job` — no document may sit `queued` with no claimable job *(fail first)*
- [X] T040 [P] [US2] `tests/test_data_integrity.py::test_commit_count_per_logical_write` — assert exactly 1 commit per logical add *(fail first — currently 5)*
- [X] T041 [P] [US2] `tests/test_facts_create.py::test_fact_and_memory_commit_together` — a fact-insert failure must not leave an orphan memory

### Implementation

- [X] T042 [US2] Add `transaction(conn)` context manager (`BEGIN IMMEDIATE` / commit / rollback) to `src/memoratum/db.py`
- [X] T043 [US2] Audit all **63** `commit()` call sites across 9 modules (`app.py` 13, `db.py` 21, `webhooks.py` 8, `jobs.py` 7, `vectorstore.py` 5, `facts.py` 3, `worker.py` 3, `backup.py` 1, `dreaming.py` 1, `search.py` 1). Record the audit in the PR; do not edit mechanically.
- [X] T044 [US2] Replace the internal commits in `create_document`, `create_memory`, `ensure_fact_memory`, `update_memory`, `soft_delete_memory`, `categorize_memory`, `set_status` in `src/memoratum/db.py` with `commit: bool = True`
- [X] T045 [US2] Convert the `mem0_add` route in `src/memoratum/app.py` to a single `transaction()` block
- [X] T046 [US2] Convert the remaining composite write routes in `src/memoratum/app.py` and the callers in `src/memoratum/facts.py`, `src/memoratum/worker.py`
- [X] T047 [US2] Keep `db.commit()` inside `backup.py` — backup/restore was verified correct and is out of scope

**Checkpoint**: quickstart Scenario 5 passes; commit count per logical write is 1.

---

## Phase 6: WS-A4 — Concurrency [US2]

Goal: concurrent requests are not serialised. **Depends on Phase 5** — removing the
lock while writes are non-atomic would expose interleavings the lock masks.

### Tests *(write first)*

- [X] T048 [P] [US2] `tests/test_concurrency.py::test_concurrent_requests_overlap` — 4 concurrent requests finish in ~1 round-trip, not 4 *(fail first — currently 1.48 s wall)*
- [X] T049 [P] [US2] `tests/test_concurrency.py::test_concurrent_writes_do_not_corrupt` — parallel writers, no partial state, no lost updates
- [X] T050 [P] [US2] `tests/test_concurrency.py::test_sqlite_busy_is_retried` — raise `busy_timeout`, assert writers serialise rather than fail

### Implementation

- [X] T051 [US2] Narrow `_DB_LOCK` in `get_conn` in `src/memoratum/app.py` to connection setup only; release before the endpoint body
- [X] T052 [US2] Add bounded writer serialisation (writers only, not readers) in `src/memoratum/app.py`
- [X] T053 [US2] Raise `busy_timeout` in `connect()` in `src/memoratum/db.py`
- [X] T054 [US2] Commit the measured before/after latency benchmark to `tests/benchmarks/concurrency.md` — required by Principle IV
- [X] T055 [US2] Correct `docs/OPERATIONS.md`, which currently says the server "serializes writes inside one process" — it serialised *all* traffic

**Checkpoint**: quickstart Scenario 6 passes; benchmark committed.

---

## Phase 7: WS-B1 — SSRF hardening (P0) [US1]

Goal: outbound requests cannot reach internal destinations, by rebinding or by CGNAT range.

### Tests *(write first)*

- [X] T056 [P] [US1] `test_failure_injection.py::test_rebinding_cannot_redirect_connection` — resolver returns public first, link-local second; assert the socket targets the first and that `getaddrinfo` is called **exactly once** *(fail first)*
- [X] T057 [P] [US1] `tests/test_webhooks_security.py::test_cgnat_range_rejected` — `100.64.0.0/10` must be refused *(fail first — confirmed currently allowed)*
- [X] T058 [P] [US1] `tests/test_webhooks_security.py::test_orchid_and_as112_ranges_rejected` — `2001:20::/28`, `2620:4f:8000::/48`, `192.88.99.0/24`
- [X] T059 [P] [US1] `tests/test_webhooks_security.py::test_tls_validation_binds_to_hostname_not_ip` — pinned IP + wrong `server_hostname` must fail cert validation *(fail first)*
- [X] T060 [P] [US1] `tests/test_webhooks_security.py::test_ipv6_scope_id_preserved` — a scoped IPv6 target keeps `flowinfo`/`scope_id`
- [X] T061 [P] [US1] `tests/test_webhooks_security.py::test_tls_error_is_not_retried` — transport errors retry, TLS errors fail fast
- [X] T062 [P] [US1] `tests/test_webhooks_security.py::test_redirects_still_not_followed` — regression guard for the existing `_NoRedirect`

### Implementation

- [X] T063 [US1] Replace `_is_public_ip` in `src/memoratum/webhooks.py` with a positive `is_global` allowlist plus a v4-in-v6 unwrap loop (`ipv4_mapped`, NAT64 `64:ff9b::/96`, 6to4 `2002::/16`)
- [X] T064 [US1] Fuse validation and resolution in `src/memoratum/webhooks.py`: one `getaddrinfo` call returning the full sockaddr list (currently `webhooks.py:188` discards `scope_id`)
- [X] T065 [US1] Add `PinnedHTTPSConnection` overriding `_create_connection` as an **instance attribute** in `src/memoratum/webhooks.py` (a method is silently shadowed by `HTTPConnection.__init__`)
- [X] T066 [US1] Add `PinnedHTTPSHandler.https_open` in `src/memoratum/webhooks.py`, passing `context=self._context` explicitly
- [X] T067 [US1] Rewrite `deliver_delivery` in `src/memoratum/webhooks.py` to connect through the pin while keeping the hostname in the URL; retry only on transport errors
- [X] T068 [US1] Document that `MEMORATUM_WEBHOOK_ALLOW_PRIVATE_TARGETS=true` disables address validation entirely, in `docs/SECURITY.md`
- [X] T069 [US1] Correct the "validated at creation and delivery" claim at `docs/SECURITY.md:21` to describe address pinning

**Checkpoint**: quickstart Scenarios 3 and 4 pass.

---

## Phase 8: WS-B2 — Strict request models [US1]

Goal: a misspelled scope field fails closed instead of writing to global scope.

### Tests *(write first)*

- [X] T070 [P] [US1] `tests/test_security_regressions.py::test_unknown_field_rejected` — `projct_id` returns 422 *(fail first — currently 201 and unscoped)*
- [X] T071 [P] [US1] `tests/test_security_regressions.py::test_wrong_case_field_rejected` — `projectId` returns 422 *(fail first)*
- [X] T072 [P] [US1] `tests/test_mem0_lifecycle.py::test_mem0_add_accepts_project_id` — explicit `project_id` is stored in that project *(fail first — currently silently NULL)*
- [X] T073 [P] [US1] `tests/test_security_regressions.py::test_lenient_compat_restores_legacy` — `MEMORATUM_LENIENT_COMPAT=true` reverts to ignore-extras

### Implementation

- [X] T074 [US1] Add `StrictModel(BaseModel)` with `model_config = ConfigDict(extra="forbid")` in `src/memoratum/app.py`
- [X] T075 [US1] Rebase all ten request models in `src/memoratum/app.py` onto `StrictModel`
- [X] T076 [US1] Add `org_id` and `project_id` to `Mem0AddIn` and `Mem0SearchIn` in `src/memoratum/app.py`
- [X] T077 [US1] Add `MEMORATUM_LENIENT_COMPAT` (default `false`) to `src/memoratum/config.py`, logged loudly at startup
- [X] T078 [US1] Document the breaking change in `docs/MEM0_COMPATIBILITY.md` and `docs/API.md`

**Checkpoint**: quickstart Scenario 9 passes. This is a **breaking API change** —
call it out in the PR title.

---

## Phase 9: WS-B3 — Observability [US3]

Goal: an operator can answer "is it up, is it slow, is work backing up" locally.

### Tests *(write first)*

- [X] T079 [P] [US3] `tests/test_metrics.py::test_exposition_format_valid` — HELP/TYPE present, escaping correct *(fail first)*
- [X] T080 [P] [US3] `tests/test_metrics.py::test_help_text_does_not_escape_quotes` — the label-vs-HELP escaping asymmetry
- [X] T081 [P] [US3] `tests/test_metrics.py::test_route_labels_are_templates` — no concrete ids in labels
- [X] T082 [P] [US3] `tests/test_metrics.py::test_no_tenant_identifiers_in_labels`
- [X] T083 [P] [US3] `tests/test_metrics.py::test_metrics_endpoint_excluded_from_own_counters`
- [X] T084 [P] [US3] `tests/test_metrics.py::test_counter_thread_safety` — concurrent increments under `threading.Lock`
- [X] T085 [P] [US3] `tests/test_health.py::test_ready_fails_when_database_unavailable`
- [X] T086 [P] [US3] `tests/test_health.py::test_ready_reports_schema_diverged`
- [X] T087 [P] [US3] `tests/test_logging.py::test_logs_contain_no_content_or_secrets`

### Implementation

- [X] T088 [US3] Create `src/memoratum/metrics.py` — stdlib registry, `threading.Lock`, counters + gauges + histograms with explicit `(le_string, le_float)` buckets
- [X] T089 [US3] Add `GET /metrics` to `src/memoratum/app.py`, loopback-bound, `Content-Type: text/plain; version=0.0.4; charset=utf-8` set explicitly
- [X] T090 [US3] Compute job depth/age as one indexed aggregate per scrape; add no cache
- [X] T091 [US3] Add `GET /health/live` and `GET /health/ready` to `src/memoratum/app.py`; keep `/health` as an alias for `/health/live` so the Dockerfile `HEALTHCHECK` keeps working
- [X] T092 [US3] Create `src/memoratum/logging_setup.py` — stdlib `logging`, JSON to stdout, scope-safe identifiers only
- [X] T093 [US3] Add `MEMORATUM_LOG_LEVEL`, `MEMORATUM_LOG_FORMAT`, `MEMORATUM_METRICS_ENABLED` to `src/memoratum/config.py`
- [X] T094 [US3] Instrument request middleware in `src/memoratum/app.py` using the registered path template
- [X] T095 [US3] Add the "local scrape is not telemetry" ruling to `docs/AUDIT_METERING.md`
- [X] T096 [US3] Point the Dockerfile `HEALTHCHECK` at `/health/live`

**Checkpoint**: quickstart Scenario 7 passes.

---

## Phase 10: WS-B4 — Retention [US1, FR-006]

Goal: every table has a documented policy; timed tables actually age out.

### Tests *(write first)*

- [X] T097 [P] [US1] `tests/test_retention.py::test_every_table_has_a_policy` — every table in `sqlite_master` has a policy row with a non-empty rationale *(fail first)*
- [X] T098 [P] [US1] `tests/test_retention.py::test_queued_deliveries_never_pruned`
- [X] T099 [P] [US1] `tests/test_retention.py::test_deliveries_pruned_before_events` — FK order
- [X] T100 [P] [US1] `tests/test_retention.py::test_memory_history_and_share_links_pruned` — the two tables with zero delete paths today
- [X] T101 [P] [US1] `tests/test_retention.py::test_dry_run_deletes_nothing`

### Implementation

- [X] T102 [US1] Create the `retention_policies` table (migration 29) in `src/memoratum/db.py` with the CHECK constraint from data-model.md
- [X] T103 [US1] Seed all 19 table policies with rationales, exactly as tabulated in `data-model.md`
- [X] T104 [US1] Create `src/memoratum/retention.py` — policy lookup and the timed sweep, FK-ordered
- [X] T105 [US1] Add `POST /v4/maintenance/prune` (admin, `dry_run` supported) to `src/memoratum/app.py`
- [X] T106 [US1] Add `MEMORATUM_RETENTION_DAYS` (default `0`, opt-in) to `src/memoratum/config.py`
- [X] T107 [US1] Run the sweep daily from `main()` in `src/memoratum/worker.py`
- [X] T108 [US1] Add `PRAGMA wal_checkpoint(TRUNCATE)` and incremental `VACUUM` to `src/memoratum/maintenance.py`

**Checkpoint**: quickstart Scenario 8 passes.

---

## Phase 11: WS-B5 — Search performance

Goal: no per-row embedding calls; latency flat as the corpus grows.

### Tests / benchmark *(write first)*

- [X] T109 [P] `tests/test_search_perf.py::test_no_reembedding_of_stored_chunks` — assert `embedder.embed` is **not** called with chunk texts when stored vectors exist *(fail first — currently 157 API calls at 10k chunks)*
- [X] T110 [P] `tests/test_search_perf.py::test_fts_prefilter_bounds_vector_candidates`
- [X] T111 [P] Commit the benchmark to `tests/benchmarks/search.md`: latency at 500 / 1,000 / 2,000 / 4,000 memories, before and after *(fail first — currently 90 ms → 1,581 ms, linear)*

### Implementation

- [X] T112 Use `c.embedding` selected at `search.py:184` instead of re-embedding at `search.py:251`; add a backfill for pre-existing rows
- [X] T113 Add the FTS5 candidate prefilter in `src/memoratum/search.py` — reuse `db.keyword_search` top-N (`N = max(limit*20, 200)`), fuse via the existing `_RRF_K = 60`
- [X] T114 Narrow the SELECT to `(id, vector)`; fetch `text`/`metadata` only for the ≤10 survivors
- [X] T115 Read vectors via `array('f')`/`memoryview`; build `VectorHit`s after top-k
- [X] T116 Re-tune against the eval harness — done: `MEMORATUM_SEARCH_PREFILTER_MIN` makes it reproducible; default 512

**Checkpoint**: latency flat from 500 → 4,000. `sqlite-vec` stays deferred per
research.md D4, with the ANN trigger recorded in `tests/benchmarks/search.md`.

---

## Phase 12: WS-B6 — CI gates and coverage integrity [US6]

- [X] T117 [P] [US6] Add a CI step failing if the count of `pragma: no cover` **increases**, in `.github/workflows/ci.yml`
- [X] T118 [US6] Add a CI step failing if `fail_under` in `pyproject.toml` is lower than in `HEAD~1`
- [X] T119 [US6] Confirm `pip-audit --local` and `npm audit --audit-level=high` run as required checks in `.github/workflows/security.yml`
- [X] T120 [US6] Add per-module coverage floors seeded from the measured table (research.md D3): `worker.py` 59%, `mcp.py` 0%, `webhooks.py` no regression
- [X] T121 [US6] Raise `mcp.py` off 0% with real MCP server tests in `tests/test_mcp.py`
- [X] T122 [US6] Add no coverage badge to `README.md`

---

## Phase 13: WS-B7 — Documentation reconciliation [US5]

- [X] T123 [US5] Correct the webhook validation claim at `docs/SECURITY.md:21` (overlaps T069 — do once)
- [X] T124 [US5] Correct the serialisation claim in `docs/OPERATIONS.md` (overlaps T055 — do once)
- [X] T125 [US5] Add the 20 undocumented variables to `.env.example` per `contracts/env-vars.md`
- [X] T126 [US5] Add `MEMORATUM_WEBHOOK_ENCRYPTION_KEY` to both services in `docker-compose.yml`
- [X] T127 [US5] Document the retention policy table in `docs/OPERATIONS.md`
- [X] T128 [US5] Add the destructive-operation recoverability annotations to `docs/API.md`
- [X] T129 [US5] Document job lease/recovery and the operator recovery script in `docs/OPERATIONS.md`
- [X] T130 [US5] Execute the `README.md` quickstart, `docs/OPERATIONS.md` backup/restore, and `docs/SECURITY.md` webhook setup **verbatim** in a clean checkout; fix any undocumented step
- [X] T131 [US5] Cross-check `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/OPERATIONS.md` against `.specify/memory/constitution.md`; resolve contradictions

---

## Phase 14: WS-B8 — Packaging [US7]

- [X] T132 [P] [US7] Build the container image and confirm it serves traffic with SQLite only
- [X] T133 [P] [US7] Confirm the base package installs with minimal core deps and every provider sits behind an extra
- [X] T134 [US7] Verify licence metadata: AGPL server, MIT clients, no blurring
- [X] T135 [US7] Confirm no plaintext secret anywhere in the repository

---

## Phase 15: Polish and validation

- [X] T136 Run the full gate set from `quickstart.md` Scenario 12 and record the output
- [X] T137 Run every quickstart scenario and attach measured numbers — coverage, search latency at 4 corpus sizes, concurrency wall time, migration retry
- [X] T138 Confirm `scripts/check_no_telemetry.py` still passes with the new modules
- [X] T139 Remove the Sync Impact Report HTML comment from `.specify/memory/constitution.md` before committing
- [ ] T140 Open a PR to `main` with a **merge commit** (repo convention — no squash, no rebase) and state what was verified and how.
  **NOT DONE — needs explicit authorisation.** No remote operation has been performed in
  this session; all work is committed locally on `001-production-ready`. Pushing and
  opening a PR are the user's call, per the standing rule that GitHub actions need a
  fresh go-ahead.

---

## Dependencies and execution order

```
Phase 1 Setup
   └── Phase 2 WS-A Foundation (T005 blocking)
          ├── Phase 3 WS-A1 job durability (P0)   ─┐
          └── Phase 4 WS-A2 migrations (US4)       ─┤ WS-A: sequential, reviewed
                 └── Phase 5 WS-A3 atomicity (US2)  │ task-by-task
                        └── Phase 6 WS-A4 concurrency
                                                     │
Phase 7 WS-B1 SSRF (P0) ───────────────────────────┴── can run in parallel with WS-A
Phase 8 WS-B2 strict models ─────────────────────────────┐
Phase 9 WS-B3 observability ─────────────────────────────┤ WS-B: parallelisable
Phase 10 WS-B4 retention ───────────────────────────────┤
Phase 11 search perf ───────────────────────────────────┤
Phase 12 CI + coverage ─────────────────────────────────┘
   └── Phase 13 docs ── Phase 14 packaging ── Phase 15 validation
```

**Hard ordering constraints**

1. **T005 before every WS-A fix.** No durability fix without a failure-injection test.
2. **Phase 5 before Phase 6.** Narrowing the lock while writes are non-atomic
   exposes partial-write interleavings the lock currently masks.
3. **Phase 3 before Phase 5.** The reaper must exist before commit semantics change,
   so an interrupted migration cannot strand work in a new way.
4. **T013 before T016.** The lease migration changes what that existing test means;
   the fixture must be corrected first so the suite is never red for the wrong reason.

**Independent work**: WS-B (Phases 7–12) touches different files from WS-A and can
proceed in parallel, except T069/T123 and T055/T124, which are duplicates — do each
once, in the earlier phase.

---

## Notes

- **[P]** = different files, no dependency. Safe to parallelise.
- **Write the test first and watch it fail.** The test task always precedes its fix.
- **Report measured numbers, not assertions.** Quickstart Phase 15 exists because
  "tests pass" is not evidence (Principle IV).
- **Commit after each task or logical group**, on a branch, merging with a
  `Merge pull request` commit.
- **WS-A is not a candidate for `scripts/prod-loop.sh`.** The script now runs only
  `/speckit.implement` (the nonexistent `/speckit.converge` step was removed), but an
  unattended pass repeated N times can still report success while losing a
  durability invariant. Implement WS-A task-by-task.