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

Recorded from a live OpenCode 1.19.3 session on 2026-11-12, using a probe
plugin that logged every hook it was handed.

| Hook | Fired? | Notes |
|---|---|---|
| `chat.message` | _pending census_ | |
| `experimental.chat.system.transform` | _pending census_ | |
| `experimental.session.compacting` | _pending census_ | |
| `tool.execute.after` | _pending census_ | |

The census is the reason Phase 0 of the plan exists: the plugin was written
against the V1 type declarations, and the census confirms the runtime
actually invokes them.

## Observed score range

`similarity` from `/v4/search` is an RRF fusion score, not a cosine, so its
absolute value is small and not comparable to a cosine threshold. The
observed range for this deployment:

_Recorded during the Phase 6 dogfood._

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
