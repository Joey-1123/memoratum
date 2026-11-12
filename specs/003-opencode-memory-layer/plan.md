# Implementation Plan: OpenCode memory layer

## Summary

Turn `clients/opencode/memoratum.js` from a file that cannot load into a
working memory layer for OpenCode, without touching the server.

The plugin becomes a dual export — `server(input)` for released OpenCode,
`setup(ctx)` for V2 builds — over one shared core that talks to the existing
Memoratum HTTP API. Recall injects facts into the model's system context;
capture stores the transcript at session compaction and lets the server's
dreaming pipeline decide what is durable.

The work is client-only. No route, schema, migration, or server behaviour
changes, because the API needed already exists.

## Technical Context

**Language**: JavaScript (ESM, no build step) with one TypeScript test file
run through Node's type stripping
**Runtime**: Node 22 (CI), OpenCode 1.19.x (target), V2 snapshots (secondary)
**Dependencies**: none added
**Transport**: `fetch` against `MEMORATUM_URL`, bearer auth from
`MEMORATUM_API_KEY`

## Constitution Check

| Principle | How this change satisfies it |
|---|---|
| I. Self-hosted and local-first | The plugin contacts exactly one host: the operator's own Memoratum URL. No telemetry, no external analytics. `scripts/check_no_telemetry.py` covers `clients/` and stays green. |
| II. Hard isolation between scopes | Every read and write carries a `containerTag` derived from the OpenCode project id (FR-004). The server enforces the boundary; the plugin never requests an unscoped read. |
| III. Durable and recoverable work | The plugin owns no jobs. Capture is idempotent through `customId`, so a retried compaction cannot duplicate a document. |
| IV. Evidence before assertion | A behaviour test suite drives the real plugin code with a stubbed transport. The hook census in `quickstart.md` records which hooks actually fire, instead of assuming. |
| V. Provider-neutral, minimal dependencies | No dependency is added. The plugin speaks the documented HTTP API, so it is not coupled to an embedding or LLM provider. |
| Licence split | The plugin stays MIT, inside the `LICENSE-MIT` client exception. It imports no server code. |
| Security constraints | Every call is bounded by a timeout and a response size limit (FR-003). The API key is read from the environment and never logged (FR-008). |
| Documentation in the same change | `clients/README.md`, `README.md`, `docs/PARITY.md`, `docs/ARCHITECTURE.md` and `CHANGELOG.md` are updated in the same PR as the behaviour they describe. |
| Quality gates | The constitution's gate list gains the plugin test command. That is a PATCH amendment with a rationale, not a silent edit. |

No principle is violated and no amendment beyond the gate-list PATCH is
required.

## Project Structure

### Documentation (this feature)

```
specs/003-opencode-memory-layer/
├── spec.md                          # user stories, requirements, success criteria
├── plan.md                          # this file
├── tasks.md                         # task list
├── research.md                      # source-verified API findings
├── data-model.md                    # tag, memory block, capture
├── quickstart.md                    # install, hook census, dogfood
└── contracts/
    └── plugin-contract.md           # the dual-export shape and hook map
```

### Source Code (repository root)

```
clients/opencode/
├── memoratum.js                     # rewritten: dual export over one shared core
├── test.ts                          # new: behaviour tests, stubbed transport
└── package.json                     # new: name, main, exports, engines.opencode
```

`clients/opencode/package.json` exists so the OpenCode loader can find the
entry point through `main` and can enforce a version range through
`engines.opencode`. Without it, a plugin built for 1.19.x loads silently on
an incompatible build and appears to work while doing nothing.

## Design

### Shared core

One implementation, used by both API generations:

| Function | Responsibility |
|---|---|
| `api(path, body)` | POST JSON to the Memoratum API with bearer auth, a timeout, and a response size limit |
| `recall(tag, query)` | Query facts, format them, bound them by hit count and character budget |
| `store(tag, text, customId)` | Store a transcript segment under an idempotent id |
| `toText(messages)` | Extract plain text from either transcript shape |
| `projectTag(input)` | Resolve the container tag for the session |

The adapters supply the differences:

```js
export default {
  id: "memoratum",
  async server(input) { /* V1 hooks */ },
  async setup(ctx) { /* V2 hooks */ },
}
```

### Hook map

