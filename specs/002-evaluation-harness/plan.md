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

**Performance Goals**: the harness itself must not be the bottleneck. Grounding is
**one keyed row lookup per hit** — measured end to end at 15.9 ms/query over the real
corpus, including the `search()` call it accompanies, and the queries-per-hit count is
constant as the corpus grows. No corpus-wide text index: re-measured, the "66x" speedup
that justified one does not hold (`research.md` D4), and the scan it replaced was
redundant anyway (invariant `G4`). Latency measurement uses `HashEmbedder` only, so no
network call sits inside a timed region.

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

## Scope Reconciliation — resolved 2026-10-04

Phase 0 research contradicted two lines of the spec's *Explicitly Out of Scope*
section. Both are now closed.

### BEAM — rejected by the spec's own conditional. No conflict.

The spec wrote: *"BEAM: deferred, not rejected. Adopt only if it runs fully
offline with no provider key; otherwise it cannot gate CI under Principle IV."*

Research verified the condition is **false**: grading is 100% LLM-judge
(`gpt-4.1-mini`, all 10 of 10 scorers, no deterministic branch), the judge prompt
is explicitly paraphrase-tolerant so re-runs drift, and `initialize_models()`
pulls further HuggingFace models plus `nltk.download`. BEAM is therefore
**rejected** by the spec's own stated rule. The deterministically reachable subset
(6 of 10 abilities via `source_chat_ids`) stays deferred and must be labelled
non-official, because it is not BEAM's metric. Recorded in `spec.md` and D5.

### HotPotQA — admitted for evidence retrieval as separable Phase C

**Decision: option 2, approved.** The spec rejected HotPotQA because *"answering
these requires an LLM to perform the reasoning"*. That reasoning is correct for
answering and wrong for retrieval: if the metric is *evidence retrieval* — does the
system return the gold `supporting_facts` paragraphs — no LLM is required, and
HotPotQA ships those facts plus a pure-stdlib scorer.

I dismissed HotPotQA in conversation before commissioning the research; that
dismissal was wrong. I had already reframed correctness as retrieval
precision/recall, then failed to check whether HotPotQA's *evidence* was
measurable that way.

`spec.md` is amended accordingly. What it adds that the current harness structurally
cannot:

- **Sentence-level** gold evidence, not session-level, giving strict partial credit.
- Explicit multi-hop `bridge`/`comparison` chains that session-recall metrics
  cannot isolate.
- A non-conversational corpus that actually exercises FTS5 lexical matching against
  Wikipedia titles instead of chat filler.

Verified: `hotpotqa/hotpot_qa` `fullwiki` validation = 28,041,820 bytes, 7,405 rows,
public, ungated. Honest caveat: indexing the union of provided contexts (~74K
paragraphs) is *easier* than true full-wiki (~5M articles, separate multi-GB dump).
Licence is CC BY-SA 4.0, so ShareAlike attaches to any normalised extract — which is
why the data is not vendored, matching the existing `data/download.log` precedent.

**Sequencing: Phase C ships only after the four axes are merged and green.** It must
not block or destabilise them. It is not started on this branch.

### The `R@k` unit fix — implemented here, ahead of the four axes

Originally planned as Phase C work. Moved onto this branch because it corrects
already-committed numbers, and leaving a known-wrong metric in place while building
a cost axis on top of it would have made every new cost figure disagree with recall
by 3–7x.

The fix: `partial_recall` / `full_recall` now slice **before** deduplicating, so
`R@k` reads the first `k` retrieval positions. `mrr` is deliberately unchanged.

Two findings from implementing it are recorded in full in
[`eval/MIGRATION-metric-v1.md`](../../eval/MIGRATION-metric-v1.md) and D7:

1. **The effect on values is small** — 0.000 at n=10, at most −0.040 at n=50. The
   effect on meaning is large. My original claim that `R@k` beyond ~3 was not
   measuring k-deep retrieval, evidenced by `R@5 == R@10`, was **wrong**: that
   equality persists after the fix.
2. **The retrieval budget must not be inflated.** The budget existed to give `R@k`
   enough hits to contain `k` *distinct sessions*. Under the corrected definition
   `R@k` reads the first `k` hits, so `max(ks)` suffices. This **reverses** D7's
   original recommendation and simplifies `C4`/`C5`.

A third finding is larger than either: **the committed results do not reproduce on
`main`**. MRR is untouched by this fix, yet `RESULTS-n10.md` records 0.599 where
current retrieval gives 0.583 under *either* definition, because feature 001's
search stages changed what is retrieved. That is why the migration note compares
three ways rather than two.

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
| **C** | `eval_cost.py` (FR-003) | Mean/p95/total chars per query; redundant-hit attribution; cost and recall share the hit unit |
| **D** | `eval_latency.py` (FR-004) | Per-phase timings at ≥3 sizes; prefilter engagement asserted via `set_trace_callback`; µs/chunk ladder shows sub-linear growth |
| **E** | `eval_grounding.py` (FR-005/006, SC-007) | Grounded corpus reports 1.0; injected ungrounded text reports <1.0 and fails |
| **F** | `eval_compare.py` hard-fail (D8); `docs/EVALUATION.md`; committed `BASELINES.md` | Mismatched-manifest comparison exits non-zero; docs conformance test |
| **—** | **`R@k` unit fix — already done on this branch** | `tests/test_eval_metrics.py`: 6 pass, 2 of them fail against pre-fix code |
| **C2** | HotPotQA evidence retrieval — **deferred, not started** | Ships only after A–F are merged and green |

**The `R@k` fix was pulled forward** out of its original Phase C slot. It corrects
already-committed numbers, and building a cost axis on top of a metric known to be
wrong would have made every new cost figure disagree with recall by 3–7x. It landed
here, with a failing-first regression test, before any axis work.

**Phase C is still ordered after B.** Isolation is the highest-severity invariant and
the one thing this feature exists to observe; it should not wait behind a cost axis.

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