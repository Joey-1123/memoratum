# Data Model: Evaluation Harness — Scope, Cost, Latency and Faithfulness

**Feature**: `002-evaluation-harness` | **Date**: 2026-10-04
**Plan**: [`plan.md`](./plan.md) | **Research**: [`research.md`](./research.md)

This harness holds **no persistent database schema of its own**. It writes a
temporary SQLite database per question — the pattern the existing harnesses
already use — reads from it through real production code paths, and emits JSON and
Markdown. So the "data model" here is the set of *report* entities, the
*attribution* relation they depend on, and the invariants that make the numbers
trustworthy.

The schema below is the **verified live schema**, read from a freshly migrated
database rather than from migration source, because two scope columns were added
by later migrations and the original `CREATE TABLE` no longer reflects them.

---

## 0. Pre-existing entities this harness reads (not modifies)

Verified by `PRAGMA table_info` against a migrated database.

### `documents`
| column | type | notes |
|---|---|---|
| `id` | TEXT | primary key |
| `container_tag` | TEXT NOT NULL | scope dimension 1 |
| `custom_id` | TEXT | caller-supplied natural key |
| `content` | TEXT NOT NULL | original document body |
| `org_id` | TEXT | scope dimension 2, **nullable** |
| `project_id` | TEXT | scope dimension 3, **nullable** |
| `status`, `metadata`, `expires_at`, `dreamed_at`, `created_at`, `updated_at` | | not read by the new axes |

Indexes: `idx_documents_project ON documents(project_id, container_tag)`,
`idx_documents_tag ON documents(container_tag)`, plus two `sqlite_autoindex`
entries backing `UNIQUE(container_tag, custom_id)`.

### `chunks`
`id INTEGER PRIMARY KEY`, `document_id TEXT NOT NULL REFERENCES documents(id)
ON DELETE CASCADE`, `idx`, `text` (the indexed row the retriever returns),
`embedding BLOB`, `created_at`.

### `memories`
`id TEXT PRIMARY KEY`, `container_tag`, `text`, `metadata`, `org_id`,
`project_id`, `document_id`, `fact_id`, `version`, `state`, timestamps.

### `facts`
`id TEXT PRIMARY KEY`, `container_tag`, `subject`, `predicate`, `object`,
`org_id`, `project_id`, `document_id`, `valid_from`, `valid_to`,
`superseded_by`. Relevant because `search.py:_fact_text` renders facts as
`"subject predicate object"` — which is why the grounding axis cannot use a single
global text rule (see §4).

### `search` hit shape
`{id, chunk, similarity}` — three keys, **no scope attribution**. Confirmed by
probe. This is why §1 exists.

---

## 1. Attribution — the relation everything else depends on

**Entity**: `Attribution` — resolves one search hit to its owning row and scope.

| field | type | source | meaning |
|---|---|---|---|
| `hit_id` | TEXT | hit dict | e.g. `chunk_41`, `mem_…`, `fact_…` |
| `hit_kind` | enum | derived from `hit_id` prefix | `chunk` \| `memory` \| `fact` |
| `document_id` | TEXT \| null | `chunks.document_id` | owning document |
| `originating_project_id` | TEXT \| null | `documents.project_id` | **the scope that owns this hit** |
| `originating_org_id` | TEXT \| null | `documents.org_id` | |
| `rank` | int | position in the ranked list | 0-based |
| `row_text` | TEXT | `chunks.text` \| `memories.text` \| `f"{s} {p} {o}"` | the text under that hit's own construction rule |

**Resolution rule.** Chunk hits resolve via
`SELECT d.custom_id, d.project_id, d.org_id FROM chunks c JOIN documents d ON d.id
= c.document_id WHERE c.id = ?`. This join is already used in production code at
`db.py:480`, and `eval_longmemeval.hit_session()` already performs the
`custom_id` variant of it. **The search response shape is therefore unchanged** —
attribution is derived, never returned.

**Invariants**
- `A1` Attribution is total: every hit id resolves or is explicitly classified
  `unresolvable`. An unresolved id is a **failure**, not a skip.
- `A2` `originating_project_id` is read from the database, never inferred from text.
  Byte-identical content in two projects produces two distinct document rows, so
  text cannot attribute scope. This is verified real, not hypothetical.
- `A3` The grounding axis and the isolation axis consume `Attribution` but never
  each other's conclusions. Keeping them separate is what stops a grounding false
  positive being read as a security finding.

---

## 2. ScopedCorpus

The ingest unit for the isolation and grounding axes.

| field | type | notes |
|---|---|---|
| `container_tag` | TEXT | always `"bench"`, matching existing harnesses |
| `projects[]` | list of `Project` | ≥2 for the isolation axis |
| `documents_expected` | int \| null | asserted after ingest; see invariant `S3` |

