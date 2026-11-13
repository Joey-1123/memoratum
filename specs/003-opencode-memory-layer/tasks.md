# Tasks: OpenCode memory layer

**Input**: Design documents from `/specs/003-opencode-memory-layer/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md,
contracts/plugin-contract.md, quickstart.md

**Tests**: included. The constitution requires evidence before assertion, and
the plugin's only current gate is a syntax check.

**Organization**: tasks are grouped by user story so each story can be
implemented, tested and demonstrated independently.

## Format: `[ID] [P] [Story] Description`

- **[P]**: can run in parallel (different files, no dependencies)
- **[Story]**: the user story the task belongs to

## Phase 0: Specification

**Purpose**: record the plan and its evidence before code exists.

- [x] T001 Write `specs/003-opencode-memory-layer/research.md` from source-verified findings
- [x] T002 Write `specs/003-opencode-memory-layer/spec.md` with user stories, FR-001..FR-012 and SC-001..SC-005
- [x] T003 Write `specs/003-opencode-memory-layer/plan.md` with the constitution check and the hook map
- [x] T004 Write `specs/003-opencode-memory-layer/data-model.md`
- [x] T005 Write `specs/003-opencode-memory-layer/contracts/plugin-contract.md`
- [x] T006 Write `specs/003-opencode-memory-layer/tasks.md`
- [x] T007 [P] Write `specs/003-opencode-memory-layer/quickstart.md`

**Checkpoint**: the design is complete and reviewable. No code has changed.

---

## Phase 1: Hook census (US1)

**Purpose**: settle which hooks OpenCode 1.19.3 actually invokes, before
writing logic against them.

**Independent Test**: a probe plugin run against a live OpenCode session
prints the names of the hooks it received.

- [x] T008 Create a throwaway probe plugin outside the repository that logs every hook it is given
- [x] T009 Run `opencode run` with the probe installed and record which hooks fired
- [x] T010 Record the census result in `specs/003-opencode-memory-layer/quickstart.md` § Hook census

**Checkpoint**: the hook set is observed, not assumed.

---

## Phase 2: Plugin rewrite (US1, US2, US4)

**Goal**: a plugin that loads on released OpenCode and on V2 builds.

**Independent Test**: `clients/opencode/test.ts` passes and the plugin loads
in a live session without an error.

### Tests for this phase

- [x] T011 [P] [US1] Assert the default export carries `id`, `server` and `setup` in `clients/opencode/test.ts`
- [x] T012 [P] [US2] Assert recall injects a bounded block into the system array in `clients/opencode/test.ts`
- [x] T013 [P] [US2] Assert the user prompt is never modified in `clients/opencode/test.ts`
- [x] T014 [P] [US2] Assert a non-2xx response injects nothing and does not throw in `clients/opencode/test.ts`
- [x] T015 [P] [US4] Assert the container tag is derived from the project id in `clients/opencode/test.ts`

### Implementation for this phase

- [x] T016 [US1] Implement the shared core in `clients/opencode/memoratum.js`: `api`, `recall`, `store`, `toText`, `projectTag`
- [x] T017 [US1] Implement the V1 `server(input)` adapter in `clients/opencode/memoratum.js`
- [x] T018 [US1] Implement the V2 `setup(ctx)` adapter in `clients/opencode/memoratum.js`
- [x] T019 [US2] Bound the injected block by hit count and character budget
- [x] T020 [US4] Derive the container tag from the OpenCode project id, with `MEMORATUM_TAG` as the override

**Checkpoint**: the plugin loads and recalls. Capture is not implemented yet.

---

## Phase 3: Capture at compaction (US3)

**Goal**: a session's lessons outlive the session.

**Independent Test**: the test suite asserts a compaction stores the
uncaptured segment and that a repeated compaction does not duplicate it.

### Tests for this phase

- [x] T021 [P] [US3] Assert a compaction stores the transcript under the project tag in `clients/opencode/test.ts`
- [x] T022 [P] [US3] Assert a repeated compaction is idempotent in `clients/opencode/test.ts`
- [x] T023 [P] [US3] Assert a second compaction does not re-store the first capture's segment in `clients/opencode/test.ts`

### Implementation for this phase

- [x] T024 [US3] Implement the V1 `experimental.session.compacting` capture in `clients/opencode/memoratum.js`
- [x] T025 [US3] Implement the V2 `compaction` hook capture in `clients/opencode/memoratum.js`
- [x] T026 [US3] Track the last captured message id per session so consecutive captures do not overlap

**Checkpoint**: capture works and is idempotent.

---

## Phase 4: Packaging and gates (US1)

**Goal**: an incompatible runtime fails loudly instead of silently.

- [x] T027 [US1] Add `clients/opencode/package.json` with `name`, `main`, `exports` and `engines.opencode`
- [x] T028 [US1] Run the plugin test in the `opencode-plugin` CI job in `.github/workflows/ci.yml`
- [x] T029 [US1] Add the plugin test command to the gate list in `.specify/memory/constitution.md` with a rationale and a version bump

**Checkpoint**: CI runs the behaviour suite, and the constitution names it.

---

## Phase 5: Documentation (US1)

**Purpose**: the constitution treats a doc/behaviour mismatch as a defect in
both directions.

- [x] T030 [US1] Update `clients/README.md` to describe the dual export, the hooks and the environment variables
- [x] T031 [US1] Update the client list in `README.md`
- [x] T032 [US1] Update the OpenCode line in `docs/PARITY.md`
- [x] T033 [US1] Update the client list in `docs/ARCHITECTURE.md`
- [x] T034 [US1] Add an `Unreleased` entry to `CHANGELOG.md`

**Checkpoint**: every document that describes the plugin matches the plugin.

---

## Phase 6: Dogfood (US1, US2, US3)

**Goal**: the whole loop proven against a running server, not a stub.

- [x] T035 Start a local Memoratum with embeddings configured
- [x] T036 Run a real OpenCode session to compaction and confirm the transcript is stored
- [x] T037 Confirm the dreaming pipeline produced facts from the stored transcript
- [x] T038 Start a second session and confirm the facts are recalled into the model context
- [x] T039 Record the observed `similarity` range in `specs/003-opencode-memory-layer/quickstart.md`

**Checkpoint**: the loop works end to end against a real server.

---

## Dependencies and execution order

- Phase 0 precedes everything; it is already complete.
- Phase 1 precedes Phase 2, because the plugin is written against the
  observed hook set.
- Phase 2 must complete before Phase 3: capture reuses the shared core.
- Phase 4 and Phase 5 follow Phase 3 and are independent of each other.
- Phase 6 follows Phase 5 and validates the whole feature.

## Parallel opportunities

- T011–T015 are independent of each other and of T016–T020.
- T021–T023 are independent of each other.
- T027–T029 (packaging and gates) and T030–T034 (documentation) touch
  different files and can run in parallel.

## Notes

- The plugin stays a single file with no dependency. If it grows past what
  one file should hold, split it then, not before.
- Every task is committed on its own branch commit, in order, so the history
  reads as the plan.

## Local completion evidence (2026-11-13)

All tasks above are complete on eature/opencode-memory-layer.

**Hooks, observed.** A probe plugin logged every hook OpenCode 1.19.3
invoked during a live session and a headless opencode serve driven
through POST /session/:id/summarize. server(), chat.message,
experimental.chat.system.transform, experimental.chat.messages.transform,
experimental.session.compacting, chat.params, chat.headers, config,
event and 	ool.definition all fired. The census is recorded in
`quickstart.md`.

**The loop, observed.** A real OpenCode session, a real Memoratum server and
worker, and a deterministic local chat stub: the plugin captured the
transcript at compaction, the worker ingested it, the dreaming step extracted
a fact from it, and a second session's recall injected that fact into the
model's context. The evidence is in `docs/DOGFOOD.md`.

**The gates.** `node --experimental-strip-types clients/opencode/test.ts`
passes; `pytest tests/test_docs_conformance.py` passes, including the two
new plugin tests; `ruff check` and `ruff format --check` pass.

Verified negatively: renaming `setup()` in the plugin fails both the
javascript suite and the python conformance test, so the gate is not
vacuous.
