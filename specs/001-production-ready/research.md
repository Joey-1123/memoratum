# Phase 0 Research: Production Readiness Hardening

**Feature**: `001-production-ready` | **Date**: 2026-10-03
**Inputs**: `spec.md`, `.specify/memory/constitution.md` (v1.0.0), plus runnable probes
recorded in this document.

Every decision below is backed by either a runnable probe (results quoted) or a
measured baseline. Where a decision rests on a probe, the probe is named so a
reviewer can re-run it. This is required by Constitution Principle IV.

---

## Measured baselines

These were measured on the current `main` at `2dddbb4`, not estimated.

### Test coverage (`pytest --cov=src/memoratum`)

```
TOTAL 79.5% | 5029 statements | 1031 missed | 241 passed in 92.69s
```

Per-file, worst first:

| Module | Stmts | Missed | Coverage | Note |
|---|---:|---:|---:|---|
| `mcp.py` | 55 | 55 | **0%** | entire MCP server untested |
| `worker.py` | 199 | 81 | **59%** | **contains the P0 stranded-job defect** |
| `eval_locomo.py` | 119 | 47 | 61% | eval harness |
| `eval_longmemeval.py` | 122 | 45 | 63% | eval harness |
| `backup.py` | 82 | 24 | 71% | contains the P1 key-loss-at-delivery gap |
| `vectorstore.py` | 450 | 99 | 78% | |
| `webhooks.py` | 340 | 69 | 80% | **contains the P0 SSRF defect** |
| `app.py` | 1528 | 276 | 82% | |
| `db.py` | 520 | 84 | 84% | 21 internal `commit()` sites |

**Finding that shaped the plan**: coverage is inversely correlated with defect
severity. Both P0 defects live in modules below the project mean, and the single
worst-covered non-trivial module is the worker — the exact component whose
failure mode permanently destroys a tenant. A flat percentage target would not
have surfaced this; per-module floors do.

### Migration transaction safety — probe `probe_migration.py`

```python
c.executescript(
    "ALTER TABLE t ADD COLUMN a INTEGER;\nALTER TABLE t ADD COLUMN a INTEGER;"
)  # second stmt fails
```

```
migration raised: OperationalError duplicate column name: a
column 'a' persisted despite failure: True
RETRY HARD-FAILS -> duplicate column name: a
=> a partially applied migration permanently blocks startup
```

`db.connect()` applies all 27 migrations with `db.executescript()`, which issues
an implicit `COMMIT` before running and auto-commits each statement. There is no
transaction boundary and no post-apply verification.

### SSRF pinning feasibility — probe `probe_pinning.py`

A local TLS server was started with a self-signed certificate for `localhost`,
and a client subclassed `HTTPSConnection` to dial a **pinned IP** while passing
`server_hostname=self.host`:

```
pinned IP + server_hostname='localhost'    -> 200 (expected 200)
pinned IP + server_hostname='evil.example' -> rejected: Hostname mismatch,
                                              certificate is not valid for 'evil.example'
```

**Conclusion**: the socket address and the certificate-validation name are fully
independent. Pinning the IP does not weaken TLS.

---

## D1 — Closing the SSRF window by pinning the resolved address

### Two distinct defects, not one

**Defect 1a — DNS rebinding (TOCTOU).** `webhooks.py:620` validates, then
`webhooks.py:649` opens. `urllib` re-resolves, so the second lookup is
independent of the check. Those 29 lines *are* the vulnerability.

**Defect 1b — an allowlist that allows CGNAT.** Independent of rebinding and live
today. `_is_public_ip` is a six-way *disjunction of negatives*. Verified against
the real function:

```
100.64.1.1             allowed=True  is_global=False is_private=False
100.127.255.254        allowed=True  is_global=False is_private=False
192.88.99.1            allowed=True  is_global=True  is_private=False
8.8.8.8                allowed=True  is_global=True  is_private=False
127.0.0.1              allowed=False
169.254.169.254        allowed=False
::1                    allowed=False
::ffff:127.0.0.1       allowed=False
fd00::1                allowed=False
fe80::1                allowed=False
```

`100.64.0.0/10` is CGNAT shared address space — Tailscale, and cloud/container
internals — and `IPv4Address.is_private` is **documented `False`** for it. Only
`is_global` is `False`. So a webhook pointed at `http://100.64.x.x` reaches
internal infrastructure today, with no DNS trickery required.

