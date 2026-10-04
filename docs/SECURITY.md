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
- Search reuses the embeddings ingest already persisted, so a query does not
  re-embed the corpus (that cost 157 provider calls per search at 10k chunks).
  Above ~512 chunk candidates the FTS5 leg bounds the vector candidate set;
  cosine is still computed directly within that bounded set, so scoring is
  brute-force rather than approximate. `sqlite-vec` ANN is deliberately deferred
  (no prebuilt aarch64 wheels) with its adoption trigger recorded in
  `tests/benchmarks/search.md`. Bounding was measured on LongMemEval-S and
  *improved* recall (partial-R@10 0.78 → 0.94). Embedding-dims changes fail
  loudly instead of silently corrupting ranking — rotate via a fresh database.

## Licensing

The split is deliberate and already in place:

| Scope | Licence |
|---|---|
| `src/memoratum/client.py` (Python SDK) | MIT (`LICENSE-MIT`) |
| `clients/` (TypeScript SDK, OpenCode plugin) | MIT (`LICENSE-MIT`) |
| Everything else, including the server and the MCP server | AGPL-3.0-or-later (`LICENSE`) |

`LICENSE-MIT` states this scope explicitly, `README.md` repeats it, and the
published packages declare it (`clients/ts/package.json` is `"license": "MIT"`).

Two things remain genuinely open, and they are *owner decisions* rather than
defects:

- **No commercial dual-license path.** Vendors who cannot ship AGPL server code
  have no option today. Adding one is a policy and pricing decision.
- **The dashboard is AGPL**, because it is served by the server rather than
  embedded in a client.

An earlier revision of this file claimed the SDKs were AGPL with no linking
exception and needed relicensing. That was stale: `LICENSE-MIT` predated it and
the claim was never reconciled. `tests/test_docs_conformance.py` now asserts the
licence statements agree, so it cannot drift again.
