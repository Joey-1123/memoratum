# Operations

## Backup and restore

Memoratum uses SQLite WAL mode. Use the bundled helper for a consistent local
snapshot; it uses SQLite's backup API, verifies integrity, writes a temporary
file, and atomically renames it.

```sh
uv run python -m memoratum.backup backup \
  --source .memoratum-data/memoratum.db \
  --destination backups/memoratum-$(date +%F).db
```

Stop the server before restoring. The destination must be explicitly replaced
with `--force`; the helper removes stale WAL/SHM sidecars after the atomic swap.

```sh
uv run python -m memoratum.backup restore \
  --snapshot backups/memoratum-2026-09-24.db \
  --destination .memoratum-data/memoratum.db --force
```

Backups contain plaintext content and key hashes. Store them with the same
protection as the live database.

## Recovering stranded jobs

Background work is claimed under a lease (`MEMORATUM_JOB_LEASE_SECONDS`, default
300 s). If a worker is killed uncatchably — SIGKILL, OOM — it cannot release its
claim, so the reaper returns the job to the queue once the lease expires. This is
automatic; you normally need to do nothing.

It matters most for `purge_project`: a stranded purge job used to wedge a project
permanently, with every write returning `409` and the delete retry handing back the
same dead job id. That is no longer possible, but if you want to see the state:

```sh
uv run python scripts/recover_stuck_jobs.py --dry-run
uv run python scripts/recover_stuck_jobs.py --force
```

`--force` refuses while any worker still holds a live lease, so stop the workers
first. Projects that remain wedged are listed with the job id to cancel:

```sh
curl -s -X POST localhost:6767/v4/maintenance/reap-jobs \
  -H "Authorization: Bearer $MEMORATUM_API_KEY"   # force one reaper pass, list wedged projects
```

Cancelling a `purge_project` job resets the project's `deleting` flag, which is
what makes it writable and deletable again.

A `running` job whose lease is still valid is never cancelled and never stolen:
that would interrupt live work.

## Retention

Every table carries an explicit retention policy, stored as data in
`retention_policies` rather than in prose — so "every table has one" is a test
assertion, and a future migration that adds a table without a policy fails
`tests/test_retention.py`.

| Table | Policy | Window |
|---|---|---|
| `organizations`, `projects`, `memories`, `documents`, `facts`, `webhooks`, `api_keys`, `usage_counters`, `retention_policies` | retain indefinitely | — |
| `chunks`, `vector_points`, `project_members` | cascaded | — |
| `idempotency_keys` | timed | 24 h |
| `jobs` | timed | 30 d (terminal only) |
| `share_links` | timed | 30 d |
| `revoked_keys`, `domain_events`, `webhook_deliveries` | timed | 90 d |
| `memory_history`, `audit_events` | timed | 365 d |

Only **terminal** rows are ever eligible: `done`/`failed`/`cancelled` jobs and
`succeeded`/`dead` deliveries. A queued delivery is an outstanding obligation, not
a log line, and deleting it would drop it silently.

The automatic sweep is **opt-in**. `MEMORATUM_RETENTION_DAYS` defaults to `0`,
meaning nothing is deleted automatically until you set it — silently deleting
audit or memory-history data would be a worse failure than a file that keeps
growing. Once set, the worker sweeps at most once a day.

Preview first:

```sh
curl -s -X POST 'localhost:6767/v4/maintenance/prune?dry_run=true' \
  -H "Authorization: Bearer $MEMORATUM_API_KEY"
```

Then run it, and read the policy registry the same way:

```sh
curl -s -X POST localhost:6767/v4/maintenance/prune \
  -H "Authorization: Bearer $MEMORATUM_API_KEY"
```

`webhook_deliveries` is swept before `domain_events` because it references it;
an event is only collectable once nothing references it. The sweep ends with
`wal_checkpoint(TRUNCATE)` and `incremental_vacuum` so free pages actually return
to the filesystem.

## Small-deployment HA

The default server is a single-process SQLite deployment. SQLite permits one
writer at a time, so run exactly one active writer process against the shared
volume.

Requests are no longer serialised: the per-request database lock covers connection
setup only and is released before the endpoint body runs, so concurrent readers
proceed in parallel under WAL. See `tests/benchmarks/concurrency.md` for the
measurement. Writes still contend with each other, and a contended writer waits up
to `busy_timeout` (15s) rather than failing. For a small high-availability setup, run one active writer
against a shared local volume, put a reverse proxy in front for TLS and
request limits, and schedule the backup command above. Do not point multiple
independent server processes at the same SQLite file.

For multi-writer or multi-host deployments, select the Postgres/pgvector
backend, move the relational schema and job claims to a managed Postgres
instance, and use an external vector backend. That topology needs an explicit
migration and deployment plan; it is not enabled by merely setting a provider
name.