**Fix for 1b.** Replace the disjunction with a positive `is_global` allowlist.
`is_global` is already `False` for every range the current code blocks, so this is
strictly tighter and fixes CGNAT, `192.88.99.0/24`, `2001:20::/28` (ORCHIDv2), and
`2620:4f:8000::/48` (AS112) in one change.

**Fix for 1a.** Keep the hostname in the URL and pin only the socket address.
Verified working design, subclassing `HTTPSConnection` and overriding
`_create_connection` (an *instance* attribute assigned by
`HTTPConnection.__init__`, not a method — see the trap below):

```python
class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, pinned=(), **kwargs):
        self._pinned = list(pinned)
        super().__init__(host, **kwargs)
        # MUST be an instance attribute; a method here is silently shadowed.
        self._create_connection = lambda address, timeout, source_address: _connect_pinned(
            self._pinned, timeout, source_address
        )


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(
            lambda host, **kw: PinnedHTTPSConnection(host, pinned=self._pinned, **kw),
            req,
            context=self._context,
        )  # omitting context -> system trust store
```

Four non-obvious requirements, each a way the obvious implementation fails:

1. **Override `_create_connection`, not `connect()`.** `HTTPConnection.__init__`
   assigns `self._create_connection = socket.create_connection` as an *instance*
   attribute, so a same-named method on a subclass is silently shadowed and DNS
   still happens. Overriding `_create_connection` also preserves the
   `sys.audit("http.client.connect")` hook, `TCP_NODELAY`, and proxy tunnelling,
   all of which a hand-rolled `connect()` drops.
2. **Keep the hostname in the URL.** Then `Host:` is derived correctly by
   `AbstractHTTPHandler.do_request_` with no code from us, SNI is
   `self.host`, and the certificate is validated against the hostname. Rewriting
   the netloc to the IP — the shape `requests` and `httpx` require — produces
   `Host: 127.0.0.1:8443` and **no SNI**, trading SSRF for a TLS identity bypass.
3. **Pass `context=self._context`.** Omitting it silently falls back to
   `http.client._create_https_context()` — the system trust store — discarding any
   configured context. Fails closed by default, but breaks private-CA pinning.
4. **Never set `check_hostname = False` to "fix" a mismatch.** On 3.12+ that
   assignment does not raise on a client context; it leaves `CERT_REQUIRED`, so
   you get chain validation without identity validation and no warning.

**IPv6.** `webhooks.py:188` does `str(sockaddr[0])` and **discards
`flowinfo`/`scope_id`**. Once validation moves to "connect to a sockaddr", the
whole 4-tuple must be carried. IPv4-mapped addresses (`::ffff:127.0.0.1`) are
currently blocked correctly on 3.11/3.12 because `IPv6Address.is_private`
delegates to `.ipv4_mapped` — verified `False`. That is stdlib implementation
behaviour and an attacker controls the AAAA record, so add an explicit
v4-in-v6 unwrap loop (`ipv4_mapped`, NAT64 `64:ff9b::/96`, 6to4 `2002::/16`)
rather than relying on it.

**Retry policy.** Try every validated address in turn, but **retry only on
transport errors and fail fast on TLS errors** — a certificate failure is either
an attack signal or a broken endpoint, and retrying it costs N× handshake time
against an adversary. Every attempt keeps `server_hostname=self.host`, so retrying
can never degrade into "accept any address presenting *a* certificate".

**The assertion that actually kills this bug.** A regression test asserting
`getaddrinfo` is called **exactly once per delivery**, and zero times during
connect. Measured on the working implementation:
`getaddrinfo during connect = 0`. That invariant fails loudly if anyone
reintroduces the URL-rewrite shortcut.

**Consequence for docs.** `docs/SECURITY.md:21` claims endpoints are "validated at
creation and delivery". That overstates the guarantee and is a defect under FR-008
regardless of whether the code is fixed.

**Alternatives considered.**
- *Re-validate immediately before connecting* — rejected, identical window.
- *`httpx` `sni_hostname` extension* — genuinely first-class and documented, but
  still needs the URL rewrite plus a manual `Host` header: more moving parts than
  the subclass, for the same result, plus a runtime dependency.
