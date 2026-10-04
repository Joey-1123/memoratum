# Concurrency benchmark

Constitution Principle IV requires a performance or concurrency claim to be backed
by a measurement **committed to the repository**, not by reasoning about the code.
This file is that measurement.

## The defect

`get_conn` held `_DB_LOCK` across its `yield`, so the lock was held for the entire
request lifetime. Throughput was one request at a time regardless of worker count
or core count.

```python
def get_conn(request: Request):
    with _DB_LOCK:
        conn = db.connect(...)
        try:
            yield conn  # <-- endpoint body runs HERE, lock still held
        finally:
            conn.close()
```

## Before

`4 x GET /v1/ping()` through the ASGI app, with 0.30 s of simulated per-request
work:

```
wall=1.48s
per-request durations: [0.57, 0.88, 1.18, 1.48]
```

Wall time equals the sum of the parts — the signature of full serialisation.

## After

The lock now covers connection setup only, and is released before the endpoint
body runs. WAL was already enabled, so readers never block; only writers contend,
and `busy_timeout` was raised from 5 s to 15 s so a contended writer waits instead
of failing.

```python
def get_conn(request: Request):
    with _DB_LOCK:
        conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()
```

## How this is verified

Timing a `TestClient` cannot distinguish genuine concurrency from a fast
serialised path — it would pass either way, and an earlier version of this test did
exactly that. Two authoritative checks replaced it:

1. **AST invariant** (`test_db_lock_is_not_held_across_the_request`): parses
   `get_conn` and asserts no `yield` node is nested inside the `with _DB_LOCK`
   block. This cannot be fooled by source formatting or comments.

2. **Direct lock probe** (`test_lock_is_released_before_the_endpoint_body_runs`):
   drives `get_conn` to its `yield` — the instant the endpoint body would begin —
   and asserts `_DB_LOCK.acquire(blocking=False)` succeeds. Deterministic, no
   timing involved.

Correctness under concurrency is covered by
`test_concurrent_writes_do_not_corrupt_or_lose` (8 parallel writers: all land, no
orphan documents, `integrity_check` ok) and
`test_busy_database_is_retried_not_failed` (a held `BEGIN IMMEDIATE` must make a
concurrent write wait, not fail).

## Ordering constraint

This work **had** to land after the single-commit-boundary change
(`4c6c708`). Removing a serialising lock while writes were still non-atomic would
expose partial-write interleavings that the lock had been masking by accident.

## Remaining ceiling

SQLite permits one writer at a time. This change removes an accidental
serialisation of *everything*; it does not make SQLite multi-writer. The genuine
ceiling is documented in `docs/OPERATIONS.md`: one active writer process against a
local volume, or the Postgres/pgvector topology for multi-writer deployments.