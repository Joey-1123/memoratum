# Implementation Plan: Production Readiness Hardening

**Branch**: `001-production-ready` | **Date**: 2026-10-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-production-ready/spec.md`

## Summary

Close the five gaps recorded as known debt in the constitution's ratification notes
— webhook SSRF, job durability, commit granularity, request serialisation, and
table retention — then add the evidence channel (metrics, health, coverage,
committed benchmarks) the project currently lacks to tell whether any of it
actually holds. Every defect below was reproduced with a runnable probe before
being planned; the probe results are quoted in [`research.md`](research.md).

Technical approach: additive schema changes behind a new transaction-wrapped
migration runner; stdlib-only implementations for pinning, metrics, and logging so
no runtime dependency is added; a lease/heartbeat/reaper protocol on the job queue;
and a single-commit-boundary discipline enforced by an explicit transaction
context manager. Work is split into **two workstreams** (below) sequenced so that
durability lands before concurrency.

## Technical Context

**Language/Version**: Python 3.11+ (CI runs 3.12; Docker image is 3.12-slim)

**Primary Dependencies**: `fastapi`, `pydantic` v2, `uvicorn`, `cryptography` —
all mandatory. Optional extras only: `llm` (litellm), `embeddings` (fastembed),
`qdrant`, `chroma`, `pgvector`. **This feature adds zero runtime dependencies.**

**Storage**: SQLite 3.53 in WAL mode, `busy_timeout=5000`, `foreign_keys=ON`.
19 tables. Non-negotiable default per Constitution Principle I.

**Testing**: `pytest` (79 files, 241 tests, 92 s). Adding `pytest-cov` as a
**dev-only** dependency with `branch = true`.

**Target Platform**: Linux server, single node, Docker or bare process.

**Project Type**: Web service (HTTP API) + background worker + CLI helpers.

**Performance Goals**: Set from measurement, not aspiration.
- Webhook delivery: zero `getaddrinfo` calls during connect (currently one).
- Search: latency flat from 500 → 4,000 memories (currently 90 ms → 1,581 ms,
  linear) and no per-row embedding calls.
- Concurrency: 4 concurrent requests complete in ~1 round-trip, not 4 (currently
  1.48 s wall, fully serialised).
- Job recovery: 100% of claimed jobs recoverable after an uncatchable kill.

**Constraints**: No new runtime dependency. No external telemetry. AGPL server /
MIT clients. Every network call bounded by timeout and response size. All provider
integrations behind optional extras.

**Scale/Scope**: 11.1k LOC across 29 modules in `src/memoratum`; 7.3k LOC of tests;
5,029 statements measured; 79% statement / 75.5% branch coverage.

## Constitution Check

*GATE: passed pre-research; re-checked post-design below.*

| Principle / rule | Applies | Status |
|---|---|---|
| I. Self-Hosted and Local-First | Yes | **Pass** — no runtime dependency added; `pytest-cov` is dev-only; all provider work stays behind extras |
| II. Hard Isolation Between Scopes | Yes | **Pass, and extended** — D10 closes the silent scope-widening path; FR-013 |
| III. Durable and Recoverable Work | Yes | **Pass** — D5, D7, D9 implement lease/heartbeat/reaper, transaction-wrapped migrations, single-commit-boundary |
| IV. Evidence Before Assertion | Yes | **Pass** — every defect reproduced by probe; coverage gate and committed benchmarks are the standing evidence channel |
| V. Provider-Neutral, Minimal Dependencies | Yes | **Pass** — pinning, metrics, and logging are all stdlib; the `requests`/`httpx`/`prometheus_client` options were each rejected on this principle |
| Secrets never plaintext | Yes | **Pass** — unchanged; key-loss detection hardened |
| SSRF: pin the validated address | Yes | **Pass** — D1, the binding constraint that made the fix non-obvious |
| Backup uses online mechanism, verified | Yes | **Already satisfied** — `backup.py` verified correct; no change needed |
| Retention policy for every table | Yes | **Pass** — D6, all 19 tables classified |
| Full CI gate set | Yes | **Pass** — existing commands unchanged; coverage folded into existing `pytest -q` |
| Docs match behaviour | Yes | **Pass, with a correction** — `docs/SECURITY.md:21` overstates webhook validation; `docs/OPERATIONS.md` understates serialisation |
| Merge commits, not squash | Yes | **Pass** — workflow constraint, recorded in `tasks.md` |

**No violations requiring a Complexity Tracking entry.** The work reduces existing
debt and adds no new architectural commitment.

### Post-design re-check

Two design decisions were tested against Principle V and held:

- `sqlite-vec` was **rejected for now** specifically because it is a native
  dependency with gaps in prebuilt ARM binaries — the exact audience this project
  serves. Adopting it later is a configuration swap, because the `VectorStore`
  Protocol already abstracts the boundary.
- Serving `/metrics` through FastAPI rather than a second `http.server` listener
  avoids introducing an unauthenticated, un-rate-limited attack surface.

One honest tension: D10 (`extra="forbid"`) is a **breaking API change** for
third-party Mem0-compatible clients. Principle II requires it — failing open on a
scope typo is a tenant-isolation defect — and `MEMORATUM_LENIENT_COMPAT` is the
documented escape hatch. This is a deliberate trade of client compatibility for
isolation correctness, and it is recorded here rather than buried.

## Workstreams

Two workstreams, sequenced so durability lands before concurrency. Removing the
request lock while writes are non-atomic would expose partial-write interleavings
that the lock is currently masking by accident.

### WS-A — Durability and Recovery

Closes the outage class: work that cannot be lost, cannot be stranded, and cannot
be half-applied.

| Item | Requirement | Research |
|---|---|---|
| Job lease, heartbeat, reaper | FR-002 | D5 |
| Cancellable stale-running jobs | FR-002 | D5 |
| Worker survives background failure | FR-002 | D5 |
| Transaction-wrapped, verified migrations | FR-007 | D7 |
| Single commit boundary per operation | FR-003 | D9 |
| Request lock narrowed + benchmark committed | FR-003 | D8 |

### WS-B — Trust and Operability

Restores trust in the project's claims, and supplies the evidence channel.

| Item | Requirement | Research |
|---|---|---|
| SSRF address pinning + CGNAT allowlist fix | FR-001 | D1 |
| Strict request models | FR-013 (promoted) | D10 |
| Metrics, structured logs, health split | FR-005 | D2, D11 |
| Retention policy + sweep for all 19 tables | FR-006 | D6 |
| Coverage gate + anti-gaming controls | FR-010 | D3 |
| Search: reuse persisted embeddings + FTS prefilter | Performance goal | D4 |
| Docs reconciled with behaviour | FR-008 | D1, D8 |
| CI gates, env vars, packaging | FR-009, FR-011, FR-012 | D3 |

**Sequencing.** WS-A first: it changes durability semantics, and everything else is
safer to build on a queue that cannot strand work. Within WS-A, migration safety
lands before commit-boundary work, and commit-boundary lands before the lock change.

## Project Structure

### Documentation (this feature)

```text
specs/001-production-ready/
├── plan.md              # This file
├── spec.md              # Input specification
├── research.md          # Phase 0 — 11 decisions, each probe- or measurement-backed
├── data-model.md        # Phase 1 — entities, states, retention policy for 19 tables
├── quickstart.md        # Phase 1 — runnable validation guide
├── contracts/
│   ├── http-api.md      # New + changed endpoints, error contract
│   └── env-vars.md      # New config surface + .env.example reconciliation
├── checklists/
│   └── requirements.md  # Spec quality checklist
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

