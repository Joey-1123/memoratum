# Phase 0 Research: Evaluation Harness — Scope, Cost, Latency, Grounding

**Feature**: `002-evaluation-harness` | **Date**: 2026-10-04
**Inputs**: `spec.md`, `.specify/memory/constitution.md` (v1.0.0), two dispatched
research agents, and first-hand probes of this repository.

Where a decision rests on a measurement, the measurement is quoted with its
hardware. Where it rests on a probe I ran myself, the probe is named. Where it
could not be verified, that is stated rather than papered over.

---

## Measured baselines for this repository

Corpus: the on-disk `data/longmemeval_s_cleaned.json`. Timing hardware for every
figure below unless stated: Intel i3-7020U @ 2.30 GHz, 4 vCPU, 3 GB RAM,
Python 3.12.14, `HashEmbedder(dims=64)`, `search_mode="documents"`, `limit=10`.

### Existing harness, verified

| Property | Value |
|---|---|
| Harnesses | `eval_longmemeval`, `eval_locomo`, `eval_mc10` |
| Metrics implemented | `partial_recall`, `full_recall`, `mrr`, `session_ids_of` — retrieval only |
| Latency / cost / grounding | **absent**; zero references in `src/memoratum/eval_*.py` |
| Ingest scope | `container_tag="bench"` only; no `project_id`, no `org_id` |
| Attribution precedent | `hit_session()` re-resolves `chunk_<id>` → `documents.custom_id` by indexed join |
| Manifest | `schema, seed, requested_n, n, ks, modes, embedder, vector_store, data_sha256` |

### Isolation already holds — it was simply never measured

Probe: two projects, one container tag, near-identical lexical content, scoped
query as project A.

```
scoped query as project A -> 1 hits
hit keys : ['chunk', 'id', 'similarity']
carries project_id: False
measured cross-scope leaks for a scoped query: 0
```

The invariant holds today. Two consequences for the design:

- Search hits carry **no** scope attribution (`{id, chunk, similarity}` only), so
  the isolation metric must re-resolve ids. `hit_session()` already does exactly
  this, so re-resolution is the harness's own idiom rather than a new mechanism,
  and the public search response shape stays untouched.
- The metric's job is regression detection, not discovery. It must be capable of
  failing, so its gate cannot be "0 leaks observed" on an unscoped corpus.

### A defect in already-committed results — found, then partly corrected

The research flagged a unit mismatch in the existing harness. Verified first-hand:

```
$ grep -n "limit=max(max(ks)" src/memoratum/eval_longmemeval.py
181:                        limit=max(max(ks) * 2, 10),

$ cat eval/RESULTS-n10.md
## hybrid
- partial-R@5: 0.800 | full-R@5: 0.500
- partial-R@10: 0.800 | full-R@10: 0.500
```

`session_ids_of()` dedupes **before** the `[:k]` slice, so `R@k` counted *distinct
sessions* while the harness fetched `max(2*max(ks), 10)` = **20 hits**. Measured
chunks/session on this corpus is ~7.6 (median 8, p95 14), so 20 hits cover at most
~2.6 distinct sessions when chunks cluster.

**This defect is real and is now fixed on this branch.** But two claims made here
originally were wrong, and are corrected in D7 with measurements:

- The `R@5 == R@10` equality in `RESULTS-n10.md` is **not** a symptom of the defect.
  It persists after the fix (both 0.800), because the ranking has a sharp head on
  this corpus.
- The fix's effect on **values** is small: **0.000 at n=10**, and at most −0.040 at
  n=50. The original claim that "`R@k` beyond roughly k=3 is not measuring k-deep
  retrieval" overstated the case.

**What the defect actually did**: credit gold sessions found *beyond* the k-th hit.
At n=50 that inflated `partial-R@10` (0.780) above `partial-R@5` (0.740) by
0.040 — an unearned climb, since nothing relevant enters between hits 5 and 10.

**Consequence for this feature.** The retrieval budget does **not** need inflating —
the fix makes `R@k` read the first `k` hits directly, so `max(ks)` would suffice, and
the cost and recall units finally agree because both count hits. Committed results
are marked v0 and left unedited; the comparison is in
[`eval/MIGRATION-metric-v1.md`](../../eval/MIGRATION-metric-v1.md).