**`Project`**: `{project_id, org_id, documents[]}`, where `documents[]` are
`{custom_id, content}`. Distinct projects use **lexically overlapping content** by
design — if project B's text does not match the query, exclusion proves nothing.

**Invariants**
- `S1` Every project is fully scoped. `project_id` is never `null` in a scoped
  corpus.
- `S2` `custom_id` values are unique within a project and deliberately **reused
  across projects**, so the harness exercises the real collision path rather than a
  convenient one.
- `S3` **The harness asserts the expected per-project document count after ingest.**
  `create_document`'s conflict lookup is
  `(container_tag, custom_id, org_clause, project_clause)`, so reusing a
  `custom_id` across projects does *not* collide — it creates two rows, which is
  correct. But omitting `project_id` on one side also does not collide: it silently
  creates a third **NULL-scope** document, which grows the unscoped corpus and makes
  leak counts look clean while reproducing the exact feature-001 defect. The count
  assertion is the only thing that catches it. Document count is **never** inferred
  from a zero leak count.

---

## 3. IsolationReport

| field | type | meaning |
|---|---|---|
| `scoped_queries` | int | queries that requested a project |
| `leaked_hits` | int | **the gate.** hits whose `originating_project_id` ≠ requested scope |
| `leaked_queries` | int | scoped queries with ≥1 leak |
| `returned_hits` | int | total hits across scoped queries |
| `leak_rate` | float \| null | `leaked_hits / returned_hits`, scoped only. Trend signal |
| `query_leak_rate` | float \| null | `leaked_queries / scoped_queries`. Robust to varying hits-per-query |
| `unscoped_queries` | int | queries with `project_id = null` |
| `unscoped_cross_scope_hits` | int | informational, expected non-zero |
| `leaks[]` | list of `Leak` | per-leak attribution |

**`Leak`**: `{hit_id, document_id, originating_project_id, requested_project_id, rank}`
— the fields SC-004 requires.

Per-query record: `{scope, returned_hits, leaked_hits, leak_rate}`.
`returned_hits` is **always** emitted next to `leak_rate`, because 0.5% over 2 hits
and 0.5% over 200 hits are different facts.

**Invariants**
- `I1` **`leaked_hits == 0` is the gate** (FR-002, SC-001). The correct value is
  exactly zero, so there is no tolerance to tune and no false-positive surface.
- `I2` Unscoped queries appear in **neither** the gate numerator nor the
  denominator. Every scope filter in the codebase is conditional on `is not None`
  (`db.keyword_search`, `db.list_memories`, `facts.list_facts`,
  `SQLiteVectorStore.query`, `search`), so `project_id=None` genuinely means "caller
  requested no scope" and genuinely returns cross-project hits. Scoring that as a
  leak would make the metric unfixable.
- `I3` `leak_rate` is a **trend**, never a gate. A rate over a variable denominator
  is exactly the shape that hides a leak.
- `I4` The axis must be **falsifiable**: a deliberately leaked document must produce
  a non-zero count and fail the run (SC-001).

---

## 4. GroundingReport

| field | type | meaning |
|---|---|---|
| `hits_checked` | int | |
| `grounded_hits` | int | text traceable to an ingested row |
| `ungrounded_hits` | int | |
| `grounded_fraction` | float \| null | `grounded_hits / hits_checked` |
| `by_kind` | dict | same three counts per hit kind |
| `ungrounded[]` | list of `Ungrounded` | `{hit_id, hit_kind, reason, row_text_excerpt}` |

**Gating rule (Tier 1, authoritative).** For each hit: resolve `hit_id` →
source row via `Attribution`, then confirm `hit.row_text` matches that row under
**that hit kind's own construction rule**, after `unicodedata.normalize("NFKC", …)`,
whitespace collapse and casefold. Any mismatch is ungrounded.

**Diagnostic-only (Tier 2, never gates)**: longest-common-substring ratio for
graceful degradation, 3-gram shingle overlap, FTS5 `snippet()`/`highlight()` as a
token-level trace.

**`row_text` per hit kind**
| kind | ground against |
|---|---|
| `chunk_*` | `chunks.text` for that `document_id` |
| memory hit | `memories.text` for that id |
| fact-rendered | reconstructed `"subject predicate object"` from the fact row |

**Invariants**
- `G1` **Never grade chunk text against `documents.content`.** Over 5,268 real
  chunks, exact substring match is 99.7%, and NFKC+casefold does not change it. All
  18 failures come from `split_markdown`'s heading rewrite (`# **Heading**` for
  `### **Heading**`), and the failure is **catastrophic, not graceful**: the longest
  in-document prefix was 68 of 1,498 characters (5%). A per-item containment ratio
  would score a perfectly grounded chunk at 0.05.
