# Clients

- [`opencode/memoratum.js`](opencode/memoratum.js) — OpenCode V2 server plugin:
  recall-on-prompt + store-on-write. Env: `MEMORATUM_URL` (default
  `http://localhost:6767`), `MEMORATUM_API_KEY`, `MEMORATUM_TAG`.
- [`ts/`](ts/) — TypeScript SDK, zero dependencies: `npm test` runs the
  native and Mem0-compatible route contract asserts (`node --experimental-strip-types`).
- Python SDK — `memoratum.client.Client` in `src/` (tested in
  `tests/test_client.py`).
- MCP server — `python -m memoratum.mcp` (stdio JSON-RPC, `remember`/`recall`).