**A larger finding surfaced while verifying this.** The committed results **do not
reproduce on `main`**: MRR is untouched by the fix, yet `RESULTS-n10.md` records
0.599 where current retrieval gives 0.583 under *either* definition, because feature
001's search stages changed what is retrieved.

---

## D1 — Scope isolation as a gated axis

**Decision.** Extend ingestion and querying with `project_id`/`org_id`, attribute
every hit by id re-resolution, gate on the **absolute count** `leaked_hits == 0`,
and report rate and leaked-query-fraction alongside it for trends.

**Rationale.** Every scope filter in the codebase is conditional on `is not None`
(`db.keyword_search`, `db.list_memories`, `facts.list_facts`,
`SQLiteVectorStore.query`, `search`), so `project_id=None` genuinely means "caller
requested no scope" and genuinely returns cross-project hits. Scoring that as a
leak would make the metric unfixable. Gating on the absolute count sidesteps the
whole question: the correct value is exactly zero, so there is no tolerance to
tune and no false-positive surface.

Per query: `{scope, returned_hits, leaked_hits, leak_rate}`. Per leak:
`{hit_id, document_id, originating_project_id, rank}`. `returned_hits` is always
emitted next to `leak_rate`, because 0.5% over 2 hits and 0.5% over 200 hits are
different facts.

**Reporting shape.** `leaked_queries / scoped_queries` is the number that survives
varying hits-per-query; `leaked_hits / returned_hits` is the trend line. Unscoped
queries live in their own bucket and appear in **neither** the gate numerator nor
the denominator.

**Footgun, specific to this schema.** `create_document`'s natural key is
`(container_tag, custom_id, org_clause, project_clause)`. Reusing `custom_id`
across projects while omitting `project_id` on one side does **not** collide — it
silently creates a third NULL-scope document, growing the unscoped corpus and
making leak counts look clean while reproducing the exact defect from feature 001.
The harness must therefore assert the expected document count per project after
ingest, and must never infer it from a zero leak count.

**Alternatives considered.**
- *Add `project_id` to search hits* — rejected: changes the public response shape
  for the harness's benefit, and `hit_session()` already establishes re-resolution
  as the convention.
- *Rate as the gate* — rejected: a rate over a variable denominator is exactly the
  shape that hides a leak.
- *Score unscoped queries as clean* — rejected: it would let the
  unscoped-scope-widening regression register as a pass.

---

## D2 — Token cost: characters canonical

**Decision.** Report `retrieved_chars` as the canonical cost unit, with
`retrieved_ws_tokens` as a companion. Reject bytes as a reported unit. Record the
**measured** chars-per-whitespace-token ratio for the corpus in the manifest and
derive a token estimate from it, clearly labelled.

Measured on 690 real sessions:

| proxy | chars/unit | sd | p05 | p95 |
|---|---:|---:|---:|---:|
| whitespace token (`str.split()`) | **6.27** | 0.47 | 5.63 | 6.97 |
| regex token `[A-Za-z0-9]+` | 6.14 | 0.46 | 5.55 | 6.90 |
| UTF-8 byte | 1.00 (sd 0.00, max 1.10) | | | |

**Rationale.**
- Bytes carry zero information here (corpus is pure ASCII, bytes/char = 1.000) yet
  silently inflate 1.1–3x on non-ASCII input with nothing visible in the report.
- The whitespace-token coefficient of variation is **7.5%**, so a chars-based gate
  is a sound proxy for a tokens-based gate when detecting *regressions* — the only
  thing a CI gate needs.
- Characters are the unit the codebase already spends: `eval_mc10.py:71`
  truncates with `context[:12000]`. A token number nobody can compare to a
  truncation constant is a worse metric.

**Honest limit.** A real BPE tokenizer gives roughly 4.0–4.2 chars/token for
English prose, so whitespace tokens overcount by ~1.5x. That figure could not be
verified here — tiktoken, sentencepiece and tokenizers are absent from `.venv`, and
adding them is what Principle V forbids. So it is a **calibration constant, not a
fact**: record the measured ratio (6.27 for this corpus) in the manifest and never
hardcode 4.0.

The estimate is defensible **only as a ratio between two runs on the same
corpus**. "This query costs 1,850 tokens" would be a guess inheriting every
tokenizer difference the project refuses to depend on.

Tokenizer counts are reported **only** in a separate non-gating field, and only
when a tokenizer happens to be installed — reporting the gated metric differently
across machines would violate FR-007.