- `G2` **One global grounding threshold is invalid.** The fact leg can never pass
  exact substring — `"Alice lives in Lisbon"` is not in the corpus, because
  `_fact_text` renders `"subject predicate object"`. A single threshold would score
  `memories` mode 0.0 and `documents` mode 1.0 for *identical evidence*. Grounding
  is per-kind.
- `G3` **Token-set containment is rejected as a primary signal.** Measured: text
  reassembled from a document's own words but not present in it scores containment
  **1.000** — identical to a perfect prefix truncation — while genuinely unrelated
  documents reach **0.739**, inside any sane 0.8 gate. Set overlap produces false
  positives on paraphrase-shaped fabrication *and* false negatives on short
  windows.
- `G4` The corpus text index is built **once per corpus**, never per hit. Measured:
  51.6 ms/query for the naive nested scan versus 0.78 ms/query for a line-keyed
  index — 66x.
- `G5` Grounding proves **provenance only**. It does not establish correctness or
  relevance; recall and MRR own those.
- `G6` An external judge is opt-in, non-gating, and its absence MUST NOT fail the
  run (FR-006, US4 scenario 3). When absent, the judge field is `null`.

---

## 5. CostReport

| field | type | meaning |
|---|---|---|
| `queries` | int | |
| `hits` | int | total hits counted |
| `retrieved_chars_total` | int | canonical cost unit |
| `retrieved_chars_mean` | float | |
| `retrieved_chars_p95` | float | |
| `retrieved_ws_tokens_mean` | float | companion unit |
| `token_estimate_mean` | float \| null | **derived, clearly labelled** |
| `chars_per_ws_token` | float | measured for this corpus, recorded in manifest |
| `tokenizer` | string \| null | non-gating; only present when one was configured |
| `redundant_hits` | int | hits duplicating an already-counted source row |

**Invariants**
- `C1` **Characters are canonical; bytes are rejected as a reported unit.** The
  corpus is pure ASCII (bytes/char = 1.000, sd 0.00), so bytes carry zero
  information — but on non-ASCII input the same metric inflates 1.1–3x with nothing
  visible in the report. Characters are also the unit the codebase already spends:
  `eval_mc10.py:71` truncates with `context[:12000]`.
- `C2` **The chars↔token ratio is measured and recorded, never hardcoded.** Measured
  6.27 chars per whitespace token (sd 0.47, CV 7.5%). A hardcoded 4.0 would be a
  fabricated constant in a committed artifact.
- `C3` `token_estimate_mean` is defensible **only as a ratio between two runs on the
  same corpus**. It is not a portable absolute claim.
- `C4` **Cost and recall MUST declare the same unit.** The existing harness fetches
  `max(2*max(ks), 10)` = 20 **hits** while `session_ids_of` dedupes **before** the
  `[:k]` slice, so `R@k` counts **distinct sessions**. Measured chunks/session is
  ~7.6 (median 8, p95 14), so 20 hits cover at most ~2.6 distinct sessions when
  chunks cluster — which is why `eval/RESULTS-n10.md` shows `partial-R@5` equal to
  `partial-R@10` to three decimals. Placing a hit-counting cost metric beside a
  session-counting recall metric would overstate cost by 3–7x.
- `C5` The retrieval budget is `limit >= max(ks) * mean_chunks_per_document`, with
  the mean recorded in the manifest.
- `C6` `tokenizer` presence MUST NOT change the gated metric. Reporting the gated
  figure differently depending on whether a tokenizer is installed would make the
  metric machine-dependent and violate FR-007.

---

## 6. LatencyReport

| field | type | meaning |
|---|---|---|
| `samples` | int | ≥20 per phase; 30 for the absolute-ms gate |
| `phase` | enum | `ingest` \| `embed` \| `index` \| `retrieve` \| `grounding_check` |
| `corpus_chunks` | int | ladder position |
| `samples_ms[]` | list[float] | **raw samples**, not pre-summarised |
| `median_ms` | float | gate input |
| `p95_ms` | float | report only |
| `us_per_chunk` | float \| null | **primary gate input** |
| `prefilter_engaged` | bool \| null | asserted, not assumed |
| `hardware` | object | `cpu`, `cores`, `ram_mb`, `python`, `platform` |

**Invariants**
- `L1` **Gate on `us_per_chunk`, not milliseconds.** Per-chunk cost is
  hardware-independent in a way raw ms is not. Measured 100 → 6,400 chunks:
  35.6 → 30.1 → 8.2 → **7.3** µs/chunk. Sub-linear growth appears as a *falling*
  number; a regression to O(n) appears as a *rising* one. Primary gate:
  `us_per_chunk(4N) < 1.5 × us_per_chunk(N)`.
- `L2` Absolute ms is a **smoke bound only**: `median_of_30 ≤ baseline × 1.40`. At
  n=30 the statistical headroom is +7%, leaving ~33% for real drift and hardware
  variation. **n=20 is the floor** — n=5 would need a +69% band and could not detect
  anything smaller than a 70% regression, which makes the gate theatre. Loosen to
  ±60% for phases under 2 ms; tighten to ±25% for phases over 50 ms.
