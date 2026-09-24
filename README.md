# Memoratum

[![CI](https://github.com/Joey-1123/memoratum/actions/workflows/ci.yml/badge.svg)](https://github.com/Joey-1123/memoratum/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

*Things to be remembered.* Self-hosted context infrastructure for AI agents — a free, open-source alternative to hosted memory APIs.

Memoratum ingests documents, chat turns, and code, then serves them back as searched **chunks** (raw grounding), **memories** (extracted facts in a temporal graph), and **profiles** (always-on summaries) — all scoped by hard-isolation **container tags**. It speaks a stable versioned HTTP API, so existing clients switch with a one-line `baseURL` change.

## Features

- **Async ingest pipeline** — documents move `queued → done|failed` through chunking, embedding, and indexing; `customId` makes re-ingest idempotent
- **Hybrid retrieval** — cosine vector search + SQLite FTS5 fused with RRF, mandatory per-tag isolation, metadata filters, recency rerank
- **Dreaming** — LLM fact extraction into a temporal graph (`instant` per-doc or `dynamic` per-tag); contradictions supersede, history is kept, and true deletion exists (`DELETE` endpoints)
- **Auth** — admin key plus container-scoped keys with wildcard support, first-boot keygen, revocation list, uniform 404s against ID probing
- **Profiles** — per-tag fact sample + document/chunk/fact stats
- **Clients** — Python SDK, TypeScript SDK, OpenCode plugin, MCP server, and a documented Mem0-compatible route shim
- **Governance** — local audit trail and per-key usage views, plus expiring read-only document share links
- **Operations** — SQLite backup/restore helper and small-deployment HA guidance
- **Enterprise seam** — injectable OIDC identity verification with local scope/audit mapping
- **Eval harness** — LongMemEval-S, LoCoMo, and LoCoMo-MC10 local runners (see `docs/EVALUATION.md`)

## Installation

```sh
git clone https://github.com/Joey-1123/memoratum
cd memoratum
uv sync --group dev
```

Requires Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/). Or with Docker Compose (persisted volume):

```sh
MEMORATUM_API_KEY=... docker compose up --build
```

## Quick Start

```sh
uv sync --group dev
uv run pytest                                  # verify your checkout
MEMORATUM_DATA_DIR=./data uv run python -m memoratum  # :6767, prints admin key on first boot
MEMORATUM_DATA_DIR=./data uv run python -m memoratum.worker  # background jobs (ingest/dream)

curl -X POST localhost:6767/v3/documents \
  -H 'Content-Type: application/json' \
  -d '{"content": "The user loves Paris.", "containerTag": "user_123"}'
# → {"id": "...", "status": "queued", "job_id": "..."}; the worker flips it to done
curl localhost:6767/v3/documents/<id>          # poll status
curl -X POST localhost:6767/v4/search \
  -H 'Content-Type: application/json' \
  -d '{"q": "where does the user want to travel?", "containerTag": "user_123"}'
```

## Auth

- No `MEMORATUM_API_KEY` set → the first boot mints a wildcard admin key and prints it **once** on stdout. `MEMORATUM_API_KEY` pins a fixed admin key instead.
- Scoped keys are bound to one tag: `POST /v4/keys {"containerTag": "proj-a"}` (admin) → `{key: "mm_..."}`. Outside its tag → 403; unknown/missing credentials → 401; missing-or-forbidden documents read as 404.
- Revoke with `POST /v4/keys/revoke {"key": "..."}` (admin). Keys are stored as SHA-256 hashes.

## Dreaming

Point `MEMORATUM_LLM_ENDPOINT` / `MEMORATUM_LLM_MODEL` at any OpenAI-compatible chat endpoint (`MEMORATUM_LLM_KEY` optional). Per document: `"dreaming": "instant"` (own LLM call, facts carry the document id) or `"dynamic"` (default; tag docs bundled in 8000-char windows). Oversized docs are windowed, never silently dropped; re-ingested content re-dreams automatically.

## Search

`POST /v4/search` with `q`, `containerTag`, and optional `searchMode` (`hybrid` default, `memories`, `documents`), `limit` (1–100), `threshold`, `filters` (AND-equality over metadata), `rerank` (recency-blended rescoring). Hits carry `memory` or `chunk` plus `similarity`. Full reference: [`docs/API.md`](docs/API.md).

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `MEMORATUM_DATA_DIR` | `./.memoratum-data` | SQLite file location |
| `MEMORATUM_API_KEY` | _(first boot mints one)_ | Fixed admin Bearer key |
| `MEMORATUM_EMBEDDINGS_PROVIDER` | `hash` | `hash` (offline dev) or `api` |
| `MEMORATUM_EMBEDDINGS_ENDPOINT/MODEL/KEY` | — | OpenAI-compatible endpoint |
| `MEMORATUM_LLM_ENDPOINT/MODEL/KEY` | — | Chat endpoint for dreaming |

## Clients

Python SDK (`memoratum.client`), TypeScript SDK (`clients/ts`, `npm test`), OpenCode V2 plugin (`clients/opencode/memoratum.js`; `MEMORATUM_URL`/`MEMORATUM_API_KEY`/`MEMORATUM_TAG`), MCP server (`python -m memoratum.mcp`, stdio `remember`/`recall`). See [`clients/`](clients/).

## Dashboard

`dashboard/` is a Vite+React console served at `/dashboard` once built (`npm run build`
inside `dashboard/`; the server mounts `dashboard/dist` when present). Tags, interactive
2D graph (Sigma) with per-node inspector, 3D presentation mode (Three.js toggle), validity
time scrubber, command palette (`Ctrl+K`), search, graphify import, vault export, and local governance/accounting tables.
Vite dev proxy forwards `/v3`+`/v4` to `:6767`.

## Project Structure

```
src/memoratum/   → app.py (routes), db.py (SQLite/FTS5/migrations),
                   ingest.py, dreaming.py, facts.py, search.py,
                   chunking.py, embeddings.py, client.py, mcp.py,
                   config.py, eval_longmemeval.py, eval_metrics.py
tests/           → pytest suite (one file per module, red-first)
clients/         → ts/ SDK, opencode/ plugin
dashboard/       → Vite+React console (served at /dashboard)
docs/            → PARITY.md (build spec), API.md, ARCHITECTURE.md, SECURITY.md, OPERATIONS.md, OIDC.md, DOGFOOD.md
eval/            → LongMemEval-S reports (generated, committed as evidence)
```

## Development

```sh
uv run pytest -q          # full suite
uv run python scripts/check_no_telemetry.py  # privacy guard
uvx pip-audit --local     # dependency audit
uv run ruff check .       # lint
uv run ruff format .      # format
```

Workflow: feature branches, one logical change per commit (`feat:`/`fix:`/`chore:`/`docs:`), tests before code, PR with review record, merge, delete branch. Security model and accepted trade-offs: [`docs/SECURITY.md`](docs/SECURITY.md).

## Contributing

PRs welcome: keep slices small and tested, follow the existing per-module test layout, update `CHANGELOG.md` under a new `Unreleased` section, and keep `docs/` consistent with code. Client code (`src/memoratum/client.py`, `clients/`) is MIT-licensed (see `LICENSE-MIT`); everything else is AGPL-3.0.

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE). Client exceptions in [LICENSE-MIT](LICENSE-MIT). Running a modified server publicly requires offering the Corresponding Source (AGPL §13).
