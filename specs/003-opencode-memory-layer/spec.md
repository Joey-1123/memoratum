# Feature Specification: OpenCode memory layer

**Feature Branch**: `feature/opencode-memory-layer`
**Created**: 2026-11-12
**Status**: Draft
**Input**: "Make Memoratum usable as a memory layer for OpenCode, or say what must be improved."

## Problem statement

`clients/opencode/memoratum.js` exists and is documented as an "OpenCode V2
plugin", but it cannot load on any released OpenCode: the loader requires a
default export carrying `server()`, and the file exports `{ id, setup }`. The
failure is swallowed into a log line, so the plugin is silently inert while
`node --check` passes in CI.

The underlying capability is already built. Memoratum stores facts in a
temporal graph, supersedes contradictions, and enforces hard isolation by
container tag. The client that is supposed to expose that to OpenCode is
the broken part.

## User Scenarios & Testing

### User Story 1 - The plugin loads (Priority: P1)

As an OpenCode user, I install the Memoratum plugin and my session starts
normally, with no plugin errors in the server log.

**Why this priority**: Nothing else matters if the plugin does not load.
Today it silently does not.

**Independent Test**: Start OpenCode with the plugin configured and read the
server log. No `failed to load plugin` line, and a probe in the plugin's
`setup`/`server` path prints once.

**Acceptance Scenarios**:

1. **Given** OpenCode 1.19.x with the plugin configured, **When** a session
   starts, **Then** the plugin loads and registers its hooks with no error
   in the log.
2. **Given** an OpenCode build that does not implement the plugin's hooks,
   **When** the plugin is loaded, **Then** the session still starts and the
   model still works.
3. **Given** the plugin's own test suite, **When** it runs, **Then** it
   exercises the real plugin code paths with a stubbed transport.

---

### User Story 2 - Relevant memory reaches the model (Priority: P1)

As an OpenCode user, when I ask about something we decided earlier, the
model sees that decision without me repeating it.

**Why this priority**: Recall is the reason a memory layer exists. Capture
without recall is an archive.

**Independent Test**: Seed a fact into a project tag, start a session, and
observe the recalled block in the model's system prompt.

**Acceptance Scenarios**:

1. **Given** a project tag holding matching facts, **When** the user sends a
   prompt, **Then** the matching facts are injected into the model context
   as a system block.
2. **Given** a prompt unrelated to anything stored, **When** the model call
   is assembled, **Then** nothing is injected.
3. **Given** a stored fact set larger than the budget, **When** it is
   injected, **Then** the injected block never exceeds the character budget
   and never exceeds the hit limit.
4. **Given** the user sends a prompt, **When** the prompt is stored in the
   session transcript, **Then** the stored prompt is byte-identical to what
   the user typed.

---

### User Story 3 - A session's lessons survive it (Priority: P2)

As an OpenCode user, what I settled in one session is available in the next
one.

**Why this priority**: Capture completes the loop, but a broken capture is
worse than none, so it follows a working recall.

**Independent Test**: Run a session that reaches compaction, then query the
project tag and find the session's content.

**Acceptance Scenarios**:

1. **Given** a session that compacts, **When** the compaction completes,
   **Then** the transcript is stored under the project tag.
2. **Given** the same compaction happens twice, **When** it is stored, **Then**
   only one document exists for it.
3. **Given** two consecutive compactions, **When** they are stored, **Then**
   the second does not re-store the content the first already captured.

---

### User Story 4 - Memory never crosses a project boundary (Priority: P1)

As an operator running several repositories against one Memoratum, memories
from one project are never visible in another.

**Why this priority**: Container-tag isolation is the property that makes a
shared self-hosted instance safe to point at more than one codebase.

**Independent Test**: Store a fact under project A's tag, then run a session
for project B and confirm project A's fact is not returned.

**Acceptance Scenarios**:

1. **Given** two projects in one OpenCode instance, **When** each starts a
   session, **Then** each uses its own container tag.
2. **Given** a fact stored under project A, **When** project B searches,
   **Then** the fact is not returned.
3. **Given** an operator sets `MEMORATUM_TAG`, **When** the plugin starts,
   **Then** that tag is used for every project.

---

### Edge Cases

- The Memoratum server is down, slow, or returns a non-2xx status: the hook
  returns without throwing, injects nothing, and the session continues.
- The server returns a body larger than the plugin's limit: the response is
  rejected without parsing, and nothing is injected.
- The user sends an empty prompt, or only whitespace: no recall is attempted.
- A session has no compaction: nothing is captured. This is the documented
  cost of compaction-only capture.
- The OpenCode project id is unavailable: the plugin falls back to a stable
  default tag rather than writing to an unscoped one.
- The API key is absent: the plugin still attempts the call and lets the
  server's own 401 decide. The key is never logged.

## Requirements

### Functional Requirements

- **FR-001**: The plugin MUST default-export an object carrying both a
  `server(input)` function and a `setup(ctx)` function, so it loads on
  released OpenCode and on V2 builds.
- **FR-002**: Every hook MUST fail soft. A Memoratum failure MUST NOT throw
  into OpenCode, MUST NOT block a prompt, and MUST NOT inject partial
  content.
- **FR-003**: Every network call MUST be bounded by an explicit timeout and
  by an explicit response size limit checked before the body is parsed.
- **FR-004**: Every read and every write MUST carry a `containerTag`. The
  default MUST be derived from the OpenCode project id; `MEMORATUM_TAG`
  MAY override it.
- **FR-005**: The plugin MUST NOT modify the text of a user prompt.
  Injection MUST happen at the system layer.
- **FR-006**: Injected memory MUST be bounded by a maximum hit count and a
  maximum character budget.
- **FR-007**: Capture MUST happen only at session compaction and MUST be
  idempotent, so a repeated compaction does not create a duplicate document.
- **FR-008**: The API key MUST be read from the environment and MUST NOT be
  written to logs, transcripts, or stored documents.
- **FR-009**: The plugin MUST add no runtime dependency and MUST remain a
  plain ESM file.
- **FR-010**: The plugin MUST remain MIT-licensed and MUST NOT import
  AGPL-licensed server code.
- **FR-011**: Recall MUST query facts, not raw chunks.
- **FR-012**: The plugin MUST NOT emit telemetry and MUST NOT contact any
  endpoint other than the operator-configured Memoratum URL.

### Key Entities

- **Project tag**: the container tag a session's memories live under —
  derived from the OpenCode project id, overridable by the operator.
- **Memory block**: the bounded, formatted text injected into the model
  context — a list of fact lines.
- **Capture**: one stored transcript segment for a session, identified by the
  id of the last message it covered so that re-capture is idempotent.

## Success Criteria

### Measurable Outcomes

- **SC-001**: The plugin loads on OpenCode 1.19.x with no plugin error in
  the server log.
- **SC-002**: A behaviour test suite exercises every hook against a stubbed
  transport and fails if the plugin regresses; the suite runs in CI.
- **SC-003**: Injected memory never exceeds the configured character budget.
- **SC-004**: A fact stored under project A's tag is never returned in a
  project B session, proven by a test.
- **SC-005**: A Memoratum outage during a session produces no user-visible
  error and no injected content, proven by a test.

## Assumptions

- The operator runs a Memoratum server reachable at `MEMORATUM_URL` and
  supplies `MEMORATUM_API_KEY`.
- Fact extraction is delegated to the server's dreaming pipeline, so the
  operator configures an LLM endpoint there. Without it, recall returns
  nothing because no facts exist.
- The operator accepts that a session which never compacts contributes no
  memory.
- OpenCode's `experimental.*` hooks may move between releases; the plugin
  degrades to doing nothing rather than to failing.