- `L3` **Never average percentiles.** p95-of-p95 is not a p95. Aggregate raw
  samples, then take the percentile. Single-sample relative MAD is 5–16% and
  max/median over 200 samples is 2.11x, so the mean is never usable.
- `L4` **Warm up with 5 discarded iterations** before sampling, or the first call
  pays FTS5 tokenizer setup and statement compilation.
- `L5` **No remote embedder inside a timed region.** Network jitter (100 ms ± 80 ms)
  dwarfs everything measured. `HashEmbedder` only, recorded in the manifest.
- `L6` **Harness bookkeeping stays outside timed regions.** The naive grounding scan
  costs 51.6 ms/query against a 13 ms retrieval; inside the timed region the
  harness benchmarks itself.
- `L7` **The corpus ladder must not cross `PREFILTER_MIN_CANDIDATES = 512`.** That
  step measures a **3.38x → 1.09x → 3.55x** wobble because the interval changes the
  *algorithm*, not the size. This repository has already produced one false result
  from exactly this class — a prefilter comparison that ran identical code and was
  reported as "bounding is free" (`tests/benchmarks/search.md`). Sizes sit on one
  side of the threshold, **or** the env var is pinned per size and recorded; and
  `prefilter_engaged` is asserted via the `set_trace_callback` technique already
  used in `tests/test_search_perf.py`.
- `L8` Hardware is recorded with every figure (FR-008). Without it, "+40%" means
  nothing.

---

## 7. Manifest (extended, backward-compatible)

Existing fields are preserved unchanged; new fields are added. FR-007 requires
every reported figure to be reconstructible from this record.

**Existing (unchanged)**: `schema`, `seed`, `requested_n`, `n`, `ks`, `modes`,
`embedder`, `vector_store`, `data_sha256`.

**New — isolation**: `scope_config` (`{container_tag, projects[], expected_documents}`)
**New — cost**: `cost_config` (`{unit, chars_per_ws_token, mean_chunks_per_document, retrieval_limit, tokenizer}`)
**New — latency**: `latency_config` (`{samples, warmup, ladder[], prefilter_min, hardware}`)
**New — grounding**: `grounding_config` (`{normalization, rule_by_kind, tier2_enabled, judge}`)

**Invariants**
- `M1` `schema` is bumped for the extended shape (`longmemeval-normalized-v1` →
  `longmemeval-scoped-v2`).
- `M2` **`eval_compare` hard-fails on manifest mismatch** — not warns. A comparison
  computed across mismatched embedder, vector store, modes, seed, corpus hash or
  corpus size produces a confident number that means nothing, which is worse than
  producing none.
- `M3` A metric that could not be recorded reports **`null`, never `0`**, and
  `null` **fails** the gate. A skipped metric must never read as a pass.
- `M4` Existing committed results are a **historical record and are not restated.**
  The `R@k` denominator defect (C4) is fixed forward-only.

---

## 8. Baseline

The committed reference for cost and latency gates.

| field | type | meaning |
|---|---|---|
| `kind` | enum | `cost` \| `latency` |
| `axis` | string | which axis/config |
| `value` | float | the gated figure |
| `tolerance` | float | the band |
| `hardware` | object | required for `latency` |
| `manifest_ref` | string | the run that produced it |

**Invariants**
- `B1` A latency baseline **without** hardware is invalid and must fail validation.
- `B2` Baselines live in `eval/BASELINES.md` with the hardware stated, satisfying
  FR-008.
- `B3` A missing baseline is `null` and **fails** the gate. It never silently
  passes.

---

## 9. Entity relationships

```
ScopedCorpus ──1:N── Project ──1:N── Document ──1:N── Chunk
                                        │                  │
                                        │                  │
                            Attribution (hit_id → row) ────┘
                             │           │
              ┌──────────────┘           └──────────────┐
              ▼                                         ▼
        IsolationReport                            GroundingReport
        (scope is id-derived)                 (text is row-derived)

   CostReport ◄── unit declared by cost_config, must match recall's unit (C4)
   LatencyReport ◄── hardware recorded (L8), ladder pinned (L7)

   Manifest ──1:1── every report        Baseline ◄── gate evaluation
```

**Relationship invariants**
- `R1` `IsolationReport` and `GroundingReport` share `Attribution` but **no
  conclusions**. Grounding never attributes scope; isolation never judges text.
- `R2` Every report is reproducible from `Manifest` alone (FR-007).
- `R3` Every gated figure resolves to a `Baseline`; an absent baseline fails
  (B3).
- `R4` `CostReport` is only comparable to a `LatencyReport`'s corpus when both
  share a `manifest_ref`.