**Alternatives considered.**
- *Bytes* — rejected above.
- *Hardcode a 4.0 chars/token conversion* — rejected: unverifiable here and would
  be a fabricated constant in a committed artifact.
- *Require tiktoken* — rejected under Principle V.

---

## D3 — Latency: gate on cost-per-unit, not milliseconds

**Decision.** Three gates. Absolute milliseconds are a smoke bound only.

1. **Gate on µs/chunk.** Per-chunk cost is hardware-independent in a way raw
   milliseconds are not, and sub-linear growth shows up as a *falling* number
   while a regression to O(n) shows up as a *rising* one.

   Measured: 100 chunks 35.6 µs → 400: 30.1 → 1,600: 8.2 → 6,400: 7.3.
   Gate: `µs_per_chunk(N*4) < 1.5 × µs_per_chunk(N)`.

2. **Absolute ms smoke bound.** `median_of_30 ≤ baseline_median × 1.40`.

3. **Record hardware with every figure** (FR-008).

**Sample-size rationale — the bootstrap below was of the wrong quantity, and the
direct measurement supersedes it.**

The original rationale bootstrapped median-of-n against the true median over 2,000
resamples of a single run:

| n | 95% CI | headroom a 1.0x gate needs |
|---:|---|---:|
| 5 | [0.80, 1.69] | +69% |
| 10 | [0.91, 1.16] | +16% |
| 20 | [0.94, 1.09] | +9% |
| 30 | [0.96, 1.07] | **+7%** |
| 50 | [0.96, 1.06] | +6% |

That gave "n=30 needs only +7%, so n=20 is the floor". **Resampling one run's
samples estimates within-run sampling error and is structurally blind to
between-run machine noise** — which, on a laptop, is the larger term. Measuring the
gate against itself instead: ten identical runs of unmodified code.

| n | 400-chunk retrieve median | spread | ms bound exceeded | per-chunk ratio spread |
|---:|---|---:|---:|---:|
| 30 | 12.28 – 26.45 ms | **2.15x** | **1 in 6** | 1.33x |
| 120 | 10.88 – 18.08 ms | 1.66x | 0 in 6 | 1.16x |
| 400 | 12.74 – 13.17 ms | **1.03x** | 0 in 6 | 1.13x |

At 30 samples the gate **failed on unmodified code**: the per-chunk ratio exceeded its
own 1.5 bound in 2 runs of 10, and the ms smoke bound (11.09 × 1.40 = 15.53 ms) was
exceeded 1 run in 6. **n=120 is the floor**, set from the measurement. n=30 was not a
sweet spot; it was a sample count at which the band was narrower than the noise.

Two things this changed beyond the constant:

* **The ms baseline moved** (11.09 → 13.35) when re-recorded at 120 samples, while the
  `us_per_chunk_ratio` baseline stayed at **0.93**. That is the axis's central claim
  demonstrated rather than asserted: the direction-travelling figure was stable across a
  20% change in the absolute one.
* **The band is now above the noise.** A gate whose noise floor exceeds its width is not
  a loose gate; it is a coin flip, and a coin flip teaches operators to ignore it.

Loosen to ±60% for phases under 2 ms and tighten to ±25% for phases over 50 ms, where
statistical headroom shrinks.

Single-sample relative MAD is 5–16% and max/median over 200 samples is 2.11x, so
**the mean is never usable**; aggregate raw samples and take the percentile
afterwards. p95-of-p95 is not a p95.

**The threshold trap, which this repository has already fallen into.** The 400→1,600
step measures a **3.38x → 1.09x → 3.55x** wobble because `PREFILTER_MIN_CANDIDATES
= 512` sits inside that interval: the step changes the *algorithm*, not the size.
A ladder that crosses the threshold measures the switch. This already produced one
false result here — a prefilter comparison that ran identical code and was reported
as "bounding is free" (`tests/benchmarks/search.md`). Therefore:

- Every corpus size in the ladder sits on one side of the threshold, **or**
- `MEMORATUM_SEARCH_PREFILTER_MIN` is set explicitly per size and recorded in the
  manifest.
- The harness asserts the prefilter actually engaged, by the `set_trace_callback`
  technique already used in `tests/test_search_perf.py`.

**Two measurement hygiene rules.** Never time a remote embedder inside the timed
region — network jitter (100 ms ± 80 ms) dwarfs everything measured here; use
`HashEmbedder` and record it, which FR-011 already requires. And warm up with 5
discarded iterations, or the first call pays FTS5 tokenizer setup and statement
compilation.