- *`requests` + custom `HTTPAdapter`* — rejected: depends on a urllib3-v2-only
  `server_hostname` kwarg, adds two runtime dependencies, and reintroduces the
  netloc→`Host` rewrite hazard.
- *`safehttpx`* — rejected: small unmaintained wrapper, unfit for a security
  control.

---

## D2 — Local metrics without adding a dependency

**Decision.** Emit Prometheus text exposition format from a stdlib-only module,
**served by FastAPI** — not by a second `http.server` listener. New endpoints:
`GET /metrics` (scrape), `GET /health/live`, `GET /health/ready`.

**Rationale.** Metrics must be scrapeable by tooling the operator already has,
without the project taking on a client library.

**Serve it through the existing FastAPI app.** A second `http.server` listener
would be a second unauthenticated, un-rate-limited attack surface with divergent
TLS and auth handling, for zero benefit. Implement the *format* in stdlib; serve it
as a FastAPI `Response`. Set `Content-Type: text/plain; version=0.0.4; charset=utf-8`
**explicitly** — Starlette's `PlainTextResponse` omits the `version` parameter.

**Local scrape is not telemetry.** The project has already ruled on this:
`docs/AUDIT_METERING.md` states these records "never leave the process and no
network client is used". `/metrics` is an inbound pull server — zero outbound
connections, zero DNS, zero exfiltration. The constitution constrains **egress**,
and `check_no_telemetry.py` regexes for vendor SDK names that a local route does
not match, so the guard passes unmodified. This should be written into
`docs/AUDIT_METERING.md` rather than left as a verbal argument.

**Histograms yes, summaries no.** `histogram_quantile()` over bucket *rates* is the
only correct way to compute p99 across a scrape window. Summary quantiles require
`rate()` over a quantile series, which is statistically wrong. Cost is trivial:
~11 buckets × ~3 label sets ≈ 33 series. Emit `+Inf` literally. Define buckets as
explicit `(le_string, le_float)` pairs, because `str(1e-05)` renders as `1e-05`
and the emitted text must be byte-stable across runs.

**Label cardinality — the real risk is tenant scope, not request count.**

- **Route templates only.** Label with the registered path after `call_next`, never
  the concrete path. `/v4/memories/{id}` as a label value is the fastest route to
  unbounded cardinality. Unmatched routes use `__unmatched__`.
- **Never `container_tag`, `org_id`, or `project_id` in labels.** Unbounded by
  construction, and they multiply by bucket count. Per-tenant accounting already
  exists at `/v4/usage`; business metrics stay out of the metrics endpoint. This
  is also what keeps the privacy story clean.
- Never label on error strings. `method`, `status`, `route`, `kind` are bounded
  enums.
- Exclude `/metrics` from its own counters, matching the existing `/health`
  exemption in the rate-limit middleware — otherwise the scrape inflates the series
  it reports.

**Thread safety.** All mutation under a `threading.Lock`. Uvicorn runs sync
endpoints in a threadpool and async ones on the event loop, so an unguarded
`dict[int, int]` increment is a genuine data race, not a theoretical one.

**Escaping — the asymmetry is the bug.** Label **values** escape `\`, `"`, and
newline. `# HELP` **text** escapes `\` and newline but **must not** escape `"`. A
probe (`probe_metrics.py`) confirmed the naive version produces an unbalanced-quote
sample line that a scraper silently drops:

```
UNBALANCED QUOTES: weird"label\with\escapes{note="line1\nline2",path="C:\\tmp\\x"} 1
```

That is exactly the class of defect that yields an endpoint which appears to work
and reports nothing. Metric names are not escapable and must be validated against
`[a-zA-Z_:][a-zA-Z0-9_:]*`; label names against `[a-zA-Z_][a-zA-Z0-9_]*` — colons
are legal in metric names but not in label names. `# TYPE` is emitted even though
optional in 0.0.4, because omitting it leaves the series untyped and unrateable.

**Queue metrics need no cache.** Depth and age are one indexed aggregate per scrape
against `idx_jobs_status(status, created_at)` — `GROUP BY status` plus
`MIN(created_at)` — index-bound, no table scan.

**Alternatives considered.**
- *`prometheus_client`* — rejected: new runtime dependency for a single-operator
  self-hosted audience.
- *JSON stats endpoint* — rejected: not scrapeable without a custom exporter,
  which defeats the purpose.
