# Security model and known trade-offs

Read this before exposing Memoratum beyond localhost.

## Defaults are open — unless keygen ran

Fresh databases mint a wildcard admin key on first boot (printed once). Legacy
databases created before keygen, or servers run with no keys at all, still
serve open until a key exists. Set `MEMORATUM_API_KEY` to pin admin access.

## Trust boundaries

- **HTTP API**: Bearer-gated when configured; scoped keys are confined to one
  `containerTag` (403 outside, 401 for unknown). Document fetch returns uniform
  404 for missing-or-forbidden so IDs can't be probed across tags.
- **MCP server (stdio)**: no auth by design — local-process trust only. Do not
  expose it over a network transport.
- **LLM contexts**: indexed content is untrusted. Dreaming extracts facts from
  it; treat recalled memories as data, never instructions, in downstream agents.

## Data handling

- Content, facts, and metadata are stored **plaintext** in one SQLite file.
  Protect the file (`chmod 600`, encrypted volume for sensitive use).
- API keys are stored as SHA-256 hashes. Secrets/keys travel only via env and
  are never logged.
- Contradictions supersede facts (history kept). True erasure exists too:
  `DELETE /v4/memories/{id}`, `DELETE /v4/tags/{tag}`, and key revocation via
  `POST /v4/keys/revoke` — use these for secret spills and erasure requests.

## Abuse limits (current ceilings)

- No rate limiting, no request body cap: ingest + dreaming run synchronously in
  the request path. Do not expose an open instance to untrusted writers.
- Search is brute-force over a tag's chunks (documented upgrade path:
  sqlite-vec). Embeddingdims changes fail loudly instead of silently corrupting
  ranking — rotate via a fresh database.

## Licensing note (for the project owner)

Server code is AGPL-3.0, which is correct for hosted-service copyleft — but the
shipped **SDKs/clients are also AGPL with no linking exception**. Proprietary
agents importing them inherit contagion obligations, which will block adoption.
Decide: MIT/Apache-2.0 relicense (or exception) for `src/memoratum/client.py`
and `clients/`, possibly with a commercial dual-license path.