**Alternatives considered.**
- *Gate on absolute ms only* — rejected: not portable, and the tail is the
  problem, not the timer (`perf_counter` resolves to 1e-09 s).
- *Gate on p95 only* — rejected: still hardware-bound.

---

## D4 — Grounding: provenance by row identity, not text overlap

**Decision.** Tier 1 gates; Tier 2 is diagnostic only.

- **Tier 1 (gates).** For each hit, resolve `hit.id` → source row by the same join
  `hit_session()` uses, then confirm the text matches that row under that hit
  kind's construction rule, after NFKC normalisation, whitespace collapse and
  casefold. Any mismatch fails.
- **Tier 2 (never gates).** Longest-common-substring ratio for graceful
  degradation, 3-gram shingle overlap, and FTS5 `snippet()`/`highlight()` as
  token-level trace.

**The measurement that decides this.** Candidate text versus corpus:

| candidate | exact substring | ws-normalised | token containment | Jaccard |
|---|---:|---:|---:|---:|
| prefix truncation, 98%→10% kept | 100% | 100% | 0.982–0.998 | 0.205–0.985 |
| mid-string window, 50/25/10% | 100% | 100% | 0.982–0.994 | 0.207–0.638 |
| **same vocabulary, reassembled** | **0%** | 1% | **1.000** | 0.116 |
| unrelated document | 0% | 0% | mean 0.251, **p95 0.433, max 0.739** | mean 0.123, max 0.250 |

**Token-set containment cannot separate grounded from fabricated.** Text assembled
from a document's own words but not present in it scores containment **1.000** —
identical to a perfect prefix truncation — while genuinely unrelated documents
reach **0.739**, inside any sane 0.8 gate. Any containment or Jaccard threshold
produces false positives on paraphrase-shaped fabrication *and* false negatives on
short windows. **Set overlap is unusable as a primary signal**, which eliminates the
most commonly proposed design.

Exact substring is 100% precise and near-100% sensitive for every truncation shape,
because a truncated or windowed hit is still a *contiguous* substring. Its only true
failure mode is non-contiguous reconstruction.

**Two non-obvious traps, both verified.**

1. *Never grade chunk text against `documents.content`.* Over 5,268 real chunks,
   exact substring is 99.7% (5,250/5,268), and NFKC+casefold does not change it.
   All 18 failures come from `split_markdown`'s heading rewrite
   (`# **Heading**` for `### **Heading**`), and the failure is **catastrophic, not
   graceful**: the longest in-document prefix was 68 of 1,498 characters (5%). A
   per-item containment ratio would score a perfectly grounded chunk at 0.05.
   Grade against `chunks.text` — the indexed row the retriever actually returns.
2. *The fact leg can never pass exact substring.* `search.py:_fact_text` renders
   `"subject predicate object"`, and `"Alice lives in Lisbon" in corpus` is `False`.
   A single global grounding threshold would score `memories` mode 0.0 and
   `documents` mode 1.0 for identical evidence. Each hit kind is therefore grounded
   against its own construction rule: `chunk_*` → `chunks.text`; memory hit →
   `memories.text`; fact-rendered → reconstructed `s p o` from the fact row.

**Cost. The "66x index" measurement was wrong, and the index it justified was
redundant.** This paragraph originally read: *"measured 51.6 ms/query against 690
documents versus 0.78 ms/query with a line-keyed index built once per corpus — 66x"*.
Re-measured on the real corpus (690 documents, 5,268 chunks):

| implementation | per hit, median | worst | note |
|---|---:|---:|---|
| nested scan over `documents.content` | 2.56 ms | 70.6 ms | the thing being replaced |
| "line-keyed" normalised index | — | — | **slower**: 60 s vs 14 s for 203 hits, because normalising the whole corpus dominates |

The index never paid for itself, and it was **redundant besides**: grounding already
resolves `hit.id` to a row, and a row that came out of the database *is* corpus
membership by definition, so nothing can fail a corpus-wide scan after passing row
identity. A mutant that deleted the corpus-presence check **survived the entire test
suite** — which is exactly what redundancy looks like from the outside. `CorpusTextIndex`
was deleted; grounding is one keyed lookup per hit and the invariant `G4` now says so.

