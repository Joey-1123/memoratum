# Research: the OpenCode plugin API surface that actually exists

Every claim below was read from source, not from documentation prose. The
documentation at `opencode.ai/v2/docs` describes an API that no released
OpenCode implements, which is the defect this feature exists to fix.

## Finding 1 — the shipped plugin cannot load on a released OpenCode

`clients/opencode/memoratum.js` default-exports `{ id, setup }`.

OpenCode 1.19.3 loads plugins in `packages/opencode/src/plugin/index.ts`:

```ts
async function applyPlugin(load: PluginLoader.Loaded, input: PluginInput, hooks: Hooks[]) {
  const plugin = readV1Plugin(load.mod, load.spec, "server", "detect")
  if (plugin) {
    hooks.push(await (plugin as PluginModule).server(input, load.options))
    return
  }
  ...
}
```

`readV1Plugin` lives in `packages/opencode/src/plugin/shared.ts`:

```ts
const value = mod.default
if (!isRecord(value)) { if (mode === "detect") return; throw ... }
if (mode === "detect" && !("id" in value) && !("server" in value) && !("tui" in value)) return
const server = "server" in value ? value.server : undefined
...
if (kind === "server" && server === undefined) {
  throw new TypeError(`Plugin ${spec} must default export an object with server()`)
}
```

The shipped export has `id` but no `server`, so detection does **not** bail out
early and the loader throws. `applyPlugin` is wrapped in
`Effect.tryPromise(...).pipe(Effect.tapError(logError("failed to load plugin")))`,
so the throw is swallowed into a log line and the session continues without
the plugin. The plugin is therefore **silently inert**, not visibly broken.

`node --check clients/opencode/memoratum.js` — the only gate CI applies —
passes, because it checks syntax and not loadability.

## Finding 2 — there is no stable V2

`registry.npmjs.org/opencode-ai` at the time of writing:

- `dist-tags.latest` = `1.19.5`
- no `2.x` version has ever been published
- V2-shaped builds exist only under moving tags: `next`, `dev`, `beta`,
  `0.0.0-tui-v2-*`, `0.0.0-next-*`

The installed `@opencode-ai/plugin@1.18.32` ships a V2 surface at
`dist/v2/promise/`, and that surface is small:

```ts
export interface PluginContext {
  readonly options: PluginOptions
  readonly agent: AgentHooks & Reload
  readonly aisdk: AISDKHooks
  readonly catalog: CatalogHooks & Reload
  readonly command: CommandHooks & Reload
  readonly integration: IntegrationHooks & Reload
  readonly plugin: PluginDomain
  readonly reference: ReferenceHooks & Reload
  readonly skill: SkillHooks & Reload
}
```

There is no `session`, no `tool`, no `location`, no `storage`, no `event`.
The APIs the shipped plugin calls — `ctx.session.hook("prompt")` and
`ctx.tool.hook("execute.after")` — do not exist in this generation at all.

## Finding 3 — the V1 hooks this feature will use do fire in 1.19.3

`@opencode-ai/plugin@1.18.32` exports the V1 `Hooks` interface. The hooks
this feature depends on:

| Hook | Input | Output | Status |
|---|---|---|---|
| `chat.message` | `{sessionID, agent, model, messageID}` | `{message, parts}` | in the V1 type |
| `experimental.chat.system.transform` | `{sessionID, model}` | `{system: string[]}` | **triggered** — see below |
| `experimental.session.compacting` | `{sessionID}` | `{context, prompt?}` | in the V1 type |
| `tool.execute.after` | `{tool, sessionID, callID, args}` | `{title, output, metadata}` | in the V1 type |

`experimental.chat.system.transform` is triggered for every model call in
`packages/opencode/src/session/llm/request.ts`:

```ts
const header = system[0]
yield* input.plugin.trigger(
  "experimental.chat.system.transform",
  { sessionID: input.sessionID, model: input.model },
  { system },
)
if (system.length > 2 && system[0] === header) {
  const rest = system.slice(1)
  system.length = 0
  system.push(header, rest.join("\n"))
}
```

`system` starts as a one-element array. Pushing a second element therefore
survives the re-join branch (`system.length` becomes 2, not > 2) and is sent
as its own `role: "system"` message. Pushing one element is the correct
way to inject a block without disturbing the primary system prompt.

The remaining hooks were confirmed present in the shipped 1.19.3 binary by
scanning its ASCII strings for the hook-name literals:
`experimental.chat.messages.transform` (3 occurrences),
`experimental.session.compacting` (2), `experimental.chat.system.transform`
(3), `tool.execute.before` (9), `tool.execute.after` (8), `chat.message` (5).

Which of them fire in practice is settled empirically in
[`quickstart.md`](./quickstart.md) § Hook census, not by argument.

## Finding 4 — the transcript is reachable in both API generations

V1 has no hook argument carrying the transcript, but `PluginInput.client` is
the OpenCode server SDK client, and the SDK exposes:

```ts
messages<ThrowOnError>(options: Options<SessionMessagesData, ThrowOnError>)
// path: { id: sessionID }, query?: { directory?, limit? }
// 200: Array<{ info: Message; parts: Array<Part> }>
```

V2's compaction hook carries the transcript directly
(`ctx.session.hook("compaction", (event) => { event.messages })`).

Both shapes are `{info, parts}`-ish, but not identical, so the shared
extractor accepts the common variants rather than assuming one.

## Finding 5 — the server side already does the hard part

The plugin needs no server changes. The existing API covers every operation:

- `POST /v4/search` with `searchMode: "memories"` returns facts only, which
  is the right recall surface — chunks are raw text, facts are memory.
- `POST /v3/documents` accepts `customId`, and `customId` is unique per
  `(container_tag, custom_id)`, so re-ingesting the same content is
  idempotent.
- The dreaming pipeline already turns prose into subject/predicate/object
  facts, and already supersedes contradictions. The plugin should hand it
  prose and let the server decide what is durable.

That last point is the reason the plugin does **not** need its own LLM
call and does **not** need to re-implement fact extraction.

## Finding 6 — the current recall design corrupts the transcript

The shipped plugin injects recalled memory by rewriting the user prompt:

```js
event.prompt.text = `${text}\n\n[memoratum]\nRelevant memories:\n${lines.join("\n")}`
```

OpenCode's own documentation for the prompt hook states that edits become the
canonical persisted user input. So the injected block is written into the
stored conversation, and the next recall appends another block to a later
prompt. Memory accumulates in the transcript as duplicated text, and the
model sees injected text as something the user said.

Injecting at the system layer instead keeps the user's prompt verbatim and
puts memory where the model treats it as context.

## Finding 7 — `similarity` is an RRF score, not a cosine

`docs/ARCHITECTURE.md` records the fusion as RRF at k=60 with a 0.6/0.4
weight, and warns that the resulting `similarity` values are small numbers.
A fixed cosine-style threshold would therefore reject everything. This
feature bounds recall by **hit count and character budget** rather than by a
score threshold, and the quickstart records the observed score range so a
future threshold can be chosen from evidence.

## Finding 8 — the configuration key differs between generations

The V2 documentation configures plugins under `plugins`. OpenCode 1.19.x
uses `plugin`:

```ts
// @opencode-ai/plugin@1.18.32
export type Config = Omit<SDKConfig, "plugin"> & {
  plugin?: Array<string | [string, PluginOptions]>
}
```

Passing the V2 spelling to 1.19.x produces this, and the setting is
dropped:

```
message="configuration compatibility diagnostic"
path="[\"plugins\"]" kind=unsupported
action="Omitted native setting that cannot be represented in V1"
```

The diagnostics are silent about the consequence: no plugin loads, and
nothing else says so.

## Finding 9 — plugins are also auto-discovered

`packages/opencode/src/config/plugin.ts`:

```ts
export async function load(dir: string) {
  const plugins: ConfigPluginV1.Spec[] = []
  for (const item of await Glob.scan("{plugin,plugins}/*.{ts,js}", {
    cwd: dir, absolute: true, dot: true, symlink: true,
  })) {
    plugins.push(pathToFileURL(item).href)
  }
  return plugins
}
```

So a plain `.js` file placed in `plugin/` or `plugins/` beside a config file
is loaded without any config entry at all. Path specs in the config are
resolved relative to the config file that declares them
(`resolvePluginSpec`), and a directory plugin must contain a `package.json`
or an `index.{ts,tsx,js,mjs,cjs}` file.

## Finding 10 — a plugin that fails to load is nearly invisible

The loader reports failures as session error events, not as log lines:

```ts
error(candidate, _retry, stage, error, resolved) {
  ...
  publishPluginError(`Failed to load plugin ${spec}: ${message}`)
  return
}
```

A session error event is easy to miss, and when the session also has an
unrelated failure — a model error, a permission prompt — the plugin error is
buried. The original plugin's failure was invisible for exactly this
reason. The fix is not only to make the plugin load but to make its absence
noticeable: a `package.json` with `engines.opencode` turns a version
mismatch into an explicit compatibility error.

## Decision record

| Decision | Choice | Why |
|---|---|---|
| Target API | Dual export `{id, server, setup}` | `server` satisfies released OpenCode; `setup` satisfies V2 snapshots. One shared core, two thin adapters. |
| Capture trigger | Session compaction only | Fewest writes, no per-turn noise, and the transcript is complete at that boundary. Sessions that never compact contribute nothing — documented, not hidden. |
| Recall surface | `searchMode: "memories"` | Facts are memory; chunks are raw text that would re-inject noise. |
| Injection point | System layer, never the user prompt | Keeps the transcript clean and stops injected memory compounding. |
| Scope | `containerTag` derived from the OpenCode project id | Container tags are the hard isolation boundary the server already enforces. |
| Fact extraction | Delegated to the server's dreaming pipeline | The plugin holds no LLM configuration and adds no dependency. |
| Dependencies | None | The plugin is a bare ESM file; `node --experimental-strip-types` runs the test with no install step. |
