---
description: "Task list for 002-evaluation-harness"
---

# Tasks: Evaluation Harness — Scope, Cost, Latency and Faithfulness

**Input**: Design documents from `/specs/002-evaluation-harness/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Test tasks ARE included. SC-001 and SC-007 explicitly require that a
deliberately introduced defect is *detected*, and `.specify/memory/constitution.md`
Principle IV requires a regression test for every bug fix. A measurement feature that
cannot fail is a feature that proves nothing, so every axis ships with a
falsifiability test.

**Already done on `main` (PR #47)** — do not redo:
- `recall@k` unit fix in `src/memoratum/eval_metrics.py` (slice before dedup)
- `eval/MIGRATION-metric-v1.md`, v0 annotations on `eval/RESULTS-*.md`
- This feature's spec, plan, research, data-model, contracts, quickstart

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to
- Include exact file paths

## Path Conventions

Single project: `src/`, `tests/`, `docs/`, `eval/` at repository root.

**Hard constraints carried from research.md / data-model.md — do not violate:**

- **Zero new runtime dependencies.** No tokenizer, no `pyarrow`, no `jsonschema`
  (Principle V). Python stdlib `sqlite3` only.
- **`search()` response shape must not change.** Scope attribution is derived by
  re-resolving ids, never returned.
- **Latency gates need `HashEmbedder` only.** No remote embedder inside a timed region.
- **Never average percentiles.** Aggregate raw samples, then take the percentile.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Confirm the change surface and stand up the baseline file. No new
dependencies, no project scaffolding — this is an existing Python project.

- [X] T001 Confirm `pyproject.toml`, `clients/ts/package.json` and `dashboard/package.json` are unmodified by this feature in `pyproject.toml` — zero new runtime deps (Principle V)
- [X] T002 [P] Create `eval/BASELINES.md` with columns: axis, figure, tolerance, corpus, seed, mode, hardware, command — hardware REQUIRED for any latency row (FR-008, invariant B1)
- [X] T003 [P] Create `tests/test_eval_axes.py` covering shared plumbing (written first, see Phase 2)

**Checkpoint**: Baseline file exists, dependency surface unchanged.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared axis plumbing in `src/memoratum/eval_axes.py`. **No user story
work can begin until this phase is complete** — isolation, cost, latency and grounding
all depend on attribution, the extended manifest, sampling, and gate evaluation.

- [X] T004 [P] Write failing test (serves US1, US4): `attribute_hit()` resolves a `chunk_<id>` hit to its `documents.project_id` via the indexed join `SELECT ... FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id = ?` in `tests/test_eval_axes.py` — write FIRST, watch it fail
- [X] T005 [P] Write failing test (serves US1, US4): `attribute_hit()` resolves memory hits via `memories.text` and fact hits via `subject predicate object` reconstruction in `tests/test_eval_axes.py`
- [X] T006 [P] Write failing test (serves US1, US4): hits carrying no derivable row are classified `unresolvable`, NOT silently skipped — an unresolved id is a failure, not a pass (invariant A1), in `tests/test_eval_axes.py`
- [X] T007 Implement `attribute_hit()` and `attribute_hits()` in `src/memoratum/eval_axes.py` — resolve `hit_id` → source row → `documents.project_id`/`org_id`; `document_id` may be `null` for memory/fact hits
- [X] T008 [P] Write failing test in `tests/test_eval_axes.py`: extended manifest emits `schema: "longmemeval-scoped-v2"` plus `scope_config`, `cost_config`, `latency_config`, `grounding_config` while preserving the seven original keys unchanged (invariant M1)
- [X] T009 Implement `build_manifest()` in `src/memoratum/eval_axes.py` — original keys (`schema`, `seed`, `requested_n`, `n`, `ks`, `modes`, `embedder`, `vector_store`, `data_sha256`) byte-identical so older committed results stay readable
- [X] T010 [P] Write failing test in `tests/test_eval_axes.py`: `latency_config.hardware` is REQUIRED when latency figures are present; a manifest lacking it fails validation (invariant B1, FR-008)
- [X] T011 Implement manifest validation in `src/memoratum/eval_axes.py` — reject a latency manifest with no `hardware` object
- [X] T012 [P] Write failing test in `tests/test_eval_axes.py`: `sample_ms()` discards `warmup` iterations then returns raw samples; median/p95 computed from raw samples by the reader, never averaged (invariant L3, L4)
- [X] T013 Implement `sample_ms(fn, samples, warmup)` in `src/memoratum/eval_axes.py` using `time.perf_counter` — must discard `warmup` iterations BEFORE sampling, else the first call pays FTS5 tokenizer setup
- [X] T014 [P] Write failing test in `tests/test_eval_axes.py`: a figure that could not be recorded is `null`, never `0`, and `null` FAILS the gate (invariant M3)
- [X] T015 Implement `Gate` / `Check` and `evaluate_gate()` in `src/memoratum/eval_axes.py` — `status` is `pass`/`fail`; a `null` figure yields `fail`
- [X] T016 [P] Write failing test in `tests/test_eval_axes.py`: `load_baseline()` rejects a latency row whose `hardware` is absent, and a missing baseline yields `null` which FAILS the gate (invariants B1, B3)
- [X] T017 Implement `load_baseline()` and `compare_to_baseline()` in `src/memoratum/eval_axes.py` — missing baseline is `null` and must fail, never silently pass
- [X] T018 Implement shared CLI helpers in `src/memoratum/eval_axes.py`: `--out-md`, `--out-json`, `--seed`, and the exit-code enum (0 pass / 1 gate failure / 2 self-check failed / 3 bad input)
- [X] T019 [P] Implement `build_scoped_corpus()` in `src/memoratum/eval_axes.py` — ≥2 projects, lexically OVERLAPPING content by design, `custom_id` deliberately REUSED across projects to exercise the real collision path
- [X] T020 [P] Write failing test in `tests/test_eval_axes.py`: `build_scoped_corpus()` asserts the expected per-project document count after ingest and never infers it from a zero leak count (invariant S3)
- [X] T021 [P] Write failing test in `tests/test_eval_axes.py`: byte-identical content in two projects yields two distinct document rows with distinct ids, so text can never attribute scope (invariant A2)

**Checkpoint**: `eval_axes.py` importable, all foundational tests green. US1–US5 can now begin.

---

## Phase 3: User Story 1 — Operator can prove tenant isolation is measured (Priority: P1) 🎯 MVP

**Goal**: Report cross-scope leak counts for project-scoped retrieval, and fail the
run when non-zero.

**Independent Test**: Run against a synthetic two-project corpus and observe the
cross-scope leak count reported as a metric.

**Contract**: `contracts/eval-axes-v1.md` §1.1 (`memoratum.eval_isolation`), §2.2

### Tests for User Story 1 ⚠️ write FIRST, watch them fail

- [ ] T022 [P] [US1] Failing test: two projects, scoped query to project A returns `leaked_hits == 0` and `scoped_queries > 0` in `tests/test_eval_isolation.py`
- [ ] T023 [P] [US1] Failing test: project B's content ranking highest on lexical similarity is still excluded from a project-A query, and the exclusion is COUNTED not silently applied, in `tests/test_eval_isolation.py`
- [ ] T024 [P] [US1] Failing test: an unscoped query populates `unscoped_queries` and `unscoped_cross_scope_hits` (>0) and these appear in NEITHER the gate numerator NOR the denominator (invariant I2), in `tests/test_eval_isolation.py`
- [ ] T025 [P] [US1] Failing test: `returned_hits` is always emitted next to `leak_rate` per query (invariant I3 note) in `tests/test_eval_isolation.py`
- [ ] T026 [P] [US1] Failing test: a non-zero `leaked_hits` produces gate status `fail` and exit code 1 (FR-002, invariant I1) in `tests/test_eval_isolation.py`
- [ ] T027 [US1] Failing test: `--require-clean` injects a document that SHOULD leak, the harness DETECTS it, and exits 0 — detection succeeded. Exit 2 means the metric is broken and is WORSE than the leak (SC-001, invariant I4), in `tests/test_eval_isolation.py`
- [ ] T028 [P] [US1] Failing test: each recorded leak carries `hit_id`, `document_id`, `originating_project_id`, `requested_project_id`, `rank` (SC-004) in `tests/test_eval_isolation.py`
- [ ] T029 [P] [US1] Failing test: `scope_config.projects` has ≥2 entries and `expected_documents` is present in the manifest (contracts schema `minItems: 2`) in `tests/test_eval_isolation.py`

### Implementation for User Story 1

- [ ] T030 [P] [US1] Implement `ingest_scoped()` in `src/memoratum/eval_axes.py` — pass `project_id` on BOTH `db.create_document` and `search`; `process_all` is imported from `memoratum.ingest`, NOT `memoratum.db`
- [ ] T031 [P] [US1] Implement `classify_scope()` in `src/memoratum/eval_isolation.py` — bucket each query as scoped or unscoped; `project_id=None` genuinely means "caller requested no scope"
- [ ] T032 [US1] Implement `evaluate_isolation()` in `src/memoratum/eval_isolation.py` — emit `scoped_queries`, `leaked_hits`, `leaked_queries`, `returned_hits`, `leak_rate`, `query_leak_rate`, `unscoped_queries`, `unscoped_cross_scope_hits`, `per_query`, `leaks_detail`
- [ ] T033 [US1] Implement `gate_isolation()` in `src/memoratum/eval_isolation.py` — gate on the ABSOLUTE COUNT `leaked_hits == 0`; `leak_rate` and `query_leak_rate` are trend-only and NEVER gate (invariants I1, I3)
- [ ] T034 [US1] Implement the `--require-clean` self-check in `src/memoratum/eval_isolation.py` — inject a known leak, assert detection, exit 2 if undetected (invariant I4)
- [ ] T035 [US1] Implement CLI `main()` in `src/memoratum/eval_isolation.py` with flags `--projects` (default 3), `--sessions-per-project` (40), `--queries` (40), `--k` (5,10), `--modes` (hybrid,documents), `--seed` (42), `--require-clean`, `--out-md`, `--out-json`
- [ ] T036 [US1] Emit `eval/RESULTS-isolation.md` and `eval/isolation.json` by running the axis; record `scope_config` in the manifest

**Checkpoint**: SC-001 satisfied — two projects report 0 leaks, an injected leak fails the run. **MVP complete.**

---

## Phase 4: User Story 2 — Operator can compare memory cost per query (Priority: P1)

**Goal**: Report mean, p95 and total retrieved characters per query as a token-cost
proxy, and gate cost regressions against a committed baseline.

**Independent Test**: Run the cost harness and observe mean and p95 retrieved
characters per query against a committed baseline.

**Contract**: `contracts/eval-axes-v1.md` §1.2, §2.3

### Tests for User Story 2 ⚠️ write FIRST, watch them fail

- [ ] T037 [P] [US2] Failing test: a retrieval returning N hits reports `retrieved_chars_mean`, `retrieved_chars_p95`, `retrieved_chars_total` (FR-003) in `tests/test_eval_cost.py`
- [ ] T038 [P] [US2] Failing test: two configs returning the SAME gold sessions but differing cost attribute the difference to `redundant_hits` (US2 scenario 2) in `tests/test_eval_cost.py`
- [ ] T039 [P] [US2] Failing test: `--cost-unit bytes` is REJECTED — bytes is not an offered unit (invariant C1: bytes inflate 1.1–3x on non-ASCII with nothing visible in the report) in `tests/test_eval_cost.py`
- [ ] T040 [P] [US2] Failing test: `chars_per_ws_token` is MEASURED from the corpus and written to the manifest, never hardcoded; a hardcoded 4.0 must be absent (invariant C2) in `tests/test_eval_cost.py`
- [ ] T041 [P] [US2] Failing test: `token_estimate_mean` carries the caveat string that it is valid only as a ratio between runs on the same corpus (invariant C3) in `tests/test_eval_cost.py`
- [ ] T042 [P] [US2] Failing test: the gated figure is IDENTICAL whether or not a tokenizer is configured — tokenizer presence must never change the gated metric (invariant C6, FR-007) in `tests/test_eval_cost.py`
- [ ] T043 [US2] Failing test: a cost regression beyond tolerance fails the gate AND reports a non-zero `delta` — a failure with no delta does not satisfy SC-005 in `tests/test_eval_cost.py`
- [ ] T044 [P] [US2] Failing test: a missing baseline yields exit code 2, NOT a silent pass (invariants B3, M3) in `tests/test_eval_cost.py`

### Implementation for User Story 2

- [ ] T045 [P] [US2] Implement `measure_chars_per_ws_token()` in `src/memoratum/eval_cost.py` — measure on the corpus, record in `cost_config`
- [ ] T046 [P] [US2] Implement `count_redundant_hits()` in `src/memoratum/eval_cost.py` — hits duplicating an already-counted source row, resolved by `Attribution`
- [ ] T047 [US2] Implement `evaluate_cost()` in `src/memoratum/eval_cost.py` — emit all `cost_config` and `cost` fields per contracts §2.3
- [ ] T048 [US2] Implement `gate_cost()` in `src/memoratum/eval_cost.py` — fail when `retrieved_chars_mean > baseline × (1 + tolerance)`, reporting `delta`
- [ ] T049 [US2] Implement CLI `main()` in `src/memoratum/eval_cost.py` with `--data`, `--n` (20), `--seed` (42), `--k`, `--baseline`, `--cost-unit` (chars|ws_tokens), `--measure-ratio`, `--out-md`, `--out-json`
- [ ] T050 [US2] Load the corpus via a STREAMING reader for the ratio probe in `src/memoratum/eval_cost.py` — `eval_datasets.load_records` reads the 277 MB file as one string and OOMs on small hosts (see quickstart §1.4)
- [ ] T051 [US2] Verify the retrieval budget stays `max(2*max(ks), 10)` and is NOT inflated in `src/memoratum/eval_longmemeval.py` — the budget existed to feed the old metric; inflating it now would mask a real signal (invariant C5)
- [ ] T052 [US2] Emit `eval/RESULTS-cost.md` and `eval/cost.json`; record the baseline row with corpus, seed, mode and hardware in `eval/BASELINES.md`

**Checkpoint**: US1 and US2 independently functional.

---

## Phase 5: User Story 3 — Operator can see where time is spent (Priority: P1)

**Goal**: Report ingest, embedding, indexing and retrieval latency separately across
≥3 corpus sizes, and gate regressions while naming the regressed phase.

**Independent Test**: Run the latency harness and observe per-phase timings at
several corpus sizes.

**Contract**: `contracts/eval-axes-v1.md` §1.3, §2.4

### Tests for User Story 3 ⚠️ write FIRST, watch them fail

- [ ] T053 [P] [US3] Failing test: `--samples 5` is REJECTED — the floor is 20; at n=5 a 1.0x gate needs a +69% band and cannot detect anything under a 70% regression (invariant L2) in `tests/test_eval_latency.py`
- [ ] T054 [P] [US3] Failing test: a ladder with fewer than 3 corpus sizes is REJECTED (FR-004) in `tests/test_eval_latency.py`
- [ ] T055 [P] [US3] Failing test: `samples_ms` holds RAW samples and percentiles are computed from them, never averaged across summaries (invariant L3) in `tests/test_eval_latency.py`
- [ ] T056 [P] [US3] Failing test: `us_per_chunk` RISING across the ladder is flagged as a regression to O(n), FALLING is sub-linear and passes (invariant L1) in `tests/test_eval_latency.py`
- [ ] T057 [P] [US3] Failing test: a ladder crossing `PREFILTER_MIN_CANDIDATES = 512` is flagged, because that step measures an ALGORITHM SWITCH rather than a size change (invariant L7) in `tests/test_eval_latency.py`
- [ ] T058 [P] [US3] Failing test: a latency regression names the regressed phase in `gate.checks[].phase` (SC-004) in `tests/test_eval_latency.py`
- [ ] T059 [P] [US3] Failing test: a baseline row lacking `hardware` is rejected with exit code 2 (invariants B1, L8, FR-008) in `tests/test_eval_latency.py`
- [ ] T060 [US3] Failing test: `prefilter_engaged` is ASSERTED via `set_trace_callback` — the same technique as `tests/test_search_perf.py` — and a `null` above the prefilter threshold is a defect (invariant L7) in `tests/test_eval_latency.py`

### Implementation for User Story 3

- [ ] T061 [P] [US3] Implement `assert_prefilter_engaged()` in `src/memoratum/eval_latency.py` using `sqlite3.Connection.set_trace_callback`
- [ ] T062 [P] [US3] Implement `build_ladder()` in `src/memoratum/eval_latency.py` — ≥3 sizes, all on ONE side of `PREFILTER_MIN_CANDIDATES = 512`, or pin `MEMORATUM_SEARCH_PREFILTER_MIN` per size and record it
- [ ] T063 [US3] Implement `time_phase()` in `src/memoratum/eval_latency.py` — wraps `sample_ms` and records `samples_ms`, `median_ms`, `p95_ms`, `us_per_chunk`, `prefilter_engaged`
- [ ] T064 [US3] Implement `evaluate_latency()` in `src/memoratum/eval_latency.py` — all FOUR phases reported separately: ingest, embed, index, retrieve (FR-004)
- [ ] T065 [US3] Implement `gate_latency()` in `src/memoratum/eval_latency.py` — primary gate `us_per_chunk(4N) < 1.5 × us_per_chunk(N)`; secondary smoke bound `median_of_30 ≤ baseline × 1.40`
- [ ] T066 [US3] Verify NO remote embedder is inside any timed region in `src/memoratum/eval_latency.py` — network jitter (100 ms ± 80 ms) dwarfs everything measured (invariant L5, FR-011)
- [ ] T067 [US3] Verify harness bookkeeping stays OUTSIDE timed regions in `src/memoratum/eval_latency.py` — the naive grounding scan costs 51.6 ms/query against a 13 ms retrieval (invariant L6)
- [ ] T068 [US3] Implement CLI `main()` in `src/memoratum/eval_latency.py` with `--ladder` (100,400,1600,6400), `--samples` (30), `--warmup` (5), `--pin-prefilter`, `--phases`, `--baseline`, `--out-md`, `--out-json`
- [ ] T069 [US3] Emit `eval/RESULTS-latency.md` and `eval/latency.json`; record baseline rows WITH hardware in `eval/BASELINES.md`

**Checkpoint**: US1, US2, US3 independently functional.

---

## Phase 6: User Story 4 — Operator can verify results are grounded (Priority: P2)

**Goal**: Report the fraction of returned hits traceable to the ingested corpus,
deterministically, with no external judge.

**Independent Test**: Run the grounding harness with a corpus containing deliberately
ungrounded content and observe it reported.

**Contract**: `contracts/eval-axes-v1.md` §1.4, §2.5

### Tests for User Story 4 ⚠️ write FIRST, watch them fail

- [ ] T070 [P] [US4] Failing test: a grounded corpus reports `grounded_fraction == 1.0` (FR-005) in `tests/test_eval_grounding.py`
- [ ] T071 [P] [US4] Failing test: text absent from every ingested row is reported ungrounded with `hit_id`, `hit_kind`, `reason` in `tests/test_eval_grounding.py`
- [ ] T072 [P] [US4] Failing test: chunk hits are graded against `chunks.text` and NEVER against `documents.content` (invariant G1) in `tests/test_eval_grounding.py`
- [ ] T073 [P] [US4] Failing test: fact hits are graded against reconstructed `subject predicate object`, NOT a global rule — `_fact_text` output is absent from the corpus by construction (invariant G2) in `tests/test_eval_grounding.py`
- [ ] T074 [P] [US4] Failing test: memory hits are graded against `memories.text` for that id (invariant G2) in `tests/test_eval_grounding.py`
- [ ] T075 [P] [US4] Failing test: `--inject-ungrounded` is DETECTED and exits 0; undetected exits 2 (SC-007) in `tests/test_eval_grounding.py`
- [ ] T076 [P] [US4] Failing test: `--judge none` does NOT fail the run and `judge` is `null` (FR-006, invariant G6) in `tests/test_eval_grounding.py`
- [ ] T077 [P] [US4] Failing test: Tier 2 diagnostics NEVER affect the gate (invariant G3) in `tests/test_eval_grounding.py`
- [ ] T078 [P] [US4] Failing test: `by_kind[kind].fraction` is `null` when `checked == 0`, NEVER `0.0` — an unexercised metric is not a passing metric (invariant M3) in `tests/test_eval_grounding.py`
- [ ] T079 [P] [US4] Failing test: the corpus text index is built ONCE per corpus, not per hit — assert build count (invariant G4, measured 51.6 ms/query vs 0.78 ms/query) in `tests/test_eval_grounding.py`

### Implementation for User Story 4

- [ ] T080 [P] [US4] Implement `normalize_text()` in `src/memoratum/eval_grounding.py` — `unicodedata.normalize("NFKC", …)`, whitespace collapse, casefold
- [ ] T081 [P] [US4] Implement `CorpusTextIndex` in `src/memoratum/eval_grounding.py` — line-keyed index built once per corpus; `contains(text)` in O(1)-ish
- [ ] T082 [US4] Implement `ground_hit()` in `src/memoratum/eval_grounding.py` — resolve via `attribute_hit()`, then confirm text matches that row under THAT KIND's rule (`rule_by_kind`)
- [ ] T083 [US4] Implement `evaluate_grounding()` in `src/memoratum/eval_grounding.py` — emit `hits_checked`, `grounded_hits`, `ungrounded_hits`, `grounded_fraction`, `by_kind`, `ungrounded`
- [ ] T084 [US4] Implement `gate_grounding()` in `src/memoratum/eval_grounding.py` — fail when `grounded_fraction < min_grounded_fraction` (default 1.0)
- [ ] T085 [P] [US4] Implement `tier2_diagnostics()` in `src/memoratum/eval_grounding.py` — LCS ratio, 3-gram shingle overlap, FTS5 `snippet()`/`highlight()`; result is diagnostic-only and MUST NOT gate
- [ ] T086 [US4] Implement CLI `main()` in `src/memoratum/eval_grounding.py` with `--data`, `--n`, `--seed`, `--inject-ungrounded`, `--judge` (none|external), `--tier2`, `--min-grounded-fraction` (1.0), `--out-md`, `--out-json`
- [ ] T087 [US4] Emit `eval/RESULTS-grounding.md` and `eval/grounding.json`

**Checkpoint**: US1–US4 independently functional.

---

## Phase 7: User Story 5 — Operator can reproduce any published number (Priority: P2)

**Goal**: Any figure under `eval/` is regenerable from a recorded command and
manifest, and a mismatched comparison fails rather than warns.

**Independent Test**: Re-run an existing results file's command and diff the manifest
and metric values.

**Contract**: `contracts/eval-axes-v1.md` §1.5, §3.4

### Tests for User Story 5 ⚠️ write FIRST, watch them fail

- [ ] T088 [P] [US5] Failing test: re-running the same command twice produces an IDENTICAL manifest and identical metric values (FR-007, SC-003) in `tests/test_eval_compare.py`
- [ ] T089 [P] [US5] Failing test: `eval_compare` exits NON-ZERO on embedder mismatch, listing the mismatched keys — it must FAIL, not warn (invariant M2, research D8) in `tests/test_eval_compare.py`
- [ ] T090 [P] [US5] Failing test: `eval_compare` exits non-zero on `vector_store`, `modes`, `seed`, `data_sha256` and `n` mismatch in `tests/test_eval_compare.py`
- [ ] T091 [P] [US5] Failing test: `--scope both` populates BOTH the scoped and unscoped buckets on a real dataset run (invariant I2) in `tests/test_eval_longmemeval_repro.py`
- [ ] T092 [P] [US5] Failing test: every emitted results file contains the command needed to regenerate it (SC-003) in `tests/test_eval_docs_conformance.py`

### Implementation for User Story 5

- [ ] T093 [P] [US5] Add `--project-count`, `--scope` (scoped|unscoped|both), `--latency-baseline`, `--cost-baseline` flags to `src/memoratum/eval_longmemeval.py` (FR-001, SC-002)
- [ ] T094 [US5] Make `eval_compare` hard-fail on manifest mismatch in `src/memoratum/eval_compare.py` — exit non-zero listing mismatched keys; a confident number across mismatched manifests is worse than no number
- [ ] T095 [US5] Emit `eval/RESULTS.md` showing all four axes alongside recall and MRR for the same run (SC-002) from `src/memoratum/eval_compare.py`
- [ ] T096 [US5] Record the exact regeneration command inside every emitted results Markdown file in `src/memoratum/eval_axes.py`
- [ ] T097 [US5] Document the four axes, gating semantics, hardware requirement and the chars-not-tokens unit in `docs/EVALUATION.md` — same change as the behaviour (constitution docs rule)
- [ ] T098 [US5] Document the v0 → v1 metric change and the non-comparability of pre-2026-10-04 `R@k` figures in `docs/EVALUATION.md`, linking `eval/MIGRATION-metric-v1.md`
- [ ] T099 [US5] Regenerate `eval/RESULTS-*.md` for the corrected metric and record new baseline rows in `eval/BASELINES.md`

**Checkpoint**: All five stories functional and every published figure regenerable.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T100 [P] Add the four axis commands to the CI gate list in `.github/workflows/` — each must fail the build on regression
- [ ] T101 [P] Run the full gate suite verbatim from `.specify/memory/constitution.md` and record each result in the PR body: telemetry check, mem0 subset with `--no-cov`, `ruff check`, `ruff format --check`, `pytest -q`, `npm test --prefix clients/ts`, `node --check`, dashboard `npm ci` + build
- [ ] T102 [P] Confirm coverage floor not lowered and no new suppressions: `uv run python scripts/check_coverage_floor.py` and `uv run python scripts/check_no_new_suppressions.py`
- [ ] T103 Verify SQLite-only operation with NO provider extra installed (SC-006): run each axis where the provider extras are absent, confirm no `MEMORATUM_EMBEDDING_PROVIDER` is read, no API key is touched and no outbound socket opens, then run `uv run python scripts/check_no_telemetry.py`
- [ ] T104 [P] Verify no benchmark data leaves the host: `data/` still holds only `download.log` (FR-010)
- [ ] T105 [P] Delete any test that cannot fail; audit `tests/test_eval_isolation.py`, `tests/test_eval_cost.py`, `tests/test_eval_latency.py` and `tests/test_eval_grounding.py`. Every axis keeps its falsifiability test
- [ ] T106 [P] Validate against `specs/002-evaluation-harness/quickstart.md` §8 — every SC row has an executed command and an observed result
- [ ] T107 Record Phase C (HotPotQA evidence retrieval) as NOT started in `specs/002-evaluation-harness/tasks.md` — deferred until US1–US5 are merged and green

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 — **BLOCKS all user stories**
- **User Stories (Phases 3–7)**: All depend on Phase 2. US1–US4 can then run in
  parallel; US5 depends on the axes existing
- **Polish (Phase 8)**: Depends on all desired stories

### User Story Dependencies

- **US1 (P1)**: after Phase 2 — no story dependencies. **MVP.**
- **US2 (P1)**: after Phase 2 — independent of US1, but ordered after it because
  tenant isolation is the highest-severity invariant
- **US3 (P1)**: after Phase 2 — independent of US1/US2
- **US4 (P2)**: after Phase 2 — independent; shares `Attribution` with US1
- **US5 (P2)**: after Phase 2, but effectively last — it aggregates the other axes

### Within Each User Story

- Tests written and **watched fail** before implementation
- Models → services → CLI → emit artifacts
- Story complete before moving to the next priority

### Parallel Opportunities

- T002, T003 in Phase 1
- T004–T006, T008, T010, T012, T014, T016, T020, T021 in Phase 2 (distinct test
  functions and distinct helpers)
- All test tasks within a story marked `[P]`
- T061, T062 in Phase 5
- T080, T081, T085 in Phase 6
- T093 in Phase 7
- T100–T102, T104, T105 in Phase 8

---

## Parallel Example: User Story 1

```bash
# Launch all US1 tests together (they must fail first):
Task: "Failing test: two projects, scoped query to project A returns leaked_hits == 0 ... in tests/test_eval_isolation.py"
Task: "Failing test: project B's content ranking highest is still excluded and COUNTED ... in tests/test_eval_isolation.py"
Task: "Failing test: an unscoped query populates unscoped_queries ... in tests/test_eval_isolation.py"
Task: "Failing test: a non-zero leaked_hits produces gate status fail and exit code 1 ... in tests/test_eval_isolation.py"
Task: "Failing test: each recorded leak carries hit_id, document_id, originating_project_id ... in tests/test_eval_isolation.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1: Setup
2. Phase 2: Foundational — **blocks everything**
3. Phase 3: User Story 1 (isolation)
4. **STOP and VALIDATE**: `uv run python -m memoratum.eval_isolation --require-clean`
5. This is the highest-value increment: it makes the project's highest-severity
   invariant observable, which nothing currently does.

### Incremental Delivery

1. Setup + Foundational → foundation ready
2. US1 isolation → validate → **MVP**
3. US2 cost → validate → committed baselines exist
4. US3 latency → validate → regressions detectable in CI
5. US4 grounding → validate → provenance provable
6. US5 reproducibility → validate → every figure regenerable
7. Polish → gates green

### Parallel Team Strategy

After Phase 2 completes:
- Developer A: US1 (isolation)
- Developer B: US3 (latency) — largest independent surface
- Developer C: US4 (grounding)
- US2 and US5 last, since US5 aggregates the others

---

## Notes

- [P] = different files, no dependencies
- [Story] label maps each task to a user story for traceability
- Each user story is independently completable and testable
- **Verify tests fail before implementing** — this is a measurement feature; an
  unverified metric is worse than no metric
- Commit after each task or logical group
- Stop at any checkpoint to validate the story independently
- Avoid: vague tasks, same-file conflicts, cross-story dependencies that break
  independence
- Every axis ships a **falsifiability test**. If a metric cannot fail, delete it.