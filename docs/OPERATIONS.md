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

## Small-deployment HA

The default server is a single-process SQLite deployment and serializes writes
inside one process. For a small high-availability setup, run one active writer
against a shared local volume, put a reverse proxy in front for TLS and
request limits, and schedule the backup command above. Do not point multiple
independent server processes at the same SQLite file.

For multi-writer or multi-host deployments, select the Postgres/pgvector
backend, move the relational schema and job claims to a managed Postgres
instance, and use an external vector backend. That topology needs an explicit
migration and deployment plan; it is not enabled by merely setting a provider
name.
