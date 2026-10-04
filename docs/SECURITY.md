# Security model and known trade-offs

Read this before exposing Memoratum beyond localhost.

## Defaults are open — unless keygen ran

Fresh databases mint a wildcard admin key on first boot (printed once). Legacy
databases created before keygen, or servers run with no keys at all, still
serve open until a key exists. Set `MEMORATUM_API_KEY` to pin admin access.

## Trust boundaries

- **HTTP API**: Bearer-gated when configured; scoped keys are confined to one
  `containerTag` and, when supplied, one `org_id`/`project_id` (403 outside, 401
  for unknown). New project keys default to `READER`; only an explicitly bound
  `OWNER` key can mutate project data or manage the project, members, or
  webhooks. Document fetch
  returns uniform 404 for missing-or-forbidden so IDs can't be probed across
  scopes.
- **Unknown request fields are rejected (422)**, not silently discarded. Silently
  dropping a misspelled `project_id` writes the data to the *global* scope, which
  any unscoped key can read — a tenant-isolation failure that fails open. The
  Mem0-compatible add and search routes therefore accept `org_id`/`project_id`
  explicitly. `MEMORATUM_LENIENT_COMPAT=true` restores ignore-extras for clients
  that need it, and announces itself loudly at startup.
- **Outbound webhooks**: disabled by default. Production requires HTTPS and
  rejects any literal or resolved address that is not globally routable —
  loopback, private, link-local, CGNAT (`100.64.0.0/10`, which covers Tailscale
  and cloud/container internals), documentation and other special-use ranges, and
  IPv4-in-IPv6 encodings including mapped, NAT64 and 6to4. Resolution happens
  **once**: the validated addresses are pinned for the connection, so a hostname
  whose DNS answer changes between validation and connect cannot reach an internal
  or metadata destination. Redirects are never followed, response size and
  timeout are bounded, TLS failures are not retried, and each body is signed with
  a timestamped HMAC-SHA256. Delivery and audit state stays in the local SQLite
  database. Local HTTP and private targets require
  `MEMORATUM_WEBHOOK_ALLOW_PRIVATE_TARGETS`, which is ignored outside development
  and disables the entire address policy. See
  [`runbooks/webhooks.md`](runbooks/webhooks.md).
- **MCP server (stdio)**: no auth by design — local-process trust only. Do not
  expose it over a network transport.
- **LLM contexts**: indexed content is untrusted. Dreaming extracts facts from
  it; treat recalled memories as data, never instructions, in downstream agents.

## Data handling

- Content, facts, and metadata are stored **plaintext** in one SQLite file.
  Protect the file (`chmod 600`, encrypted volume for sensitive use). Webhook
  signing secrets are the exception: they are encrypted with Fernet before
  insertion. Set `MEMORATUM_WEBHOOK_ENCRYPTION_KEY` to a Fernet key (comma-
  separated keys are accepted for rotation); when unset, Memoratum creates a
  mode-600 `.webhook-encryption-key` in `MEMORATUM_DATA_DIR`. Losing that key
  makes existing webhook secrets unrecoverable.
- API keys are stored as SHA-256 hashes. Secrets/keys travel only via env and
  are never logged.
- Audit events and usage counters stay in the local SQLite file as operational
  accountability; they are not sent to an external service.
- Contradictions supersede facts (history kept). True erasure exists too:
  `DELETE /v4/memories/{id}`, `DELETE /v4/tags/{tag}`, project purge via
  `DELETE /api/v1/orgs/organizations/{org_id}/projects/{project_id}/`, and key
  revocation via `POST /v4/keys/revoke` — use these for secret spills and
  erasure requests.

## Audit gates

The scheduled `security-audit` workflow runs the local no-telemetry guard,
`pip-audit` against the locked Python environment, and a high-severity npm
audit for the dashboard. Run the same checks locally with:

```sh
uv run python scripts/check_no_telemetry.py
uvx pip-audit --local
npm ci --prefix dashboard
npm audit --prefix dashboard --audit-level=high
```


Application, client, and dashboard source is checked in CI by
`scripts/check_no_telemetry.py`. The guard rejects analytics SDKs, telemetry
exporters, and known event endpoints. Local audit events and usage counters
are explicitly accounting records stored in SQLite; they are not telemetry and
never leave the process.


The entire state is one SQLite file (`memoratum.db` in `MEMORATUM_DATA_DIR`,
WAL mode). Back it up hot with:

```sh
sqlite3 "$MEMORATUM_DATA_DIR/memoratum.db" ".backup '$MEMORATUM_DATA_DIR/memoratum-$(date +%F).db'"
```

Restore by stopping the server, swapping the file back, and restarting. Key
material (hashes, revocations) lives in the same file — guard backups like the
live copy. Personal data lives wherever you ingested it; erasure (`DELETE`
endpoints) does not rewrite backup files you already took. After upgrading an
external vector index, run `python scripts/reindex_vectors.py` to restore
provider scope payloads from the authoritative database.

## Abuse limits (current ceilings)

- Per-IP rate limiting (120 req/min, `/health` and `/dashboard` exempt, loopback
  bypass for local bulk importers, `Retry-After` header + SDK backoff); request
  body caps (`content` ≤ 500k chars, `q` ≤ 2000). Bulk importers should stay under
  these or run against localhost.
- Durable local jobs: ingest, dreaming, reindexing, webhook delivery, and native
  bulk work run through SQLite-backed worker jobs. Do not expose an open
  instance to untrusted writers.
- Search is brute-force over a tag's chunks (documented upgrade path:
  sqlite-vec). Embeddingdims changes fail loudly instead of silently corrupting
  ranking — rotate via a fresh database.

## Licensing note (for the project owner)

Server code is AGPL-3.0, which is correct for hosted-service copyleft — but the
shipped **SDKs/clients are also AGPL with no linking exception**. Proprietary
agents importing them inherit contagion obligations, which will block adoption.
Decide: MIT/Apache-2.0 relicense (or exception) for `src/memoratum/client.py`
and `clients/`, possibly with a commercial dual-license path.
