# Memoratum

*Things to be remembered.* Self-hosted context infrastructure for AI agents — a free, open-source alternative to hosted memory APIs.

Memoratum ingests documents, chat turns, and code, then serves them back as searched **chunks** (raw grounding), **memories** (extracted facts in a temporal graph), and **profiles** (always-on summaries) — all scoped by hard-isolation **container tags**.

## Status

Phase 1 core works: ingest → hybrid search behind a Supermemory-compatible API.

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

Set `MEMORATUM_API_KEY` to require Bearer auth. For fact extraction
("dreaming"), point `MEMORATUM_LLM_ENDPOINT`/`MEMORATUM_LLM_MODEL` at any
OpenAI-compatible chat endpoint (`MEMORATUM_LLM_KEY` optional); documents
accept `"dreaming": "instant"|"dynamic"`, and search accepts
`"searchMode": "memories"|"documents"|"hybrid"`. Build follows `docs/PARITY.md`.

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE). If you run a modified
version on a network server, you must offer users the Corresponding Source (AGPL §13).