- *Summaries* — rejected: quantiles are not aggregatable, see above.
- *`http.server` second listener* — rejected: new unauthenticated surface.

---

## D3 — Coverage gate: measure-and-hold

**Decision.** Add `pytest-cov` as a **dev-only** dependency, enable
`branch = true`, and gate at `--cov-fail-under=74` — that is
`floor(measured) - 1`, absorbing run-to-run and Python-version noise so an
unrelated PR can never fail on rounding. The exact measured baseline is recorded
in this document and in `pyproject.toml` as a comment.

**Measured baseline (branch-aware), `main` at `2dddbb4`:**

```
statements  3998 / 5029  = 75.5%
branches    1246 / 1916
combined percent_covered = 75.51
241 passed in 81.76s
```

**Why branch coverage, not statement coverage.** It costs roughly 5–15 points
here and penalises exactly the untested error paths where this codebase's real risk
lives — webhook delivery, the SSRF guard, job recovery. It is also structurally
harder to pad: a statement can be executed by an import, a branch cannot.

**Configuration:**

```toml
[tool.coverage.run]
branch = true
source_pkgs = ["memoratum"]      # excludes tests/ and is layout-agnostic
omit = ["*/__main__.py"]          # 671-byte CLI shim. Nothing else. Ever.

[tool.coverage.report]
fail_under = 74
show_missing = true
skip_covered = false
precision = 1
```

`addopts = "--cov --cov-report=term-missing"` means the existing
`uv run pytest -q` in `ci.yml` picks this up with **no CI change required** for the
gate itself.

**Why not a substantive number on day one.** Goodhart's law is the default here,
not the exception. Once `--cov-fail-under=85` blocks a merge, the cheapest green
path is a `def test_x(): pass` per uncovered module (which executes all
module-level statements and moves the number a long way) or `# pragma: no cover`
on whatever is inconvenient. Setting a gate above today's value applies that
pressure *immediately*, against 11k lines nobody wrote tests for, with nowhere else
to go. It also inverts the diagnostic: coverage is informative only when you are
near or below it. And it makes the honest test the *expensive* path — if the gate
is already failing, writing the real regression test costs more than adding trivia,
which is precisely backwards.

**Ratchet mechanics — the part that actually matters.**

1. This PR sets the floor from measurement. No predicted number anywhere.
2. **Raise only in the same PR that adds the tests**, to `new_measurement - 1`.
   Never by calendar, never by quota — raising on a schedule decouples the number
   from the work and invites padding in a batch.
3. **Lowering requires a constitution amendment** plus a CI step that fails when
   `fail_under` in `pyproject.toml` is below its value in `HEAD~1`. The config must
   not be the easy lever. This mirrors the existing `check_no_telemetry.py` guard.
4. Seed per-module floors from the measured worst-first table so a fix cannot
   silently reduce coverage where the defects live: `worker.py` 59%, `mcp.py` 0%,
   `webhooks.py` (must not regress).

**Anti-gaming controls, enforced mechanically rather than in prose:**

- `omit` is limited to `*/__main__.py`. `app.py` (1528 statements) and `eval_*.py`
  stay measured even though excluding them would be the easy win.
- A CI grep fails if the count of `pragma: no cover` **increases**.
- Always `term-missing`, never `skip_covered`. "61 uncovered branches, all in
  webhook delivery" stays auditable; "72%" is trivially gameable.
- No coverage badge in the README — badges read as targets to contributors.
- Coverage is documented as a **floor, not evidence**, in the same sentence where
  the number appears.

**Rejected: mutation testing as a gate** (`mutmut`, `cosmic-ray`) — heavy new dev
dependencies. The compensating control already exists and is free: Principle IV
requires every fix to ship a regression test that fails when the fix is reverted.
That *is* mutation testing, scoped to where it pays.

---

## D4 — Search: the dominant cost is an unimplemented cache

**Decision.** Fix the persisted-embedding path first, then add an FTS5 candidate
prefilter. Defer `sqlite-vec` behind an explicit, measurable trigger.

**The actual defect.** `chunks.embedding BLOB` exists (`db.py:39`) and is
**written at ingest** — `ingest.py:48` calls
`db.add_chunks(conn, doc_id, texts, [_pack(v) for v in vecs])`. `search.py:184`
even SELECTs `c.embedding`. And then it throws the column away:

