# Contract: the Memoratum OpenCode plugin

The shape of `clients/opencode/memoratum.js` that OpenCode's loader accepts
and that the test suite pins.

## Export shape

```js
export default {
  id: "memoratum",
  async server(input) { /* released OpenCode */ },
  async setup(ctx) { /* V2 builds */ },
}
```

| Export | Requirement | Reason |
|---|---|---|
| `id` | Required, string, `"memoratum"` | The loader uses it for plugin identity and rejects a path plugin without one |
| `server` | Required, a function of `(input) => Promise<Hooks>` | The released loader calls `server(input)`; without it the plugin throws and is skipped |
| `setup` | Required, a function of `(ctx) => Promise<void>` | The V2 loader calls `setup(ctx)`; without it a V2 build loads the plugin and does nothing |

Both exports are required by this contract even though each runtime uses
only one. Exporting both is what makes a single file work on both.

`server` MUST return an object of V1 hooks. `setup` MUST register its hooks
and MUST NOT throw.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `MEMORATUM_URL` | `http://localhost:6767` | Base URL of the Memoratum server. Trailing slashes are stripped. |
| `MEMORATUM_API_KEY` | _(empty)_ | Bearer token. When empty, no `Authorization` header is sent and the server's own 401 decides. |
| `MEMORATUM_TAG` | _(unset)_ | Overrides the derived container tag for every project. |

## Bounds

| Bound | Value | Enforced |
|---|---|---|
| Request timeout | 2000 ms | `AbortSignal.timeout` on every `fetch` |
| Response size | 262144 bytes | Checked on the raw text before `JSON.parse` |
| Recall hits | 3 | `limit` on the search request |
| Injected characters | 600 | Applied to the formatted block |
| Stored characters | 20000 | Applied to the transcript text before the request |

## Requests the plugin makes

### Recall

```
POST {MEMORATUM_URL}/v4/search
Authorization: Bearer {MEMORATUM_API_KEY}

{"q": "<first 500 chars of the user prompt>",
 "containerTag": "<project tag>",
 "searchMode": "memories",
 "limit": 3}
```

### Capture

```
POST {MEMORATUM_URL}/v3/documents
Authorization: Bearer {MEMORATUM_API_KEY}

{"content": "<transcript text>",
 "containerTag": "<project tag>",
 "customId": "oc:<sessionID>:<lastMessageID>"}
```

The plugin makes no other request. It never issues a `GET`, and it never
contacts a host other than `MEMORATUM_URL`.

## Hook contracts

### V1 — `server(input)`

`input` is OpenCode's `PluginInput`: `{client, project, directory, worktree, serverUrl, $}`.

| Hook | Signature | Behaviour |
|---|---|---|
| `chat.message` | `(input, output) => Promise<void>` | Records the user prompt text for the session and invalidates the cached recall |
| `experimental.chat.system.transform` | `(input, output) => Promise<void>` | Pushes the cached memory block onto `output.system` |
| `experimental.session.compacting` | `(input, output) => Promise<void>` | Fetches the transcript via `input.client.session.messages` and stores the uncaptured segment |

`experimental.session.compacting` MUST NOT set `output.prompt` or
`output.context`. The plugin observes the compaction; it does not replace it.

### V2 — `setup(ctx)`

| Hook | Signature | Behaviour |
|---|---|---|
| `ctx.session.hook("prompt")` | `(event) => void` | Records `event.prompt.text` and invalidates the cached recall |
| `ctx.session.hook("context")` | `(event) => void` | Pushes `{type: "text", text: block}` onto `event.system` |
| `ctx.session.hook("compaction")` | `(event) => void` | Stores the text of `event.messages` |

`ctx.session.hook("compaction")` MUST NOT set `event.result`. The plugin
observes the compaction; it does not replace it.

## Failure behaviour

| Condition | Behaviour |
|---|---|
| Server unreachable | The hook returns without throwing. Nothing is injected. |
| Server returns non-2xx | Same. The status is not logged with the request body. |
| Response larger than the limit | The response is rejected before parsing. Nothing is injected. |
| Body is not JSON | The parse error is caught. Nothing is injected. |
| The prompt is empty | No recall is attempted. |
| The project id is unavailable | The tag falls back to `opencode:default`. |

In every case the OpenCode session continues normally. A plugin that breaks
the host application is worse than a plugin that remembers nothing.

## Licensing

The plugin is MIT, inside the `LICENSE-MIT` client exception. It imports no
server code and adds no dependency.
