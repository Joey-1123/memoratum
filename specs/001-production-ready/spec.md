# Feature Specification: Production Readiness Hardening

**Feature Branch**: `001-production-ready`

**Created**: 2026-10-03

**Status**: Draft

**Input**: User description: "Make memoratum production-ready: enumerate the gaps against the constitution (security hardening, test coverage and CI, observability, docs matching behavior, safe migrations, packaging) as acceptance criteria"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Operator runs Memoratum safely in production (Priority: P1)

An operator deploys Memoratum on their own infrastructure and needs confidence that the documented guarantees — tenant isolation, durable work, SSRF defense, encrypted secrets, backup/restore integrity — actually hold against real failure injection, not just passing unit tests.

**Why this priority**: Gaps in security invariants are the project's highest-severity risks; several are explicitly recorded as known debt in the constitution.

**Independent Test**: Run the security and failure-injection test subset against a real (non-mocked) wired-up server and observe every case exercising the real code path fail closed.

**Acceptance Scenarios**:

1. **Given** an operator configures a webhook URL, **When** the target hostname changes its DNS answer between validation and connection, **Then** the request is pinned to the validated address and cannot reach internal or link-local destinations (closes the documented DNS-rebinding window).
2. **Given** a redirect response from a configured URL, **When** the redirect target has not been validated, **Then** it is not followed.
3. **Given** outbound calls to operator-configured URLs, **When** responses are slow or oversized, **Then** they are bounded by explicit timeout and response-size limits.
4. **Given** a running worker, **When** it is killed uncatchably mid-job, **Then** the claimed job's lease expires and the job is recovered by the reaper rather than stranded.
5. **Given** a queued job or its lease record, **When** an operator requests cancellation, **Then** a job not actively progressing is cancelled.
6. **Given** any claimed job, **When** it is actively progressing, **Then** it carries a heartbeat and is never stolen by another worker.
7. **Given** a background job that raises an unexpected error, **When** the error propagates, **Then** the worker process survives and continues processing.
8. **Given** webhook signing secrets at rest, **When** the store is inspected on disk, **Then** secrets are encrypted; key material is supplied by the operator or generated into the data directory with restrictive permissions.
9. **Given** key material and operator-controlled secrets, **When** the repository and logs are inspected, **Then** no plaintext secrets are present.
10. **Given** an authorization failure for a resource another tenant owns, **When** an API caller requests it, **Then** the response is a uniform 404 indistinguishable from a nonexistent resource.
11. **Given** usage/audit records, **When** the system operates, **Then** records are retained locally and never transmitted.

---

### User Story 2 - Logical operations behave atomically under real concurrency (Priority: P1)

An operator runs concurrent read/write traffic and needs every logical operation to commit at a single transaction boundary, with jobs enqueued in the same transaction as the state change they depend on, so a crash cannot leave half-applied mutations or orphaned follow-up work.

**Why this priority**: The constitution records commit-boundary and enqueue-atomicity debt; without it durability claims are false.

**Independent Test**: Kill the process at injected points inside logical operations and verify on restart that each operation is either fully applied (including its enqueued work) or fully absent, and that throughput under concurrent request-scoped lock removal is measured and committed to the repo.

**Acceptance Scenarios**:

1. **Given** a state change that requires follow-up work, **When** either is persisted, **Then** both are enqueued/committed in the same transaction.
2. **Given** a logical operation spanning multiple helpers, **When** it executes, **Then** a single commit occurs at the operation's boundary and helpers do not commit on their caller's behalf.
3. **Given** traffic on multiple containers/scopes, **When** the previous request-scoped database lock is removed, **Then** concurrent operations on different scopes make progress independently, with the resulting latency measured and recorded.
4. **Given** a crash injected mid-operation, **When** the store recovers, **Then** no partial mutation and no orphaned job exists.

---

### User Story 3 - Operator can observe health, usage, and failures (Priority: P2)

An operator needs structured logs, metrics, and health endpoints sufficient to answer "is it up, is it slow, is work backing up" without reading source, and local-only — no external telemetry.

**Why this priority**: Observability is absent as a verified capability today; it is a prerequisite to operating anything in production.

**Independent Test**: Start the server, generate load and injected failures, and confirm metrics/health/log output reflects them and that `scripts/check_no_telemetry.py` still passes.

**Acceptance Scenarios**:

1. **Given** a running server, **When** an operator scrapes or queries the metrics endpoint, **Then** request counts, latencies, job queue depth/age, and error counts are available.
2. **Given** storage, provider, or worker degradation, **When** it occurs, **Then** health reflects it (liveness vs readiness distinguishable).
3. **Given** a request lifecycle, **When** it completes or fails, **Then** structured log lines exist with scope-safe identifiers and no secrets.
4. **Given** observed usage, **When** an operator reviews the audit/metering record, **Then** usage is retained locally with a documented retention policy.

---

### User Story 4 - Schema changes and upgrades are safe (Priority: P2)

An operator upgrading Memoratum needs migrations that are reversible or explicitly irreversible, verified against real data, and whose tables carry documented retention policies — no silent destructive changes.