```
$ grep -n '\["embedding"\]' src/memoratum/search.py
265:  f["embedding"] = blob
269:  vec_items.append((fact_keys[f["id"]], _unpack(bytes(f["embedding"]))))
```

Both uses are **facts**, not chunks. `search.py:251` re-embeds every chunk text on
every query instead. The facts leg (`search.py:257-269`) already does it correctly
— compute if absent, **persist** via `UPDATE facts SET embedding = ?`, reuse. The
chunks leg was simply never given the same treatment.

At the embedder's `batch_size = 64`, a 10,000-chunk container costs **157 embedding
API calls per search query** against a remote provider. Fixing this removes the
dominant cost with zero new dependencies, zero new tables, and no ANN.

**Measured, 768-dim, on the current code path:**

| Path | 1,000 rows | 10,000 rows | 50,000 rows |
|---|---:|---:|---:|
| `SQLiteVectorStore.query` (current) | 0.33 s | **3.66 s** | ~16 s |
| + reuse stored embedding, preread `(id, vector)` only | ~0.11 s | ~1.0 s | ~5 s |
| + FTS5 prefilter (k=200) then cosine | ~0.08 s | **0.079 s** | ~0.09 s (flat) |

**~46x at k=200.** The prefilter's dominant term is the constant-k scoring loop, so
latency flattens as the corpus grows while the FTS side stays index-bound.

**The prefilter.** The project already computes an FTS5 leg via `db.keyword_search`
(BM25-ranked, external-content `chunks_fts`). Lift its top-N ids
(`N = max(limit * 20, 200)`) and score **only those** against the query vector,
then fuse with the existing RRF machinery (`_RRF_K = 60`) already present at
`search.py:299-303`. No new fusion code — the vector leg's input set is simply
bounded. Honest cost: purely-semantic matches with zero lexical overlap fall
outside the candidate set. Mitigations already present: `expand_query()` generates
lexical variants, `k` is a tunable dial, and FTS-leg hits enter fusion
independently. Re-tune `k` against the eval harness, which is the correct arbiter.

**Free wins to bundle with the fix** (all measured):
- Store **pre-normalized** blobs so scoring is one dot product (~185 → ~105 µs/row).
- Read via `array('f')`/`memoryview` instead of `list(struct.unpack(...))` —
  ~65 µs/row of pure waste today.
- Narrow the SELECT to `(id, vector)`; fetch `text`/`metadata` only for the ≤10
  survivors. `json.loads` currently runs per row at ~7 µs.
- Build `VectorHit`s after top-k, not `VectorRecord`s before.

**Why `sqlite-vec` is deferred.** It is a loadable C extension (works with stdlib
`sqlite3`; no Python-version constraint). But: prebuilt **Linux aarch64** binaries
are the gap, i.e. exactly the Raspberry Pi / ARM homelab self-hoster who is this
project's audience; `enable_load_extension` is compiled out on some builds,
forcing a second code path; `vec0` is a virtual table so schema changes need
drop-and-rebuild, conflicting with the additive `ALTER TABLE` migration list;
current blobs are **un-normalized** `struct.pack` output, needing a full data
migration; and ANN's value proposition starts around 10M vectors — roughly three
orders of magnitude above a realistic single-operator corpus.

**ANN trigger (record it now so it is measurable later).** Adopt `sqlite-vec` when
*any* of: a container exceeds ~50,000 vectors **and** the FTS prefilter measurably
misses relevant results; or p95 latency exceeds 250 ms *after* all the above; or
metadata filters become selective enough that the lexical prefilter cannot narrow.

**Alternatives considered.**
- *Brute-force cosine in SQL via a registered Python UDF* — rejected: SQLite has
  no vector type, so the math still runs in Python at the same per-row cost. Only
  a constant-factor and round-trip win; strictly dominated by the changes above.
- *`faiss` / `hnswlib`* — rejected: heavyweight native dependencies for a
  premature problem.

The `VectorStore` Protocol already makes a future swap configuration-only. That is
what Principle V bought; there is no need to spend it now.

---

## D5 — Job leases, heartbeat, and a reaper

