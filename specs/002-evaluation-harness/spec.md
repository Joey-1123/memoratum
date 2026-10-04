# Feature Specification: Evaluation Harness — Scope, Cost, Latency and Faithfulness

**Feature Branch**: `002-evaluation-harness`

**Created**: 2026-10-04

**Status**: Draft

**Input**: User description: "The evaluation harness measures retrieval quality only and is structurally blind to scope bugs. Add scope isolation, token cost, latency and deterministic faithfulness as measured axes. Reject benchmarks aimed at a reasoning layer this project does not have."

## Context

Verified against the repository on `main` at `f1a297c`:

- `eval_longmemeval.py`, `eval_locomo.py`, `eval_mc10.py`, `eval_metrics.py`,
  `eval_compare.py`, `eval_datasets.py` exist and run offline.
- Committed results exist at `eval/RESULTS-n10.md`, `RESULTS-n50.md`,
  `RESULTS-bridge.md`, each with a reproducibility manifest recording data hash,
  seed, retrieval depths, modes and backend. Benchmark data is never uploaded.
- Every metric implemented is retrieval-only: `partial_recall`, `full_recall`,
  `mrr`, `session_ids_of`.
- Every harness ingests into a single `container_tag="bench"` with no project
  scoping.

That last point is the motivation. The two worst security findings in the previous
feature were:

1. `Mem0AddIn` had no `project_id` field, so an admin explicitly targeting a
   project received `200 OK` and the write landed in the **global NULL scope**.
2. No request model forbade unknown fields, so `projct_id` returned `201` and
   wrote to `project_id = None`.

A single unscoped corpus reports **perfect recall** for both. Recall is
monotonically indifferent to where data landed, so a leakage bug can only improve
the score. The metric family that exists cannot observe the failure mode that
actually occurred.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Operator can prove tenant isolation is measured (Priority: P1)

An operator evaluating Memoratum needs evidence that retrieval never crosses a
tenant boundary, expressed as a number they can track over time rather than a
pass/fail assertion buried in a test.

**Why this priority**: tenant isolation is the project's highest-severity
invariant, and today nothing measures it. Every other axis is secondary to
knowing the store does not leak.

**Independent Test**: Run the isolation harness against a synthetic two-project
corpus and observe the cross-scope leak count reported as a metric.

**Acceptance Scenarios**:

1. **Given** two projects with distinct corpora in one database, **When** a query
   runs scoped to project A, **Then** the number of returned hits originating
   from project B is reported, and a non-zero value fails the run.
2. **Given** a query scoped to a project, **When** project B's content ranks
   highest on lexical similarity, **Then** it is still excluded and the exclusion
   is counted rather than silently applied.
3. **Given** an unscoped query, **When** it runs, **Then** cross-project hits are
   expected and the metric distinguishes "no scope requested" from "scope
   enforced".
4. **Given** any leak, **When** the run completes, **Then** the offending hit's
   id, originating project and rank are recorded.

### User Story 2 - Operator can compare memory cost per query (Priority: P1)

An operator needs to know what a memory layer costs in context-window budget per
retrieval, to reason about whether it is affordable at their query volume.

**Why this priority**: token cost is the metric that makes a memory store
comparable against no memory at all. It is exactly measurable here and is a
differentiator no retrieval metric provides.

**Independent Test**: Run the cost harness and observe mean and p95 retrieved
characters per query against a committed baseline.

**Acceptance Scenarios**:

1. **Given** a retrieval returning N hits, **When** cost is reported, **Then**
   mean, p95 and total retrieved characters per query are produced.
2. **Given** two configurations returning the same gold sessions, **When** cost
   differs, **Then** the difference is attributable to redundant hits.
3. **Given** a cost regression against a committed baseline, **When** the harness
   runs in CI, **Then** it fails and reports the delta.

### User Story 3 - Operator can see where time is spent (Priority: P1)

An operator needs ingest, indexing and retrieval latency reported separately,
because a slow memory layer and a slow index have different fixes.

**Why this priority**: Phase 1 of the previous feature reduced search from 3.66 s
to 39.6 ms at 6,000 chunks with no latency metric in the harness. The next
regression would be invisible.

**Independent Test**: Run the latency harness and observe per-phase timings at
several corpus sizes.

**Acceptance Scenarios**:

1. **Given** a full ingest → index → retrieve cycle, **When** it is timed,
   **Then** ingest, embedding, indexing and retrieval are reported separately.
2. **Given** several corpus sizes, **When** measured, **Then** growth per size is
   reported so linear versus sub-linear behaviour is visible.
3. **Given** a regression against a committed baseline, **When** run in CI,
   **Then** the run fails and names the phase that regressed.

### User Story 4 - Operator can verify results are grounded (Priority: P2)

An operator needs evidence that returned content originates from the ingested
corpus rather than being synthesised, without depending on an external judge.

**Why this priority**: for a retrieval-backed store the ground truth is its own
corpus, so grounding is checkable deterministically. It is P2 because a grounded
answer built from wrong documents is still caught by the isolation and recall axes.

**Independent Test**: Run the grounding harness with a corpus containing
deliberately ungrounded content and observe it reported.

**Acceptance Scenarios**:

1. **Given** a returned hit, **When** it is compared against the ingested
   corpus, **Then** text absent from every ingested document is reported as
   ungrounded.
2. **Given** an ungrounded hit, **When** the run completes, **Then** the
   fraction of grounded hits is reported per query and in aggregate.
3. **Given** an optional external judge, **When** it is not configured, **Then**
   the deterministic result still reports and the harness does not fail for its
   absence.