**Why this priority**: Unsafe migrations are a leading cause of unrecoverable production incidents; the constitution already mandates safe backup/restore and retention.

**Independent Test**: Run the upgrade path from the previous release's schema to the new one on a populated database and verify data integrity, backup integrity, and documented rollback behavior.

**Acceptance Scenarios**:

1. **Given** an existing production database, **When** a migration runs, **Then** the migration is applied inside a transaction, is idempotent or tracked, and the resulting schema is verified.
2. **Given** every table, **When** an operator consults the retention policy, **Then** a documented policy exists (retain-indefinitely vs timed vs size-bounded), and no table lacks one.
3. **Given** an operator requests backup, **When** backup completes, **Then** SQLite's online backup mechanism captured WAL content and restore verifies integrity before and after writing.
4. **Given** a destructive operation, **When** it is exposed in the API, **Then** the contract documents whether it is recoverable or explicitly irreversible.

---

### User Story 5 - Documentation matches actual behavior (Priority: P2)

A new operator follows the docs to deploy, configure, and operate Memoratum; every documented behavior must be implemented, and every implemented behavior must not contradict the docs.

**Why this priority**: The constitution declares doc/behavior mismatches defects; production readiness fails if operators cannot trust the docs.

**Independent Test**: Execute the documented quickstart, security, operations, and provider-configuration procedures verbatim and confirm they succeed as written.

**Acceptance Scenarios**:

1. **Given** a fresh environment, **When** the operator follows the documented quickstart, **Then** it reaches a working install with zero undocumented steps.
2. **Given** each documented endpoint/config knob, **When** exercised, **Then** observed behavior matches the description in `docs/`.
3. **Given** an unimplemented documented behavior or a documented-behavior contradiction, **When** found in this audit, **Then** it is fixed in the same change as this spec's remediation.
4. **Given** `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, and `docs/OPERATIONS.md`, **When** cross-checked against the constitution, **Then** none contradicts it.

---

### User Story 6 - Every change is gated by CI (Priority: P2)

A contributor opens a pull request; CI must run and must be required for merge on the full gate set, including dependency security scanning as its own workflow.

**Why this priority**: Without enforced coverage and required checks, green-locally drifts from green-in-CI and regressions land.

**Independent Test**: Open a PR that deliberately weakens a check (e.g., skips a test, adds a vulnerable dependency, drops the telemetry script) and confirm CI fails and blocks merge.

**Acceptance Scenarios**:

1. **Given** a PR, **When** CI runs, **Then** all constitution-listed commands execute: telemetry check, mem0 official-SDK gate, ruff check and format-check, full pytest, TS client tests, opencode plugin syntax check, dashboard install+build.
2. **Given** a changed Python or JS dependency, **When** CI runs, **Then** `pip-audit --local` and `npm audit --audit-level=high` run as a separate security workflow and are required.
3. **Given** test coverage, **When** the suite runs, **Then** coverage over `src/` meets the project's stated threshold and is reported, with failure-injection and regression tests included in the count.
4. **Given** a failing gate, **When** a merge is attempted, **Then** the merge is blocked (required checks), and merge commits (not squash/rebase) remain the policy.
5. **Given** every bug fix merged, **When** reviewed, **Then** it ships with a regression test that fails when the fix is reverted.

---

### User Story 7 - Operator consumes a clean, installable package (Priority: P3)

An operator installs Memoratum from its published artifacts — a container image, Python package, TS client, or dashboard bundle — and gets exactly the documented licenses, extras, and configuration.

**Why this priority**: Packaging defects surface at install time in production, but core functionality is already usable from source; P3.

**Independent Test**: Build all published artifacts from a clean checkout and run them: the container starts, the Python package installs with correct extras and a resolvable import, and the TS client imports.

**Acceptance Scenarios**:

1. **Given** the Dockerfile, **When** the image builds and runs, **Then** it completes on a single machine with SQLite only, and the image starts the server with documented environment variables.
2. **Given** `pyproject.toml`, **When** a user installs the base package, **Then** only minimal core dependencies are required and every provider integration is behind an optional extra reachable through one interface.
3. **Given** published artifacts, **When** their LICENSE metadata is inspected, **Then** server code is AGPL-3.0-or-later and client/SDK code is MIT, with no blurring across that boundary.
4. **Given** `.env.example` and packaging docs, **When** compared, **Then** every required environment variable is documented with safe defaults and no undocumented required variable exists.

---

### Edge Cases

- What happens when the operator kills a worker with SIGKILL mid-job? (Covered by recovery/lease requirement.)
- How does the system handle a webhook URL whose hostname re-resolves after validation? (Covered by DNS pinning requirement.)
- What happens when backup runs during heavy WAL traffic? (Online backup mechanism must capture WAL.)
- How are uniform-404 responses kept distinguishable from genuine 404s by operators? (Logged server-side with scope, not client-side.)
- What happens when a migration is interrupted halfway? (Transaction-wrapped, idempotent, verifiable.)
- What happens when an optional provider extra is not installed? (Documented local fallback, never a hard failure.)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Close the webhook SSRF gap — resolved addresses MUST be pinned for the connecting socket, redirects MUST NOT be followed to unvalidated hosts, and all outbound calls MUST have an explicit timeout and response size limit.
- **FR-002**: Add lease, heartbeat, and a reaper to job claims; a non-progressing job MUST be operator-cancellable; a background job failure MUST NOT terminate the worker.
- **FR-003**: Enforce single-commit-boundary per logical operation; helpers MUST NOT commit; work enqueue MUST share the transaction with its dependent state change; document and mitigate the request-scoped lock serialization with measured results.
- **FR-004**: Encrypt webhook signing secrets at rest; key material MUST be operator-supplied or generated with restrictive permissions; document the key-handling contract.
- **FR-005**: Provide liveness and readiness health signals, structured logs, and local metrics for requests, errors, and job depth; no data leaves the host.
- **FR-006**: Every table MUST have a documented retention policy; backup MUST use SQLite's online backup and restore MUST verify integrity before and after.
- **FR-007**: Migrations MUST be transaction-wrapped, tracked, and verified; destructive operations MUST be documented as recoverable or explicitly irreversible.
- **FR-008**: `docs/` MUST be reconciled with implementation in the same change; doc/behavior mismatches are defects to fix.
- **FR-009**: CI MUST enforce the full constitution gate set as required checks, including pip-audit, npm audit, telemetry check, lint/format, full test suites, and dashboard/client builds.
- **FR-010**: Test coverage MUST meet the project threshold, include failure-injection tests that exercise real code paths (no mocks standing in for the asserted behavior), and every fix MUST include a regression test.
- **FR-011**: Packaging MUST produce a working container image, a minimal base Python install with optional extras per provider, correct AGPL/MIT license split, and a complete `.env.example`.
- **FR-012**: No plaintext secrets in the repository; no external telemetry; `scripts/check_no_telemetry.py` MUST remain green.

### Key Entities

- **Webhook endpoint**: Operator-configured outbound URL with an encrypted signing secret, timeout/size caps, pinned connections, and a retention-governed delivery record.
- **Job / lease / reaper**: Background work item claimed with a lease carrying heartbeat; reaped on lease expiry; cancellable when not progressing.
- **Scope (container/org/project)**: Tenant boundary constraining every read/write; authorization failures surface as uniform 404.
- **Audit and usage records**: Locally retained, never transmitted, with documented retention.
- **Migration record**: Tracked, transaction-wrapped schema change verified post-apply.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of the constitution's recorded known-debt items (SSRF DNS pinning, job leases/reaper, commit granularity, request-scoped lock, table retention) have a closed, test-backed state.
- **SC-002**: A full CI gate run (lint, format, full pytest, mem0 official-SDK suite, TS tests, dashboard build, telemetry check, pip-audit, npm audit) passes end-to-end from a clean checkout.
- **SC-003**: Every `docs/` procedure in the quickstart, security, and operations docs executes verbatim without an undocumented step, and no documented behavior contradicts implementation.
- **SC-004**: Upgrading a populated database from the prior release's schema to the new one preserves all records (verified by integrity check and row counts) and completes inside the documented transaction boundary.
- **SC-005**: The published container image and the base Python package install and serve traffic using SQLite only, with all provider integrations behind optional extras.
- **SC-006**: Zero plaintext secrets and zero outbound telemetry endpoints exist; the telemetry check and `npm audit --audit-level=high` / `pip-audit` report no unresolved high-severity findings.
- **SC-007**: A killed worker leaves 100% of its claimed jobs recoverable by the reaper, and concurrent same-scope lock removal yields a committed, measured latency report in the repo.

## Assumptions

- The constitution at `.specify/memory/constitution.md` (v1.0.0) is the binding quality bar; this spec adds criteria, not new principles.
- The gaps recorded in the constitution's ratification notes are representative and will be re-audited as part of this work; newly found gaps join the same acceptance-criteria list.
- Observability is implemented with local-only mechanisms consistent with the no-external-telemetry principle; exporting to operator-chosen infrastructure is permitted only as documented, opt-in behavior.
- Test coverage threshold approach: measure-and-hold. Establish the real coverage number on day one, gate CI at exactly that value, and ratchet upward only when a change is genuinely well-tested. An aspirational absolute floor is explicitly rejected as it reliably produces deleted tests.
- Contract documentation scope: contracts cover the externally visible surface only — HTTP endpoints and operator-facing environment variables. Internal designs (job lease/reaper, commit-boundary enforcement) are documented in `data-model.md` and the plan, not as contracts.
- Two findings from repo-internal research are promoted to their own explicitly ordered workstreams rather than grouped with their FR siblings: (a) migrations are currently not transaction-wrapped, so a mid-migration failure permanently blocks startup (FR-007); (b) `docs/SECURITY.md` overstates webhook delivery validation, overlapping the SSRF DNS-rebinding window (FR-008/FR-001).
- Dashboard, TS client, and opencode client are in scope for packaging and CI gates as already defined in the constitution.