Existing layout, with the modules this feature touches marked. No new top-level
directory is introduced.

```text
src/memoratum/
├── app.py          # MODIFIED  strict models, Scoped-Mem0 scope fields,
│                   #           health split, /metrics, prune + reap endpoints,
│                   #           narrowed _DB_LOCK
├── db.py           # MODIFIED  db.transaction(), commit removal (21 sites),
│                   #           migration runner, retention registry
├── jobs.py         # MODIFIED  lease/heartbeat/reaper, atomic claim, stale cancel
├── worker.py       # MODIFIED  top-level exception handling, reaper loop,
│                   #           heartbeat on bulk progress
├── webhooks.py     # MODIFIED  pinned connection, positive is_global allowlist,
│                   #           IPv6 sockaddr handling
├── config.py       # MODIFIED  new settings
├── backup.py       # VERIFIED  already correct — no change
├── search.py       # MODIFIED  reuse persisted chunk embeddings, FTS prefilter
├── ingest.py       # VERIFIED  already persists embeddings — no change
├── metrics.py      # NEW       stdlib Prometheus exposition registry
├── logging_setup.py# NEW       stdlib logging, JSON formatter
├── retention.py    # NEW       policy registry + timed sweep
└── mcp.py          # COVERAGE  0% today; must reach a non-zero floor

scripts/
├── check_no_telemetry.py   # unchanged, must stay green
├── recover_stuck_jobs.py   # NEW  operator recovery for wedged projects
└── reindex_vectors.py      # unchanged

tests/
├── test_failure_injection.py   # NEW  crash-between-commits, kill-worker,
│                              #      rebinding resolver, reopen-after-crash
├── test_metrics.py             # NEW  format, escaping, cardinality, thread safety
├── test_retention.py           # NEW  per-table policy coverage
├── test_migrations.py          # NEW  interrupt + retry
├── test_security_regressions.py# EDIT one fixture — see below
└── ... 79 existing files

contracts are in specs/001-production-ready/contracts/
```

**Structure Decision.** Keep the existing flat `src/memoratum/` package. Three new
modules (`metrics.py`, `logging_setup.py`, `retention.py`) are added because each
owns a distinct concern with its own tests; none of them warrants a subpackage.
Tests follow the existing flat convention rather than a `unit/`/`integration/`
split, because 79 of the 81 existing test files are flat and reorganisation would
inflate the diff without improving the signal.

**One existing test must be edited, and that is expected.**
`tests/test_security_regressions.py:171` sets `status='running'` by raw SQL and
asserts the job is never reclaimed. Under a lease, `lease_expires_at` defaults to
`0`, so that job is correctly reaped as dead. The test must set a future
`lease_expires_at` to model a *live* claim. The invariant it protects — never steal
running work — is legitimate and is preserved; only the fixture needs correcting.

## Complexity Tracking

> No constitution violations. No entries required.

Two rejected-for-now options are recorded in `research.md` rather than here, since
they are not violations but premature complexity: `sqlite-vec` (D4) and a
`prometheus_client` dependency (D2).