# Clients

- [`opencode/memoratum.js`](opencode/memoratum.js) — OpenCode server plugin:
  recall into the model's context, capture at session compaction. Env:
  `MEMORATUM_URL` (default `http://localhost:6767`), `MEMORATUM_API_KEY`,
  `MEMORATUM_TAG` (overrides the project-scoped container tag).
  - Ships a dual export: `server(input)` for released OpenCode (1.19.x) and
    `setup(ctx)` for V2 builds. Released OpenCode configures plugins under
    `plugin`; V2 uses `plugins`.
  - Install by dropping the file into `plugin/` or `plugins/` beside an
    OpenCode config file, or by adding the directory to the `plugin` list.
  - `npm test` runs the behaviour suite
    (`node --experimental-strip-types test.ts`).
  - Requires a running Memoratum with an LLM endpoint configured, because
    facts — not raw chunks — are what recall returns.
- [`ts/`](ts/) — TypeScript SDK, zero dependencies: `npm test` runs the
  native and Mem0-compatible route contract asserts (`node --experimental-strip-types`).
- Python SDK — `memoratum.client.Client` in `src/` (tested in
  `tests/test_client.py`).
- MCP server — `python -m memoratum.mcp` (stdio JSON-RPC, `remember`/`recall`).
