# Implementation Plan: Evaluation Harness — Scope, Cost, Latency and Faithfulness

**Branch**: `002-evaluation-harness` | **Date**: 2026-10-04
**Spec**: [`spec.md`](./spec.md)
**Research**: [`research.md`](./research.md)

**Input**: Feature specification from `specs/002-evaluation-harness/spec.md`

## Summary

Extend the existing retrieval-only evaluation harness with four measured axes —
**scope isolation**, **token cost**, **latency** and **deterministic grounding** —
and gate the first two in CI.

The motivation is structural, not incremental. The two worst security findings in
feature 001 were a write landing in the global NULL scope (`Mem0AddIn` had no
`project_id`) and a typo'd field silently accepted (`projct_id` → `201`,
`project_id = None`). A single unscoped corpus reports **perfect recall** for both:
recall is monotonically indifferent to where data landed, so a leakage bug can only
*improve* the score. The metric family that exists cannot observe the failure mode
that actually occurred. This feature builds the observer.

Technical approach: reuse the existing harness structure wholesale — same
`python -m memoratum.eval_*` CLI, same temp-per-question SQLite DB, same
`--out-md`/`--out-json`, same manifest, same dataset adapters (FR-009). Scope
attribution re-resolves hit ids through the indexed join that
`eval_longmemeval.hit_session()` already performs, so **the public search response
shape does not change**. Grounding is provenance-by-row-identity, not text overlap,
because measurement showed token-set containment cannot separate grounded text
from fabricated text. Every new figure lands in the extended manifest so it stays
reproducible under Principle IV.

Phase 0 additionally surfaced a **defect in already-committed results**: the
existing harness fetches 20 *hits* while `R@k` counts *distinct sessions*
(`session_ids_of` dedupes before slicing), so `R@k` beyond ~3 is not measuring
k-deep retrieval. The symptom is visible in `eval/RESULTS-n10.md`, where
`partial-R@5 == partial-R@10` to three decimals. This is fixed forward-only;
existing results are a historical record and are **not** silently restated.

## Technical Context

**Language/Version**: Python 3.12.14 (project floor declared in `pyproject.toml`)

**Primary Dependencies**: none added. `sqlite3` from the standard library only for
the new axes. `pyarrow` is required **only** for a one-time offline dataset
conversion, never at eval time, and is not a declared dependency (see Scope
Reconciliation).

**Storage**: SQLite (temp directory per question, as the existing harness already
does). No new storage.

**Testing**: `pytest` with `addopts = "--cov"`, `fail_under = 74` branch-aware.
Baseline 77.25%.

**Target Platform**: Linux/macOS developer and CI machines; offline. SQLite only,
no provider extra installed (FR-011).

**Project Type**: library + CLI evaluation harness.

**Performance Goals**: the harness itself must not be the bottleneck. Measured
grounding cost budget: ≤1 ms/query for the text index build and lookup, versus
51.6 ms/query for the naive nested scan it replaces (66x). Latency measurement uses
`HashEmbedder` only, so no network call sits inside a timed region.

**Constraints**:
- Absolute latency is hardware-bound; only ratios and per-unit costs travel (D3).
- The chars↔token conversion is a **calibration constant recorded in the manifest**,
  never a hardcoded literal (D2).
- Benchmark data never leaves the host (FR-010); `data/` holds only `download.log`.
- Latency gates require n ≥ 30 samples; below n = 20 the gate is theatre (D3).

**Scale/Scope**: ≥3 corpus sizes for the latency ladder (FR-004); 2+ projects for
the isolation axis; no change to runtime code paths outside `eval_*.py`.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle / Gate | Requirement | How this plan satisfies it | Status |
|---|---|---|---|
| **I. Self-Hosted, Local-First** | Runs on a single machine, no external service; SQLite default; no telemetry | All four axes are pure `sqlite3` + stdlib. Hash embedder only. No network call in any gated path. `scripts/check_no_telemetry.py` unchanged and must pass | PASS |
| **II. Hard Isolation** | Reads/writes constrained by container tag, org, project | This feature *measures* Principle II rather than changing it. Isolation is the highest-priority axis because two P0 findings violated it. A test must **deliberately leak a document** and prove the harness detects it (SC-001) — otherwise the metric is unfalsifiable | PASS |
| **III. Durable Work** | Single commit boundary; leases | Not touched. The harness writes only to temp DBs and result files | PASS |
| **IV. Evidence Before Assertion** | Real code paths, no mocks standing in for asserted behaviour; every bug fix ships a failing-first regression test; performance claims backed by real measurement | Isolation is measured against **real** `search()` and real two-project ingestion — no mock scope filter. The grounding metric is proven falsifiable by injecting an ungrounded document (SC-007). Latency bands derive from a 2,000-resample bootstrap on the real retrieval path, not from intuition. The `R@k` denominator defect is fixed with a test that fails before the fix | PASS |
| **V. Minimal Dependencies** | Minimal core runtime deps; providers behind optional extras | **Zero** new runtime dependencies. No tokenizer: the chars-per-token ratio is *measured* and recorded. `pyarrow` is a manual offline step only, never imported by the harness | PASS |
| **Docs same change** | `docs/` updated with the behaviour | `docs/EVALUATION.md` gains the four axes, the gating semantics, and the hardware requirement. Conformance test required, per the `docs/SECURITY.md` precedent | PASS |
| **Quality gates** | The seven verbatim commands | Every one must pass. Note `--no-cov` is required for narrow subset runs, since the coverage floor is a full-suite property | PASS |
| **Governance** | Branch → PR → merge commit; PR states what was verified and how | Branch `002-evaluation-harness`. No squash or rebase | PASS |