| Concern | V1 (`server(input)`) | V2 (`setup(ctx)`) |
|---|---|---|
| Observe a new user prompt | `chat.message` → `output.parts` | `ctx.session.hook("prompt")` → `event.prompt.text` |
| Inject at model-call time | `experimental.chat.system.transform` → `output.system.push(block)` | `ctx.session.hook("context")` → `event.system.push({type:"text", text:block})` |
| Capture at compaction | `experimental.session.compacting` → `input.client.session.messages({path:{id}})` | `ctx.session.hook("compaction")` → `event.messages` |

### Recall

`POST /v4/search` with `{q, containerTag, searchMode: "memories", limit}`.

`searchMode: "memories"` returns facts only. Chunks are raw text and would
re-inject noise the model has already seen. Recall is bounded by a hit count
and a character budget rather than by a score threshold, because
`similarity` is an RRF fusion score and not a cosine — see
[`research.md`](./research.md) Finding 7.

The result is cached per session and invalidated when a new user prompt
arrives, so a multi-step turn performs one recall rather than one per model
call.

### Injection

The block is pushed onto the system array as its own element, which
OpenCode sends as a separate `role: "system"` message. The user's prompt is
never modified, so nothing injected is persisted into the transcript and
nothing compounds across turns.

### Capture

At compaction, the transcript since the previous capture is stored as one
document with `customId = oc:<sessionID>:<lastMessageId>`. The server's
`UNIQUE(container_tag, custom_id)` makes a repeated compaction idempotent,
and the last-message id keeps the second compaction from re-storing what
the first already captured.

Capture stores prose. The server's dreaming pipeline extracts the durable
facts and supersedes contradictions, so the plugin holds no LLM
configuration and adds no dependency.

### Bounds

| Bound | Value | Reason |
|---|---|---|
| Request timeout | 2 s | A hook that stalls a prompt is worse than no memory |
| Response size | 256 KiB | The constitution requires an explicit limit on network calls |
| Recall hits | 3 | Enough signal, small enough to ignore |
| Injected characters | 600 | Keeps the block a footnote, not a document |
| Stored characters | 20 000 | Bounds a pathological transcript |

## Implementation Phases

**Phase 0 — Hook census.** A probe plugin logs every hook OpenCode 1.19.3
invokes during a real session. This settles which hooks fire before any of
the plugin's logic is written. Recorded in
[`quickstart.md`](./quickstart.md) § Hook census.

**Phase 1 — Rewrite.** `clients/opencode/memoratum.js` becomes the dual
export over the shared core.

**Phase 2 — Tests.** `clients/opencode/test.ts` drives the real plugin with
a stubbed `fetch`: recall injection, the character budget, fail-soft on a
server outage, capture idempotency, project isolation, and the export
shape.

**Phase 3 — Packaging.** `clients/opencode/package.json` with `name`,
`main`, `exports`, and `engines.opencode`.

**Phase 4 — Gates.** The plugin test runs in CI, and the constitution's gate
list gains it.

**Phase 5 — Documentation.** The five documents that describe the plugin
are corrected in the same change.

**Phase 6 — Dogfood.** A real session against a local Memoratum with
embeddings configured: facts land, and a later session recalls them.

## Cross-Cutting Invariants

1. The plugin never throws into OpenCode. Every hook body is wrapped, and a
   failure degrades to "no memory", never to "broken session".
2. The plugin never mutates a user prompt.
3. The plugin never issues an unscoped request.
4. The plugin never logs a credential.
5. The plugin adds no dependency and touches no server file.
6. The plugin's behaviour is covered by an executed test, not by a claim.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `experimental.*` hooks move between OpenCode releases | Plugin silently stops working | `engines.opencode` range; fail-soft by design; documented as experimental in `clients/README.md` |
| Capture-only-at-compaction misses sessions that never compact | Some sessions contribute nothing | Documented in the spec and the quickstart; `GET /v4/profile` and the MCP server remain available for manual capture |
| Transcripts contain user data | Sensitive text stored locally | Local-only by constitution; scoped by container tag; the operator opts in by configuring the plugin |
| Recall finds nothing before dreaming runs | Memory appears empty on a fresh install | Expected and explained in the quickstart; the operator configures `MEMORATUM_LLM_ENDPOINT` on the server |
| `similarity` is an RRF score, so a naive threshold rejects everything | Recall returns nothing | Recall is bounded by count and budget, not by score; the observed range is recorded in the quickstart |
| Dual export doubles the adapter surface | Two places to update | The shared core is single-sourced; only the hook registrations differ |
| The plugin grows past what one file should hold | Maintenance cost | The shared core is plain functions in one file; split only if it actually hurts |