Jaccard with per-hit re-tokenisation is 1,336 ms versus 176 ms with a precomputed corpus
token cache — that one holds, but it belongs to Tier 2, which never gates. Harness
bookkeeping must also stay **outside** the timed region, or the harness benchmarks itself.

**The corpus choice decided two of these conclusions.** `eval_longmemeval.format_session`
emits `role: content` lines with no headings, so `split_markdown`'s heading rewrite never
fires on LongMemEval: measured over its 5,268 chunks, **zero** fail exact substring match
against their parent document. Over this repository's own 47 markdown files (596 chunks),
**138 (23.2%)** fail, worst in-document prefix **1%**. The G1 figure quoted throughout this
spec ("5,268 chunks, 99.7%, 18 failures, worst prefix 68 of 1,498") was measured on the
corpus that cannot exhibit the trap. It is corrected to the markdown measurement, which is
70x worse and is the one that reflects real markdown documents.

**Scope attribution is a separate axis.** Byte-identical content across two
projects produces two distinct document rows with distinct ids, so text-based
grounding *cannot* attribute scope and must not try. Scope is `hit.id` →
`chunks.document_id` → `documents.project_id`, which belongs to D1 alone. Keeping
the axes separate is what stops a grounding false positive being read as a
security finding.

**FTS5 note.** `snippet(chunks_fts, 0, '[', ']', '...', 8)` works on this project's
external-content FTS5 table through stdlib `sqlite3` and is the best available
zero-dependency proof that tokens are in the index. `matchinfo()` does **not** work
in `SELECT` context; `bm25()`/`rank` do, since `db.py` already uses `rank`.

**Alternatives considered.**
- *Jaccard / token containment as primary* — rejected on the measurement above.
- *LLM judge for grounding* — rejected: non-deterministic and credentialed, so it
  cannot gate under FR-007.

---

## D5 — BEAM: rejected as a gating benchmark

**Decision.** Do not adopt BEAM. Defer a clearly-labelled non-official
retrieval-only subset.

**What it is.** *Beyond a Million Tokens: Benchmarking and Enhancing Long-Term
Memory in LLMs* — Tavakoli et al., arXiv:2510.27246, ICLR 2026, repo
`github.com/mohammadtavakoli78/BEAM`. Dataset (100 synthetic conversations at
128K/500K/1M/10M tokens, 2,000 human-validated questions, 10 abilities) plus a
harness. Code MIT, dataset **CC BY-SA 4.0**.

**Why rejected.** Grading is **100% LLM-judge**: `gpt-4.1-mini` in all 10 of 10
`evaluate_*` scorers, with no deterministic branch. The judge prompt is explicitly
anti-reproducible — *"Judge by meaning, not exact wording. Accept paraphrases and
synonyms"*. No pinned judge, no seed, no offline path. Re-runs drift, so it cannot
gate CI under FR-007. Answer generation is a second LLM stage, and
`initialize_models()` pulls further HuggingFace models plus `nltk.download`.

Data is ~449 MiB across two repos (~1.19 GiB resident). Vendoring a normalised
extract would be an **adaptation** under CC BY-SA 4.0, so ShareAlike would attach
to it — real licence friction in an AGPL repository. This project's own precedent
already handles it correctly: `data/` holds only a `download.log`, not the 277 MB
LongMemEval file.

**Overlap.** BEAM's own comparison table concedes LongMemEval and LoCoMo on
information extraction, multi-session reasoning, knowledge update, temporal
reasoning, abstention and summarisation — six of the ten abilities this project
already measures.

**The narrow subset.** Probing questions carry `source_chat_ids`, giving gold
evidence for 6 of 10 abilities and mapping onto existing recall/MRR. Abstention
has no `source_chat_ids` at all; summarization, instruction following and
preference following grade generated output. Any such subset **must be labelled
non-official**, because it is not BEAM's metric.

**Could not verify.** Whether the HuggingFace `probing_questions` string field is
byte-equivalent to the repo's own file (`BEAM-10M` types `user_questions` as
`sequence<null>`, suggesting a lossy conversion); that `source_chat_ids` are
well-formed across all 100 conversations (one file inspected); whether those ids map
cleanly onto this project's document/chunk scheme (ids repeat within a question,
and 10M nests turns under `plan-N`); the clone size of the repo's `chats/` tree.

---

## D6 — HotPotQA: promoted, and I was wrong to dismiss it

**Decision.** Adopt HotPotQA `fullwiki` validation as a retrieval harness, scoped
to **evidence recall**, not answer correctness.