**Complexity Tracking**: no violations. One item requires a **scope decision**
rather than a complexity waiver — see below.

## Scope Reconciliation — requires explicit approval

Phase 0 research contradicts two lines of the spec's *Explicitly Out of Scope*
section. I am not resolving this silently.

### BEAM — the spec's own conditional is now answered, no conflict

The spec wrote: *"BEAM: deferred, not rejected. Adopt only if it runs fully
offline with no provider key; otherwise it cannot gate CI under Principle IV."*

Research verified the condition is **false**: grading is 100% LLM-judge
(`gpt-4.1-mini`, all 10 of 10 scorers, no deterministic branch), the judge prompt
is explicitly paraphrase-tolerant so re-runs drift, and `initialize_models()`
pulls further HuggingFace models plus `nltk.download`. Therefore BEAM is
**rejected** by the spec's own stated rule. No amendment needed; the deferred
conditional is resolved to "rejected". The retrieval-only subset that *is*
deterministically reachable (6 of 10 abilities via `source_chat_ids`) is deferred
and must be labelled non-official, because it is not BEAM's metric.

### HotPotQA — a genuine conflict, needs your decision

The spec rejects HotPotQA and MuSiQue: *"multi-hop reasoning over Wikipedia...
answering these requires an LLM to perform the reasoning, so they would measure a
layer this project does not have."*

**That reasoning is correct and I endorsed it. It is also, on inspection, aimed at
the wrong measurement.** If the metric is *evidence retrieval* — does the system
return the gold `supporting_facts` paragraphs — no LLM is required. HotPotQA ships
those facts and a pure-stdlib scorer (`normalize_answer`, `exact_match_score`,
`f1_score`, `update_sp`, joint as elementwise product). The spec's stated reason
does not apply to that metric.

I dismissed HotPotQA in conversation before commissioning the research, and that
dismissal was wrong. I had already reframed correctness as retrieval
precision/recall, then failed to check whether HotPotQA's *evidence* was
measurable that way. It is.

What it would add that the current harness structurally cannot:

- **Sentence-level** gold evidence, not session-level, giving strict partial credit.
- Explicit multi-hop `bridge`/`comparison` chains that session-recall metrics
  cannot isolate.
- A non-conversational corpus that actually exercises FTS5 lexical matching against
  Wikipedia titles instead of chat filler.

Verified: `hotpotqa/hotpot_qa` `fullwiki` validation = 28,041,820 bytes, 7,405 rows,
public, ungated. Honest caveat: indexing the union of provided contexts (~74K
paragraphs) is *easier* than true full-wiki (~5M articles, separate multi-GB dump),
so it is multi-hop evidence recall under a ~74K-paragraph distractor load. The
`distractor` config does **not** force out-of-window retrieval, so `fullwiki` is the
correct config. Licence is CC BY-SA 4.0, so ShareAlike attaches to any normalised
extract — the same reason the existing 277 MB LongMemEval file is **not** committed.

**Decision required.** Three options:

1. **Keep the spec as written** — HotPotQA stays out of scope. The four axes ship
   unchanged. Zero scope risk.
2. **Amend the spec** to admit HotPotQA scoped explicitly to *evidence recall, not
   answer correctness*, and ship it as a separable Phase C after the four axes are
   merged and green.
3. **Amend now** and fold it into this feature. Higher risk; mixes a new dataset
   adapter and a licence-attribution obligation into an already broad change.

**Recommendation: option 2.** The four axes are the actual request and they are
independent of dataset choice; HotPotQA should not be able to block or destabilise
them. Until you choose, `tasks.md` will treat Phase C as out of scope, and I will
not implement it.

## Project Structure

### Documentation (this feature)

```text
specs/002-evaluation-harness/
├── spec.md              # input specification
├── plan.md              # This file
├── research.md          # Phase 0 output — decisions D1..D8 with measurements
├── data-model.md        # Phase 1 output — entities and their invariants
├── quickstart.md        # Phase 1 output — runnable validation
├── contracts/
│   ├── eval-axes-v1.md          # CLI surface + JSON result contract
│   └── eval-axes-v1.schema.json # machine-checkable schema for the above
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

New modules follow the existing `eval_*.py` convention exactly — one concern per
module, argparse `main()`, `python -m memoratum.<module>`.

```text
src/memoratum/
├── eval_axes.py          # NEW — shared axis plumbing: manifest extension,
│                         #   scoped ingest, id→row attribution, sampling helpers,
│                         #   baseline load/compare, gate evaluation
├── eval_isolation.py     # NEW — FR-001, FR-002, SC-001
├── eval_cost.py          # NEW — FR-003, D2, D7
├── eval_latency.py       # NEW — FR-004, D3
├── eval_grounding.py     # NEW — FR-005, FR-006, D4
├── eval_longmemeval.py   # MODIFIED — FR-001 scoping + D7 retrieval budget fix
├── eval_metrics.py       # MODIFIED — D7: declare one unit for cost and recall
├── eval_compare.py       # MODIFIED — D8: hard-fail on manifest mismatch
└── eval_datasets.py      # UNCHANGED (reused per FR-009)

