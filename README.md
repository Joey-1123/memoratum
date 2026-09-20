# Memoratum

*Things to be remembered.* Self-hosted context infrastructure for AI agents — a free, open-source alternative to hosted memory APIs.

Memoratum ingests documents, chat turns, and code, then serves them back as searched **chunks** (raw grounding), **memories** (extracted facts in a temporal graph), and **profiles** (always-on summaries) — all scoped by hard-isolation **container tags**.

## Quickstart

```sh
uv sync --group dev
uv run pytest
MEMORATUM_DATA_DIR=./data uv run python -m memoratum  # :6767

curl -X POST localhost:6767/v3/documents \
  -H 'Content-Type: application/json' \
  -d '{"content": "The user loves Paris.", "containerTag": "user_123"}'
curl -X POST localhost:6767/v4/search \
  -H 'Content-Type: application/json' \
  -d '{"q": "where does the user want to travel?", "containerTag": "user_123"}'
```

Or with Docker Compose (persisted volume, env from your shell):

```sh
MEMORATUM_API_KEY=... docker compose up --build
```

## Auth

No `MEMORATUM_API_KEY` set → open local-dev mode (like the reference
self-hosted servers). Set it to require Bearer auth:

- The admin key accesses every container tag.
- Scoped keys are bound to one tag: create them as admin —
  `POST /v4/keys {"containerTag": "proj-a"}` → `{key: "mm_..."}`.
- A scoped key gets 403 outside its tag; unknown/missing credentials get 401.
- Keys are stored as SHA-256 hashes; the raw value is shown once at creation.

## Dreaming (fact extraction)

Point `MEMORATUM_LLM_ENDPOINT` / `MEMORATUM_LLM_MODEL` at any OpenAI-compatible
chat endpoint (`MEMORATUM_LLM_KEY` optional). Documents accept
`"dreaming": "instant"` (per-doc call, facts carry the document id) or
`"dynamic"` (default; one call per tag bundle, tag-level facts). Contradictions
supersede old facts (validity closed, history kept) — nothing is ever deleted.

## Search

`POST /v4/search` with `q`, `containerTag`, and optional `searchMode`
(`hybrid` default, `memories`, `documents`), `limit`, `threshold`, `filters`
(AND-equality over metadata), `rerank` (recency-blended rescoring). Results
carry `memory` or `chunk` plus `similarity`.

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `MEMORATUM_DATA_DIR` | `./.memoratum-data` | SQLite file location |
| `MEMORATUM_API_KEY` | _(empty = open)_ | Admin Bearer key |
| `MEMORATUM_EMBEDDINGS_PROVIDER` | `hash` | `hash` (offline dev) or `api` |
| `MEMORATUM_EMBEDDINGS_ENDPOINT/MODEL` | — | OpenAI-compatible endpoint |
| `MEMORATUM_EMBEDDINGS_KEY` | — | Embeddings credential |
| `MEMORATUM_LLM_ENDPOINT/MODEL/KEY` | — | Chat endpoint for dreaming |

## Clients

Python SDK (`memoratum.client`), TypeScript SDK (`clients/ts`), OpenCode V2
plugin (`clients/opencode/memoratum.js`, `MEMORATUM_URL`/`MEMORATUM_TAG`), and
MCP server (`python -m memoratum.mcp`, stdio `remember`/`recall`). See
[`clients/`](clients/).

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE). If you run a modified
version on a network server, you must offer users the Corresponding Source (AGPL §13).