**This reverses a position I took in conversation.** I dismissed HotPotQA as
"aimed at the wrong layer" because multi-hop reasoning needs an LLM. That is wrong
once the metric is evidence retrieval: HotPotQA ships gold `supporting_facts` and a
**deterministic stdlib scorer** — `normalize_answer` (lowercase, strip
punctuation, drop articles, collapse whitespace), `exact_match_score`, `f1_score`,
`update_sp` over `(title, sent_id)` sets, joint as the elementwise product. No
model, no network. I had already reframed "correctness" as retrieval precision and
recall, then failed to check whether HotPotQA's evidence was measurable that way.
It is.

**Verified properties.** HF `hotpotqa/hotpot_qa`,
`fullwiki/validation-00000-of-00001.parquet` = 28,041,820 bytes, `num_rows_total`
7,405, public and ungated. A live row: 10 context paragraphs, gold
`supporting_facts` `{title: [...], sent_id: [...]}`, `level: "hard"`. Scorer lives
at `hotpotqa/hotpot@master/hotpot_evaluate_v1.py`, pure stdlib. Licence
CC BY-SA 4.0 (same ShareAlike consideration as D5); scorer repo Apache-2.0.
`pyarrow` is needed only for a one-time offline parquet→JSONL conversion — the same
pattern as the existing `longmemeval_s_cleaned.json`.

**What it adds that the current harness cannot.** Sentence-level gold evidence
rather than session-level, giving strict partial credit; explicit multi-hop
`bridge`/`comparison` at `level: hard`, isolating retrieval chains that
session-recall metrics cannot; and a non-conversational corpus that actually
exercises FTS5 lexical matching against Wikipedia titles instead of chat filler.

**Honest caveat.** Indexing the union of provided contexts (~74K paragraphs) is
*easier* than true full-wiki (~5M articles, separate multi-GB dump), so this is
multi-hop evidence recall under a ~74K-paragraph distractor load — not full-wiki
scale. And `distractor` (10 paragraphs, ~1.2K tokens) does **not** force
out-of-window retrieval, so `fullwiki` is the correct config.

**Still rejected.** HotPotQA and MuSiQue as *answer*-correctness benchmarks. No
LLM here.

---

## D7 — Correct the `R@k` unit before adding cost

**Status: implemented on this branch. Superseded in part — read the correction
below before using the original recommendation.**

**Decision (original).** Declare one unit for both cost and recall, and size the
retrieval budget from measured chunks-per-document.

**Rationale (original).** `session_ids_of()` dedupes before slicing, so `R@k`
counts distinct sessions; the harness fetches `max(2*max(ks), 10)` = 20 **hits**;
measured chunks/session is ~7.6. Twenty hits therefore cover at most ~2.6 distinct
sessions when chunks cluster — which is why `RESULTS-n10.md` shows `R@5 == R@10`
exactly.

### Correction (2026-10-04, after implementing and measuring the fix)

Two things in the original rationale were wrong. Both are recorded here rather
than quietly edited, because the second one changes what this feature should do.

**1. `R@5 == R@10` was never evidence of the defect.** The original rationale cited
`eval/RESULTS-n10.md` showing `partial-R@5 == partial-R@10` as the symptom. It is
not. Under the corrected definition the equality persists (both 0.800 at n=10), so
it is not caused by the deduplication defect. The real cause is that the ranking
has a sharp head on this corpus: gold sessions are either within the first few
chunks or absent from all 20 retrieved.

**2. The fix has a much smaller effect on values than the original rationale
implied.** Measured, isolating the metric change from feature 001's search change
(full data in [`eval/MIGRATION-metric-v1.md`](../../eval/MIGRATION-metric-v1.md)):

| | metric-only Δ | search-only Δ |
|---|---:|---:|
| n=10, all metrics | **0.000** | MRR −0.016 |
| n=50, `partial-R@10` | −0.040 | −0.100 |
| n=50, `full-R@10` | −0.013 | −0.160 |

The **search** change is the larger effect. The metric fix is a correctness fix
with a small effect on values and a large effect on meaning.