tests/
├── test_eval_isolation.py     # NEW — incl. the deliberate-leak falsifiability test
├── test_eval_cost.py          # NEW
├── test_eval_latency.py       # NEW — sampling, gate math, threshold pinning
├── test_eval_grounding.py     # NEW — incl. injected-ungrounded falsifiability test
├── test_eval_axes.py          # NEW — manifest, attribution, baseline compare
└── test_eval_longmemeval.py   # MODIFIED — D7 regression (fails before the fix)

eval/
├── BASELINES.md           # NEW — committed cost + latency baselines with hardware
└── RESULTS-*.md           # existing — historical record, NOT restated

docs/EVALUATION.md        # MODIFIED — four axes, gating semantics, hardware rule
```

**Structure Decision**: single-package `src/` layout, unchanged. The new modules
are siblings of the existing `eval_*` modules rather than a subpackage, because
FR-009 requires reuse of the existing structure and the existing harnesses are
flat. `eval_axes.py` holds only genuinely shared plumbing — attribution, manifest
extension, sampling, baseline comparison — so the four axis modules stay
independently testable and independently runnable.

## Implementation Phases

Each phase is independently verifiable and leaves the tree green.

| Phase | Deliverable | Verification |
|---|---|---|
| **A** | `eval_axes.py` + `test_eval_axes.py`: id→row attribution, manifest extension, sampling with warm-up, baseline load/compare, gate evaluation | Unit tests; attribution proven against real two-project ingestion |
| **B** | `eval_isolation.py` (FR-001/002, SC-001) | Two-project corpus reports 0 leaks; **injected** leak produces non-zero and fails the run |
| **C** | D7 budget fix in `eval_longmemeval.py` + `eval_cost.py` (FR-003) | Test fails before the fix; after it, `R@5 != R@10` on the n=10 sample |
| **D** | `eval_latency.py` (FR-004) | Per-phase timings at ≥3 sizes; prefilter engagement asserted via `set_trace_callback`; µs/chunk ladder shows sub-linear growth |
| **E** | `eval_grounding.py` (FR-005/006, SC-007) | Grounded corpus reports 1.0; injected ungrounded text reports <1.0 and fails |
| **F** | `eval_compare.py` hard-fail (D8); `docs/EVALUATION.md`; committed `BASELINES.md` | Mismatched-manifest comparison exits non-zero; docs conformance test |

**Phase C is ordered after B deliberately.** The denominator defect must be fixed
*before* a cost metric is added next to recall, or the two metrics would disagree
on their unit by 3–7x and every committed cost number would be wrong.

## Cross-Cutting Invariants

1. **A metric that cannot fail is deleted.** Each axis ships with a
   falsifiability test that fails when the detection logic is removed.
2. **`null` never `0`.** A metric that could not be recorded reports `null` and
   `null` **fails** the gate. A skipped metric must not read as a pass.
3. **Harness bookkeeping stays outside timed regions**, or the harness benchmarks
   itself (measured: the naive grounding scan costs 51.6 ms/query against a 13 ms
   retrieval).
4. **No absolute portable claim.** Latency gates are ratios and per-unit costs;
   absolute milliseconds are a smoke bound with hardware recorded.
5. **Comparisons hard-fail on manifest mismatch** — a confident number computed
   across mismatched manifests is worse than no number.

## Risks

| Risk | Mitigation |
|---|---|
| Latency gate flakes on shared CI runners | Gate on µs/chunk ratio (hardware-independent) as the primary; absolute ms is a +40% smoke bound at n≥30; hardware recorded in every result |
| Corpus ladder silently crosses `PREFILTER_MIN_CANDIDATES=512` | Already caused one false result in this repo. Sizes sit on one side of the threshold, or the env var is pinned per size and recorded; engagement asserted via `set_trace_callback` |
| Grounding index build dominates runtime | Build once per corpus, never per hit. Measured 66x faster than the naive scan |
| Scope metric scores a NULL-scope query as a leak | Every scope filter is conditional on `is not None`, so `project_id=None` genuinely means "no scope requested". Unscoped queries get their own bucket, in neither the gate numerator nor denominator |
| Reused `custom_id` silently creates a third NULL-scope document | `create_document`'s natural key makes omission *non-colliding*. The harness asserts expected per-project document counts after ingest and never infers it from a zero leak count |
| Broad change destabilises a green tree | Four independent phases, each gated; `tasks.md` ordered so every phase is separately mergeable |