**Decision.** Add `lease_expires_at` and `heartbeat_at` to `jobs`. `claim()`
wraps its select-then-update in `BEGIN IMMEDIATE`, sets the lease, and increments
attempts. A new `heartbeat()` extends the lease; the `bulk_memories` loop
piggybacks a heartbeat onto its existing per-item `update_result` so long jobs
stay live at no extra cost. `reap_stale()` returns expired-lease `running` jobs to
`queued` and is called once per worker iteration. `cancel()` accepts a `running`
job **only** when its lease has expired.

**Rationale.** This is the direct fix for the confirmed P0: a SIGKILLed worker
leaves a job `running` forever, a fresh `claim()` returns `None`, and `cancel()`
returns `False`, so a `purge_project` job wedges a project permanently — writes
return 409, the delete retry returns the same dead job id.

The lease must be a timeout and not a heartbeat-only scheme because the failure
mode is an *uncatchable* kill, where no heartbeat can ever be sent.

**Invariant that must survive.** `tests/test_security_regressions.py:171` sets
`status='running'` by raw SQL and asserts the job is never reclaimed. Under a
lease that job has `lease_expires_at = 0` and is correctly reaped, so **that test
must be updated to set a future lease**. The invariant it protects — never steal
*live* work — is legitimate and is preserved; only the test's fixture needs to
model a live lease rather than an unlabelled running job.

**Defaults.** Lease 300 s, heartbeat on entry to dispatch, reaper every worker
iteration, `MEMORATUM_JOB_LEASE_SECONDS` to override.

---

## D6 — Retention policy for all 19 tables

**Decision.** Assign every table an explicit policy class — retain-indefinitely,
cascaded, or timed — and implement the timed class in a `retention.py` sweep run
daily by the worker plus an admin-only manual trigger.

**Rationale.** Measured: `memory_history` and `share_links` have **zero** `DELETE`
statements in the entire codebase, and `jobs`, `domain_events`, and
`webhook_deliveries` are only removed by an explicit project purge. Nothing bounds
growth. FR-006 requires a policy for every table, which makes the enumeration
itself the deliverable — an unclassified table is a failure.

**Alternatives considered.**
- *`VACUUM` on a schedule only* — rejected: it reclaims file space but does not
  bound row growth, which is the actual problem.
- *Operator-run `DELETE`* — rejected: retention that depends on someone noticing
  is not a policy.

---

## D7 — Transaction-wrapped, verified migrations

**Decision.** Replace `db.executescript()` with an explicit transaction per
migration and a post-apply verification step. Migration 28 adds the job lease
columns and is itself the first migration shipped under the new regime.

**Rationale.** The probe shows the current regime fails open: partial DDL
persists and the retry hard-fails at startup, turning a one-off migration fault
into a permanent outage. With 27 migrations already in the list, the exposure is
proportional.

**Complication to design around.** Some existing migrations contain
`PRAGMA foreign_keys=OFF`, which only takes effect outside a transaction. Those
migrations cannot simply be wrapped; they need an explicit
enable/disable-around-transaction form, and the reordering risk must be called out
in `tasks.md` rather than discovered mid-migration.

---

## D8 — Narrowing the request-scoped database lock

**Decision.** Hold the database lock only for connection setup, not across the
request. WAL is already enabled and each request already receives its own
connection, so concurrent readers are safe. Writers are serialised separately,
with `busy_timeout` raised and bounded writer concurrency. Commit the measured
before/after latency as a benchmark in the repository.

**Rationale.** `_DB_LOCK` is held across the `yield` in `get_conn`, i.e. for the
entire request lifetime, so throughput is one request at a time regardless of
cores or worker count. Measured: 4 concurrent `GET /v1/ping/` took 1.48 s wall with
per-request durations of 0.57/0.88/1.18/1.48 s — textbook serialisation.

`docs/OPERATIONS.md` currently says the server "serializes writes inside one
process", which understates the problem: it serialises *everything*.

**Ordering constraint.** This must land **after** D9/commit-boundary work.
Removing the serialising lock while writes are still non-atomic would expose
partial-write interleavings that the lock was accidentally masking.

---

## D9 — One commit boundary per logical operation

**Decision.** Add a `db.transaction()` context manager (`BEGIN IMMEDIATE` /
commit / rollback). Remove the 21 internal `commit()` calls in `db.py`, replacing
them with an explicit `commit: bool = True` parameter so existing callers keep
working, then convert composite routes to a single transaction.

