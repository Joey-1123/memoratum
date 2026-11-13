# Quickstart: the Memoratum OpenCode plugin

## Install

The plugin is a single ESM file with no dependency.

```sh
# From a checkout
cp clients/opencode/memoratum.js ~/.config/opencode/plugins/memoratum.js
```

Or point OpenCode at the repository copy:

```jsonc
// opencode.jsonc
{
  "plugins": ["D:/projects/memoratum/clients/opencode"]
}
```

Configure the server:

```sh
export MEMORATUM_URL=http://localhost:6767
export MEMORATUM_API_KEY=mm_...        # the admin key the server printed on first boot
# optional: one shared memory across every project
# export MEMORATUM_TAG=my-team
```

Restart OpenCode. The plugin registers itself; there is no command to run.

## What it does

| Moment | Behaviour |
|---|---|
| You send a prompt | The plugin looks up facts stored for this project and injects the matching ones into the model's context |
| The session compacts | The transcript is stored under the project tag, and the server's dreaming pipeline extracts the durable facts from it |

The plugin never modifies what you typed, and never breaks a session. If
Memoratum is unreachable, the session behaves exactly as if the plugin were
not installed.

## Verify it is loaded

```sh
opencode --print-logs --log-level DEBUG 2>&1 | grep -i "plugin"
```

A line like `failed to load plugin` means the runtime does not accept the
plugin's shape. Check `opencode --version` against the range in
`clients/opencode/package.json` under `engines.opencode`.

## Verify it remembers

```sh
# 1. Ask the agent something worth remembering.
opencode run "we decided to use RRF fusion instead of weighted cosine because FTS5 has no vector index"

# 2. Let the session compact, or trigger it explicitly.
opencode run "/compact"

# 3. Check the server has the facts.
curl -s localhost:6767/v4/facts?containerTag=opencode:<project-id> \
  -H "Authorization: Bearer $MEMORATUM_API_KEY"

# 4. Ask again in a new session.
opencode run "why did we choose RRF?"
```

Step 3 returning facts means the loop closed: the transcript was stored, the
dreaming pipeline extracted the decision, and the fact is retrievable.

## Hook census

Recorded from live OpenCode 1.19.3 on 2026-11-12/13, using a probe plugin
that appended every hook it was handed to a file. The probe was driven both
through `opencode run` and through a headless `opencode serve` instance
whose `POST /session/:id/summarize` route was called to force a compaction.

| Hook | Fired | Notes |
|---|---|---|
| `server(input)` | yes | `input.project.id`, `input.directory` and `input.client` are all populated |
| `chat.message` | yes | `output.parts` carries the user message |
| `experimental.chat.system.transform` | yes | `output.system` is a **one-element** array on entry; pushing a second element is what sends memory as its own system message |
| `experimental.chat.messages.transform` | yes | `output.messages` carries the transcript |
| `experimental.session.compacting` | yes | Fires on the summarize route, which is the compaction path |
| `chat.params` | yes | |
| `chat.headers` | yes | |
| `config` | yes | |
| `event` | yes | |
| `tool.definition` | yes | |
| `experimental.provider.small_model` | yes | During the title-generation call |
| `tool.execute.before` / `tool.execute.after` | not observed | The probe runs used no tools |
| `experimental.text.complete` | not observed | Not needed: capture reads the transcript instead |
| `permission.ask` | not observed | The probe ran with `--auto` |
| `command.execute.before`, `shell.env` | not observed | Not needed by this plugin |

Two observations that shape the implementation:

1. `experimental.chat.system.transform` fires **twice** per new session —
   once for the title-generation call and once for the agent call. The hook
   cannot tell them apart, so the injected block is sent in both. The block
   is character-bounded, so the cost is bounded too.
2. The plugin's own capture path (`experimental.session.compacting`) fires
   **before** the summarising model call, so the transcript it reads is the
   pre-compaction one.

The census is the reason Phase 0 of the plan exists: the plugin was written
against the V1 type declarations, and the census confirms the runtime
actually invokes them.

## Installing the plugin

OpenCode 1.19.x reads plugins from two places.

**Auto-discovery** — drop the file into a `plugin/` or `plugins/` directory
next to a config file:

```sh
# per project
mkdir -p .opencode/plugin
cp clients/opencode/memoratum.js .opencode/plugin/memoratum.js

# or for every project
mkdir -p ~/.config/opencode/plugin
cp clients/opencode/memoratum.js ~/.config/opencode/plugin/memoratum.js
```

**Config entry** — the key differs between API generations. OpenCode 1.19.x
uses `plugin` (singular); V2 uses `plugins`:

```jsonc
// OpenCode 1.19.x
{ "plugin": ["../clients/opencode"] }
```

A path is resolved relative to the config file that declares it, so the
entry above points at the repository checkout. OpenCode 1.19.x reports the
V2 spelling as `Omitted native setting that cannot be represented in V1`
and loads nothing.

## Observed score range

`similarity` from `/v4/search` is an RRF fusion score, not a cosine, so its
absolute value is small and not comparable to a cosine threshold.

Observed during the Phase 6 dogfood, with the default `hash` embeddings
provider and one stored fact:

| Query | Hit | `similarity` |
|---|---|---|
| "why did we choose RRF?" | the team decided to use RRF fusion instead of weighted cosine because FTS5 has no vector index | 0.009836 |

A matching fact scores in the low thousandths. A cosine-style threshold
would reject it, which is why recall is bounded by hit count and character
budget rather than by a score. A real embeddings provider will move the
number; the *shape* — a small RRF value — will not.

## Troubleshooting

**No facts come back.**
Facts are created by the dreaming pipeline, which needs an LLM endpoint
configured on the server (`MEMORATUM_LLM_ENDPOINT`, `MEMORATUM_LLM_MODEL`).
Without it, documents are chunked and embedded but no facts exist, and
recall — which queries facts only — returns nothing. Confirm with
`GET /v4/facts?containerTag=...`.

**Recall returns nothing even though facts exist.**
Check the tag. The plugin derives `opencode:<project-id>`; a mismatch with
the tag the facts were written under is the usual cause. Set `MEMORATUM_TAG`
to pin it.

**Everything matches.**
The default embeddings provider is `hash`, a deterministic pseudo-random
vector that carries no semantic similarity. Configure a real provider
(`MEMORATUM_EMBEDDINGS_PROVIDER=api` with an OpenAI-compatible endpoint, or
FastEmbed) before expecting meaningful retrieval.

**The session does not capture.**
Capture happens at compaction. A session that ends without compacting
contributes nothing. This is a deliberate trade: capture-on-every-turn would
store far more noise and would cost more dreaming calls.