### User Story 5 - Operator can reproduce any published number (Priority: P2)

An operator must be able to re-run any figure in `eval/` and get the same result,
which the existing harness already does for retrieval metrics and must extend to
the new ones.

**Why this priority**: Principle IV makes a committed measurement worthless if it
cannot be regenerated. Every new metric inherits this requirement.

**Independent Test**: Re-run an existing results file's command and diff the
manifest and metric values.

**Acceptance Scenarios**:

1. **Given** any results file under `eval/`, **When** its recorded command is
   re-run, **Then** the manifest matches on data hash, seed, modes and backend.
2. **Given** a new metric, **When** the manifest is written, **Then** it records
   the metric's configuration alongside the existing fields.
3. **Given** a provider or dependency that would make a run irreproducible,
   **When** the harness is designed, **Then** it is excluded from the gating
   path.

---

### Edge Cases

- A query scoped to a project whose corpus is empty, while another project has
  content that matches lexically.
- Retrieval returning duplicate hits for one source document (cost inflation and
  a misleading recall denominator).
- A hit whose text was truncated for the response, so byte comparison against the
  corpus would wrongly report it ungrounded.
- Two projects containing identical text, making id-based attribution ambiguous.
- A latency measurement polluted by first-call import or embedding warm-up.
- A cost baseline recorded on different hardware, making a CI comparison
  meaningless.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Retrieval harnesses MUST support project-scoped ingestion and
  scoped querying, so isolation is measurable rather than assumed.
- **FR-002**: The harness MUST report a cross-scope leak count and MUST fail a
  run when it is non-zero for a scoped query.
- **FR-003**: The harness MUST report mean, p95 and total retrieved characters
  per query as a token-cost proxy.
- **FR-004**: The harness MUST report ingest, embedding, indexing and retrieval
  latency separately, across at least three corpus sizes.
- **FR-005**: The harness MUST report the fraction of returned hits whose text is
  present in the ingested corpus, without requiring an external judge.
- **FR-006**: An external judge MAY be supported as an explicitly opt-in,
  non-gating extra requiring operator configuration.
- **FR-007**: Every reported figure MUST be reproducible from a recorded command
  and manifest, extending the existing manifest with the new metric configuration.
- **FR-008**: Latency and cost baselines MUST be committed, and comparisons MUST
  state the hardware they were measured on.
- **FR-009**: New metrics MUST reuse the existing harness structure, dataset
  adapters and results format rather than introducing a parallel framework.
- **FR-010**: Benchmark data MUST remain local; no harness may upload it.
- **FR-011**: The harness MUST run with SQLite only and no optional provider
  extra installed.

### Explicitly Out of Scope

- **HotPotQA** and **MuSiQue**: multi-hop reasoning over Wikipedia. Memoratum is a
  memory store, not a reasoning engine; answering these requires an LLM to
  perform the reasoning, so they would measure a layer this project does not
  have.
- **STATE-Bench**: requires an agent harness and an LLM to grade decisions.
- **BEAM**: deferred, not rejected. Adopt only if it runs fully offline with no
  provider key; otherwise it cannot gate CI under Principle IV.
- **MemoryBench as a dependency**: conflicts with Principle V (minimal
  dependencies) and Principle IV (a committed measurement must be re-runnable).
- Answer-level free-text correctness scoring as a gating metric.

### Key Entities

- **Scoped corpus**: synthetic or dataset-derived documents attributed to a
  project, org and container tag, forming the ground truth for isolation.
- **Isolation report**: per-query and aggregate cross-scope leak counts with
  offending hit attribution.
- **Cost report**: per-query and aggregate retrieved-character statistics.
- **Latency report**: per-phase timings across corpus sizes.
- **Grounding report**: per-query and aggregate fraction of hits traceable to the
  ingested corpus.
- **Manifest**: the reproducibility record, extended with metric configuration.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With two projects in one database, a scoped query reports a
  cross-scope leak count of **0**, and an intentionally leaked document produces a
  non-zero count that fails the run.
- **SC-002**: Every results file under `eval/` reports scope isolation, token
  cost, latency and grounding alongside the existing recall and MRR figures.
- **SC-003**: Re-running any committed results command reproduces its metric
  values and manifest on the recorded hardware.
- **SC-004**: A latency regression beyond the stated tolerance fails CI and names
  the phase that regressed.
- **SC-005**: A cost regression beyond the stated tolerance fails CI and reports
  the delta.
- **SC-006**: The harness runs to completion with SQLite only, no provider extra
  installed, and no benchmark data leaving the host.
- **SC-007**: Adding an ungrounded document to the corpus is detected and reported
  by the grounding metric.

## Assumptions

- The existing dataset adapters, results format and manifest structure are
  retained; this feature adds axes rather than replacing the framework.
- Synthetic corpora are acceptable for the isolation axis, since isolation is a
  property of the store rather than of any particular benchmark.
- Recall and MRR remain the primary retrieval-quality signals; scope containment
  is a gate, not a trade-off against them.
- Latency and cost tolerances are set per hardware profile, since absolute
  milliseconds are not portable.
- The deterministic grounding check is authoritative; any judge-based score is
  supplementary and never gates.

## Open Questions Deferred to Planning

- Exact token-cost proxy: characters versus a tokenizer. Recorded as a research
  decision; no tokenizer dependency is assumed.
- Latency tolerance bands and whether they gate CI or only report.
- Whether scope isolation is reported as a rate or an absolute count when the
  caller requests no scope.