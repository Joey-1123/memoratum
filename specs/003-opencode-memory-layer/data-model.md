# Data model: OpenCode memory layer

The plugin owns no storage. It reads and writes Memoratum's existing
records through the HTTP API. Three concepts are worth naming because they
exist on the plugin side and have no server-side table of their own.

## Project tag

The container tag under which a session's memories live.

| Attribute | Value |
|---|---|
| Derivation | `opencode:<opencode project id>` |
| Override | `MEMORATUM_TAG` replaces the derivation entirely |
| Fallback | `opencode:default` when the project id is unavailable |
| Scope | One tag per project, shared by every session and worktree of that project |

The tag is the isolation boundary. The server enforces it as a mandatory
filter on every read path, so the plugin's job is only to send the right one
on every request.

An operator who wants one shared memory across several repositories sets
`MEMORATUM_TAG` to a fixed value and accepts that those repositories can see
each other's memories.

## Memory block

The bounded text injected into the model's context.

| Attribute | Value |
|---|---|
| Source | `POST /v4/search` with `searchMode: "memories"` |
| Shape | One line per fact: `- <subject> <predicate> <object>` |
| Hit limit | 3 |
| Character budget | 600, applied after formatting |
| Placement | Pushed as one element of the system array |
| Empty case | No element is pushed; the model's context is untouched |

The block is derived state. It is never stored, and it is never written into
the transcript.

## Capture

One stored transcript segment.

| Attribute | Value |
|---|---|
| Trigger | Session compaction |
| Content | Plain text extracted from the transcript since the previous capture |
| Identity | `customId = oc:<sessionID>:<lastMessageId>` |
| Idempotency | `UNIQUE(container_tag, custom_id)` — re-capture replaces, never duplicates |
| Character budget | 20 000 |
| Scope | The project tag of the session |

The `lastMessageId` is the id of the last transcript message the capture
covered. The next capture starts after that message, so consecutive
compactions do not re-store the same content.

## Server-side records the plugin creates

| Record | Created by | Notes |
|---|---|---|
| `documents` row | `POST /v3/documents` | One per capture; `status` moves `queued → done` under the worker |
| `chunks` rows | the ingest pipeline | Derived from the document |
| `facts` rows | the dreaming pipeline | Extracted from the document's prose; superseded when contradicted |

The plugin never writes a `facts` row directly. Fact extraction, and the
supersession that makes the memory trustworthy, stay in the server where
they are already tested.

## What the plugin never stores

- The API key, in any form.
- The user's prompt text, modified or appended to.
- Anything scoped to a tag other than the session's project tag.