**Rationale.** Measured: one `POST /v3/memories/add/` performs **5 separate
COMMIT statements**. A crash injected between the document commit and the job
enqueue leaves `documents=1, memories=1, jobs=0` — content durably stored and
searchable, with the ingest job that would ever process it simply absent, and the
document stuck at `queued` forever. The transactional-outbox design is undermined
because commits happen inside helpers rather than at the transaction boundary.

**Blast radius, measured.** 63 `commit()` call sites across 9 modules:
`app.py` 13, `db.py` 21, `webhooks.py` 8, `jobs.py` 7, `vectorstore.py` 5,
`facts.py` 3, `worker.py` 3, `backup.py` 1, `dreaming.py` 1, `search.py` 1.
`ensure_fact_memory`, `update_memory`, and `soft_delete_memory` are called from
both routes and the worker, so this needs a caller audit rather than a mechanical
edit.

**Rejected alternative.** *Keep internal commits, add a compensating repair job.*
Rejected: it leaves the durability claim false and adds a second source of truth.

---

## D10 — Strict request models (promoted from the review)

**Decision.** Introduce a `StrictModel` base with
`model_config = ConfigDict(extra="forbid")` for all request models, add
`org_id`/`project_id` to `Mem0AddIn` and `Mem0SearchIn`, and provide an
operator opt-out via `MEMORATUM_LENIENT_COMPAT=true`.

**Rationale.** Verified: `Mem0AddIn` has no `project_id` field and no model in the
codebase sets `extra`, so Pydantic silently discards it. An admin explicitly
targeting a project receives `200 OK` and the data lands in **global NULL scope**:

```
caller asked for : project-eCf-kPsjkItnTfb-
actually stored in project_id: None
```

The same failure generalises to typos on native write routes: `projct_id` on
`POST /v4/facts` returns `201` and writes to `project_id = None`; `projectId` on
`POST /v3/documents` likewise. Tenant-isolation mistakes fail **open and
silently**, which is the worst possible failure mode for a multi-tenant store.

**Why this is in scope.** The spec's Assumptions state that newly found gaps join
the same acceptance-criteria list, and Constitution Principle II requires scope to
be enforced rather than inferred from whatever the caller spelled correctly.

**Alternatives considered.**
- *Strict everywhere, no escape hatch* — rejected: breaks third-party
  Mem0-compatible clients that send additional fields, with no operator recourse.
- *Allowlist of known Mem0 client fields* — rejected: still lets a typo'd scope
  field through unscoped on exactly the routes where it matters most.
- *Leave as-is* — rejected: this is a live scope-escalation path.

---

## D11 — Structured logging

**Decision.** Use the standard library `logging` with a JSON formatter, emitted
to stdout. Scope-safe identifiers only (container tag, project id, job id); never
content, secrets, API keys, or raw query strings.

**Rationale.** Measured: `grep -rn "logging\|logger"` over `src/memoratum/`
returns **nothing**. There is no logging of any kind today, so FR-005 cannot be
satisfied by configuring what exists. `logging` is stdlib and therefore costs
nothing under Principle V. JSON to stdout is what a container log driver expects
and requires no agent to be installed.

**Rejected alternative.** *A third-party structured-logging package* — rejected
under Principle V for identical reasons to D2.

---

## Resolved clarifications

Every `NEEDS CLARIFICATION` from the spec and from Planning assumptions is now
closed:

| Question | Resolution | Section |
|---|---|---|
| Coverage threshold value | measure-and-hold, branch-aware, gate 74, baseline 75.5% recorded | D3 |
| Metrics endpoint shape | Prometheus text via FastAPI, stdlib format, counters + gauges + histograms | D2 |
| Migration safety | transaction-wrapped + post-apply verified | D7 |
| ANN or not | deferred; reuse the persisted `chunks.embedding` + FTS5 prefilter (46x measured); `sqlite-vec` on a recorded trigger | D4 |
| SSRF pinning feasibility | proven by probe; TLS validation preserved; two defects, not one | D1 |
| Log format | stdlib `logging`, JSON to stdout | D11 |
| Request-model strictness | `extra="forbid"` + `MEMORATUM_LENIENT_COMPAT` escape hatch | D10 |
| Lock contention | WAL readers concurrent, writers serialised, benchmark committed | D8 |

No unresolved clarifications remain.