# Phase 2: Polish & Validation

Run in this order. Record the actual output — Principle IV forbids reporting
success for anything not executed.

## Setup

- [ ] T136 Run the full gate set from [`quickstart.md` Scenario 12](./quickstart.md) and record the output below
- [ ] T137 Run every quickstart scenario and attach the measured numbers: coverage, search latency at 4 corpus sizes, concurrency wall time, migration retry
- [ ] T138 Confirm `scripts/check_no_telemetry.py` still passes with the new modules
- [ ] T139 Remove the Sync Impact Report HTML comment from `.specify/memory/constitution.md`
- [ ] T140 Open a PR to `main` with a **merge commit** and state what was verified and how

## Measured results

Recorded from the run that closed this phase. These are observations, not targets.

### Test suite

| Metric | Before this feature | After |
|---|---:|---:|
| Tests | 241 | 425 |
| Statement coverage | 79.5% | 76.3% (branch-aware) |
| Skipped | 0 | 1 (docker unavailable) |

Coverage fell in absolute terms because `branch = true` is stricter than
statement coverage and because three new modules (`metrics.py`, `retention.py`,
`logging_setup.py`) landed partially covered. The floor was set from measurement
rather than aspirationally, so this is a more honest number, not a worse one.

### Defects closed

| Severity | Defect | Evidence |
|---|---|---|
| P0 | Worker killed mid-job stranded work forever; `purge_project` wedged a project with no recovery | `tests/test_recovery.py` (6), `tests/test_jobs_lease.py` (13) |
| P0 | Webhook SSRF via DNS rebinding | `test_delivery_resolves_exactly_once` asserts one lookup |
| P0 | Webhook SSRF via CGNAT (`100.64.0.0/10` allowed outright) | `test_special_use_ranges_are_rejected` |
| P1 | Interrupted migration left partial DDL and blocked startup permanently | `tests/test_migrations.py` (9) |
| P1 | Worker died on any transient error | `tests/test_worker_resilience.py` (5) |
| P1 | One logical write = 5 commits; crash left orphan content with no job | `tests/test_atomicity.py` (8) |
| P1 | Misspelled `project_id` wrote to global scope with a 200 | `tests/test_request_strictness.py` (9) |
| P2 | All requests serialised behind `_DB_LOCK` | `tests/test_concurrency.py` (7), AST + direct lock probe |
| P2 | No metrics, logs, or readiness signal | `tests/test_observability.py` (19) |
| P2 | Unbounded growth; two tables never pruned | `tests/test_retention.py` (15) |
| P2 | Search re-embedded the whole corpus per query | `tests/test_search_perf.py` (11) |
| P2 | 20 env vars undocumented, 2 security-relevant | `tests/test_docs_conformance.py` (18) |

### Performance

| Claim | Before | After | Committed at |
|---|---|---|---|
| Search provider calls | O(corpus) — 157 calls at 10k chunks | O(missing rows) — 0 in steady state | `tests/benchmarks/search.md` |
| Request concurrency | 4 requests = 1.48 s wall, fully serialised | lock released before the endpoint body | `tests/benchmarks/concurrency.md` |

Search latency remains roughly linear in corpus size. Stage 1 removed the
dominant cost (provider calls); making it sublinear requires the FTS5 candidate
prefilter, which is deferred — it changes result ordering and needs re-tuning
against the eval harness. Recorded as T113–T116, still open.

## Not verified here

- **Container image build and boot** (`test_docker_image_builds_and_starts`)
  skips because docker is unavailable in this environment. SC-005's container half
  must be exercised on a host with a docker daemon before it can be claimed.
- **Search Stage 2** (bounded candidate set) — deliberately deferred, see above.
- **`sqlite-vec` ANN** — deferred behind the trigger recorded in
  `tests/benchmarks/search.md`.

## Follow-ups: closed after the merge

Both items left open at merge have been addressed in the follow-up change.

### MCP server coverage — 0% to 95.9%

`src/memoratum/mcp.py` was the least-tested module in the project at **0%**. It
is the one server surface with no HTTP boundary, so nothing else in the suite ever
reached it — the JSON-RPC framing *is* the contract and nothing tested it.

`tests/test_mcp.py` drives the real `main()` loop over real stdin/stdout rather
than importing helpers, and covers the handshake, `tools/list`, a
remember/recall round trip, container-tag isolation between recalls, `limit`,
unknown tool and method, missing arguments, malformed JSON, blank lines,
one-reply-per-request ordering, and the empty-stdin case.

No functional defect was found: the server already worked end-to-end. It was
simply untested.

### Remaining composite write paths

`POST /v3/documents` and `POST /v4/facts` each still issued **4 commits** after the
single-boundary work, so a crash could still leave half a write behind. Measured,
not assumed:

| Endpoint | Before | After |
|---|---:|---:|
| `POST /v3/documents` | 4 | 2 |
| `POST /v4/facts` | 4 | 2 |

`add_fact`, `_ensure_memory` and `ensure_fact_memory` gained an explicit `commit`
flag, and the facts route now composes the fact, its memory, its embedding and the
audit row into one `db.transaction()`.

The remaining 2 commits per request are 1 mutation + 1 usage-metering commit.
Metering runs during authorization, *before* the transaction, and deliberately
survives a request that later fails — rolling it back would drop a real request
from the accounting record.

`test_every_write_path_is_atomic` now asserts every write route stays at that
floor, so it cannot drift back.

## Still deferred, with reasons

- **`sqlite-vec` ANN** — no prebuilt aarch64 wheels (this project's audience),
  `vec0` conflicts with additive migrations, and the trigger for adoption is
  recorded in `tests/benchmarks/search.md`.
- **Container image on real hardware** — verified on a GitHub runner (where the
  image builds and serves); still unverified on the operator's own host.