**3. The retrieval budget does not need inflating — this reverses the original
recommendation.** The budget was compensating for the wrong metric: v0 needed a
deep hit budget so that `k` *distinct sessions* would exist at all. Under v1, `R@k`
reads only the first `k` hits, so a budget of exactly `max(ks)` suffices and
`max(2*max(ks), 10)` is merely generous. Because v1 recall now also counts **hits**,
the cost and recall units finally agree without any budget change — which resolves
the `C4`/`C5` problem in `data-model.md` more cleanly than inflating the budget
would have.

**The genuine signature of the defect**, visible only at n=50: v0 `partial-R@10`
(0.780) exceeded v0 `partial-R@5` (0.740) by crediting a gold session found beyond
the 5th hit. That gap was an artefact, and the committed file's apparent
`0.780 → 0.880` climb included `0.040` of unearned improvement.

**What was implemented.** `partial_recall` and `full_recall` now slice before
deduplicating (`set(session_ids_of(ranked[:k]))`). `mrr` is deliberately unchanged
— it has no `k` and remains a session-space metric. Scores can only move **down**,
because v1's visible session set is always a subset of v0's; a decrease is a
correction, not a retrieval regression.

**A second, larger finding.** The committed results **do not reproduce on `main`**.
MRR is untouched by this fix, yet `RESULTS-n10.md` records 0.599 where current
retrieval gives 0.583 under *either* metric definition. Feature 001's search stages
changed what is retrieved. This is a reproducibility gap in its own right, and it is
why the original two-column "old vs new" framing was replaced with the three-way
comparison in the migration note.

**Alternatives considered.**
- *Restate the committed results* — rejected. Existing results are a historical
  record; the fix is forward-only and the migration note carries the comparison.
- *Inflate the retrieval budget* — **rejected on measurement.** See correction 3.
- *Also change `mrr` to hit-space* — rejected as scope creep. MRR has no `k` and is
  not defective; a regression test pins its current behaviour.

---

## D8 — Manifest mismatches hard-fail comparisons

**Decision.** `eval_compare` must **fail**, not warn, when manifests disagree on
embedder, vector store, modes, seed, corpus hash, or corpus size.

**Rationale.** `HashEmbedder(64)`, `HashEmbedder(768)` and a remote provider differ
in recall, latency *and* per-chunk cost. A comparison run across mismatched
manifests is worse than no comparison, because it produces a confident number that
means nothing.

**Also enforced.** A metric that fails to record MUST report `null`, never `0`, and
`null` MUST fail the gate — a skipped metric that reads as a pass is worse than a
missing metric.

---

## Resolved clarifications

Every question deferred by the spec is now closed. Nothing remains open.

| Spec question | Resolution | Section |
|---|---|---|
| Characters or a tokenizer | Characters canonical, whitespace tokens companion, bytes rejected; ratio recorded, never hardcoded | D2 |
| Do latency gates block or only report | Three gates: µs/chunk primary, absolute ms smoke bound at n≥30, hardware recorded | D3 |
| Leak metric as rate or count | Count gates (`== 0`); rate and leaked-query fraction reported; unscoped in its own bucket | D1 |
| BEAM viability | Rejected as gating; non-official subset deferred | D5 |
| HotPotQA | Promoted for evidence recall; my earlier dismissal was wrong | D6 |
| Existing `R@k` denominator defect | **Fixed on this branch.** Slice before dedup; `mrr` unchanged. Committed results marked v0 and left unedited; three-way comparison in `eval/MIGRATION-metric-v1.md`. Effect on values is small (0.000 at n=10); effect on meaning is large. Budget inflation **reversed** — no longer needed | D7 |
| Grounding without a judge | Provenance by row identity, gated; set overlap rejected on measurement | D4 |

## What cannot be measured reliably, stated plainly

- **Absolute token cost**, and therefore real context-window fit. Only a
  chars↔token calibration constant, itself unverifiable without the dependency
  Principle V forbids. Report `retrieved_chars` and let the operator convert.
- **Absolute latency as a portable claim.** Every millisecond figure is
  hardware-bound. Only ratios and per-unit costs travel.
- **Whether a grounded answer is correct.** Grounding proves provenance. Recall and
  MRR own relevance. A grounded answer built from the wrong document is caught by
  D1 and D7, not by grounding.
- **True cross-tenant attribution from text.** Byte-identical content across
  projects is verified real; only id-based attribution is sound.
- **Whether a `memories`-mode hit is grounded**, unless the metric knows the
  `subject predicate object` rendering rule.
- **BEAM's HF↔repo field equivalence**, and `source_chat_ids` coverage across all
  100 conversations.