# Contract: Evaluation Axes v1

**Feature**: `002-evaluation-harness` | **Date**: 2026-10-04
**Machine-checkable schema**: [`eval-axes-v1.schema.json`](./eval-axes-v1.schema.json)

This is a **CLI contract**, not an HTTP contract. Memoratum's eval harnesses are
invoked as `python -m memoratum.<module>` and emit JSON plus Markdown. The
contract therefore has three parts:

1. **Command surface** — flags, defaults, exit codes.
2. **JSON result shape** — the machine-readable artifact, versioned.
3. **Gate semantics** — what makes a run pass or fail.

The existing harness convention is preserved exactly (FR-009): `argparse`, a
`main()` guard, `--out-md` / `--out-json`, a temp SQLite DB per question, and a
`manifest` block in every result.

---

## 1. Command surface

### 1.1 `memoratum.eval_isolation`

Proves tenant isolation is measured. FR-001, FR-002, SC-001.

```
uv run python -m memoratum.eval_isolation \
  --projects 3 \
  --sessions-per-project 40 \
  --queries 40 \
  --k 5,10 \
  --modes hybrid,documents \
  --seed 42 \
  --require-clean \
  --out-md eval/RESULTS-isolation.md \
  --out-json eval/isolation.json
```

| flag | type | default | meaning |
|---|---|---|---|
| `--projects` | int | `3` | number of synthetic projects. ≥2 required for the axis to mean anything |
| `--sessions-per-project` | int | `40` | documents per project |
| `--queries` | int | `40` | probes; half scoped, half unscoped |
| `--k` | csv int | `5,10` | retrieval depths |
| `--modes` | csv | `hybrid,documents` | search modes |
| `--seed` | int | `42` | sampling seed |
| `--require-clean` | flag | off | **on in CI.** Enables the falsifiability self-check |
| `--out-md` / `--out-json` | path | `""` | write artifacts; stdout always printed |

`--require-clean` runs the self-check required by SC-001: it injects a document
that *should* leak, asserts the harness detects it, and fails if it does not. A
metric that cannot fail must not be allowed to pass (invariant `I4`).

**Exit codes**: `0` clean · `1` leak detected · `2` self-check failed (the metric is
broken) · `3` bad input.

### 1.2 `memoratum.eval_cost`

FR-003, D2, D7.

```
uv run python -m memoratum.eval_cost \
  --data data/longmemeval_s_cleaned.json \
  --n 50 --seed 42 --k 5,10 \
  --baseline eval/BASELINES.md \
  --out-md eval/RESULTS-cost.md --out-json eval/cost.json
```

| flag | type | default | meaning |
|---|---|---|---|
| `--data` | path | required | dataset; never uploaded (FR-010) |
| `--n` | int | `20` | sampled questions |
| `--baseline` | path | `""` | when set, gates on the committed baseline |
| `--cost-unit` | enum | `chars` | `chars` \| `ws_tokens`. **bytes is not offered** — invariant `C1` |
| `--measure-ratio` | flag | on | recompute chars-per-token and record it; off reuses the manifest value |

**Exit codes**: `0` within baseline · `1` regression · `2` baseline missing or
invalid (`B3`) · `3` bad input.

### 1.3 `memoratum.eval_latency`

FR-004, D3.

```
uv run python -m memoratum.eval_latency \
  --ladder 100,200,400 \
  --samples 30 --warmup 5 \
  --pin-prefilter \
  --baseline eval/BASELINES.md \
  --out-md eval/RESULTS-latency.md --out-json eval/latency.json
```

| flag | type | default | meaning |
|---|---|---|---|
| `--ladder` | csv int | `100,200,400` | corpus sizes in chunks. ≥3 required (FR-004). **Must not straddle `PREFILTER_MIN_CANDIDATES`** (default 512) unless `prefilter_min` is recorded — see below |
| `--samples` | int | `30` | **floor is 20**; below that the gate is theatre (`L2`) |
| `--warmup` | int | `5` | discarded iterations (`L4`) |
| `--pin-prefilter` | flag | **off** | set `MEMORATUM_SEARCH_PREFILTER_MIN` per size and record `prefilter_min` (`L7`). Off by default so a ladder measures the real production threshold |
| `--phases` | csv | `ingest,embed,index,retrieve` | FR-004 requires these four reported separately |

**Why the default ladder stops at 400.** `search.PREFILTER_MIN_CANDIDATES` is **512**.
A ladder spanning 400 → 1600 crosses it, which changes the *algorithm* — whether the
FTS5 leg bounds the candidate set — rather than the corpus size. `research.md` D3 records
the measured consequence: that step produced a **3.38x → 1.09x → 3.55x** wobble. This
repository has already shipped one false result from this class (`tests/benchmarks/search.md`,
a prefilter comparison that ran identical code and was reported as "bounding is free").

So the default is entirely below the threshold. Two documented options:

| approach | ladder | when |
|---|---|---|
| **default** — stay below the threshold | `100,200,400` | normal use; measures the real production configuration |
| pin the threshold | any, with `--pin-prefilter` and `prefilter_min` recorded | deliberately measuring across 512, e.g. to compare bounding on vs off |

`eval_axes._validate_ladder` **rejects** a straddling ladder that does not record
`prefilter_min`, so the error is loud rather than a silently misleading number.

Measured microseconds-per-chunk across the default ladder (`research.md` D3), which is
the sub-linear shape the `us_per_chunk` gate keys on:

| chunks | 100 | 200 | 400 |
|---|---:|---:|---:|
| µs/chunk | 35.6 | ~33 | 30.1 |

**Exit codes**: `0` within tolerance · `1` regression, naming the phase · `2`
missing baseline or missing hardware (`B1`) · `3` bad input.

### 1.4 `memoratum.eval_grounding`

FR-005, FR-006, SC-007.

```
uv run python -m memoratum.eval_grounding \
  --data data/longmemeval_s_cleaned.json \
  --n 50 --seed 42 \
  --inject-ungrounded \
  --judge none \
  --out-md eval/RESULTS-grounding.md --out-json eval/grounding.json
```

| flag | type | default | meaning |
|---|---|---|---|
| `--judge` | enum | `none` | `none` \| `external`. **`external` is opt-in and never gates** (FR-006) |
| `--inject-ungrounded` | flag | off | SC-007 falsifiability check |
| `--tier2` | flag | off | enable diagnostic LCS / shingle / FTS5 snippet. **never gates** |
| `--min-grounded-fraction` | float | `1.0` | the gate. `1.0` is correct: every hit should be traceable |

**Exit codes**: `0` all grounded · `1` ungrounded hits found · `2` self-check
failed · `3` bad input.

### 1.5 Modifications to existing commands

`eval_longmemeval` gains, per FR-001 and D7:

| flag | default | meaning |
|---|---|---|
| `--project-count` | `1` | >1 ingests across projects, enabling the isolation axis on a real dataset |
| `--scope` | `scoped` | `scoped` \| `unscoped` \| `both`. `both` populates the unscoped bucket (invariant `I2`) |
| `--latency-baseline` / `--cost-baseline` | `""` | gate the existing runs on the new axes (SC-002) |

The retrieval budget changes from `max(max(ks) * 2, 10)` to
`max(max(ks) * mean_chunks_per_document, 10)` with the mean recorded in the
manifest (`C5`). **This changes future recall numbers.** It does not restate
committed ones (`M4`).

---

## 2. JSON result shape

Common envelope, matching the existing harness:

```json
{
  "axis": "isolation",
  "schema": "memoratum-eval-axes-v1",
  "manifest": { "...": "see §2.1" },
  "n": 50,
  "seconds": 12.4,
  "isolation": { "...": "see §2.2" },
  "gate": { "status": "pass", "checks": [] }
}
```

### 2.1 `manifest` — extended, backward-compatible

Existing keys unchanged: `schema`, `seed`, `requested_n`, `n`, `ks`, `modes`,
`embedder`, `vector_store`, `data_sha256`. `schema` bumps
`longmemeval-normalized-v1` → `longmemeval-scoped-v2`.

```json
{
  "schema": "longmemeval-scoped-v2",
  "seed": 42,
  "requested_n": 50,
  "n": 50,
  "ks": [5, 10],
  "modes": ["hybrid", "documents"],
  "embedder": "HashEmbedder:64",
  "vector_store": "SQLiteVectorStore",
  "data_sha256": "…",

  "scope_config": {
    "container_tag": "bench",
    "projects": ["proj-a", "proj-b", "proj-c"],
    "expected_documents": 120
  },
  "cost_config": {
    "unit": "chars",
    "chars_per_ws_token": 6.27,
    "mean_chunks_per_document": 7.6,
    "retrieval_limit": 76,
    "tokenizer": null
  },
  "latency_config": {
    "samples": 30,
    "warmup": 5,
    "ladder": [100, 200, 400],
    "prefilter_min": null,
    "hardware": {
      "cpu": "Intel i3-7020U @ 2.30GHz",
      "cores": 4,
      "ram_mb": 3072,
      "python": "3.12.14",
      "platform": "Linux-…"
    }
  },
  "grounding_config": {
    "normalization": "nfkc+whitespace+casefold",
    "rule_by_kind": { "chunk": "chunks.text", "memory": "memories.text", "fact": "subject predicate object" },
    "tier2_enabled": false,
    "judge": null
  }
}
```

`hardware` is **required** whenever `latency_config` is present. A latency baseline
without hardware is invalid and fails validation (`B1`, `L8`).

### 2.2 `isolation`

```json
{
  "scoped_queries": 20,
  "leaked_hits": 0,
  "leaked_queries": 0,
  "returned_hits": 380,
  "leak_rate": 0.0,
  "query_leak_rate": 0.0,
  "unscoped_queries": 20,
  "unscoped_cross_scope_hits": 214,
  "leaks": [],
  "per_query": [
    { "scope": "proj-a", "returned_hits": 19, "leaked_hits": 0, "leak_rate": 0.0 }
  ],
  "leaks_detail": [
    {
      "hit_id": "chunk_88",
      "document_id": "doc_9f2c",
      "originating_project_id": "proj-b",
      "requested_project_id": "proj-a",
      "rank": 3
    }
  ]
}
```

`unscoped_cross_scope_hits` is expected to be non-zero and is **informational
only**. It appears in neither the gate numerator nor the denominator (`I2`).

### 2.3 `cost`

```json
{
  "queries": 50,
  "hits": 3800,
  "retrieved_chars_total": 1140320,
  "retrieved_chars_mean": 300.1,
  "retrieved_chars_p95": 512.4,
  "retrieved_ws_tokens_mean": 47.9,
  "token_estimate_mean": 181905,
  "chars_per_ws_token": 6.27,
  "tokenizer": null,
  "redundant_hits": 3102,
  "token_estimate_caveat": "derived from measured chars_per_ws_token; valid only as a ratio between runs on the same corpus"
}
```

### 2.4 `latency`

```json
{
  "ladder": [
    {
      "corpus_chunks": 100,
      "phases": {
        "retrieve": {
          "samples": 30,
          "samples_ms": [3.11, 3.56, "…"],
          "median_ms": 3.56,
          "p95_ms": 5.42,
          "us_per_chunk": 35.6,
          "prefilter_engaged": null
        }
      }
    },
    { "corpus_chunks": 400, "phases": { "retrieve": { "median_ms": 12.03, "us_per_chunk": 30.1 } } }
  ],
  "gate": {
    "status": "pass",
    "checks": [
      {
        "name": "us_per_chunk_sublinear",
        "value": 30.1,
        "bound": 53.4,
        "status": "pass",
        "reason": "4x corpus growth cost 0.85x per chunk, so growth is sub-linear"
      }
    ]
  }
}
```

`samples_ms` holds **raw samples**. Percentiles are computed by the reader from
raw samples, never averaged across summaries (`L3`).

### 2.5 `grounding`

```json
{
  "hits_checked": 380,
  "grounded_hits": 380,
  "ungrounded_hits": 0,
  "grounded_fraction": 1.0,
  "by_kind": {
    "chunk":  { "checked": 342, "grounded": 342, "fraction": 1.0 },
    "memory": { "checked": 38,  "grounded": 38,  "fraction": 1.0 },
    "fact":   { "checked": 0,   "grounded": 0,   "fraction": null }
  },
  "ungrounded": [],
  "tier2_diagnostics": null,
  "judge": null
}
```

`fraction` is `null` when `checked == 0`, never `0.0` — a metric that was not
exercised is not a passing metric (`M3`).

---

## 3. Gate semantics

### 3.1 What gates

| Axis | Gated figure | Fails when |
|---|---|---|
| Isolation | `leaked_hits` | `> 0` |
| Cost | `retrieved_chars_mean` | `> baseline × (1 + tolerance)` |
| Latency | `us_per_chunk(4N)` ratio; `median_ms` | ratio `≥ 1.5×`; median `> baseline × 1.40` |
| Grounding | `grounded_fraction` | `< min_grounded_fraction` |

Recall and MRR remain **report-only**, as today.

### 3.2 What never gates

- Leak **rates** — trends only (`I3`).
- Unscoped cross-scope hits — expected (`I2`).
- Tier 2 grounding diagnostics — diagnostic only (`G3`).
- Any external judge score — opt-in, supplementary (`G6`, FR-006).
- Absolute token counts — the chars↔token estimate is a calibration, not a fact
  (`C2`, `C3`).

### 3.3 `null` fails

A figure that could not be recorded is `null`, and `null` **fails** the gate. A
skipped metric must never read as a pass (`M3`). This applies to a missing baseline
(`B3`), an invalid baseline (`B1`), and an unexercised `by_kind` fraction.

### 3.4 Manifest mismatch fails

`eval_compare` **hard-fails** — does not warn — when manifests disagree on
`embedder`, `vector_store`, `modes`, `seed`, `data_sha256`, or `n` (`M2`). The
command exits non-zero with the mismatched keys listed.

### 3.5 Falsifiability is itself gated

Each axis ships a self-check that must *detect* a deliberately introduced defect:

| Axis | Injected defect | Expected |
|---|---|---|
| Isolation | a document whose scope contradicts the query scope | non-zero `leaked_hits`, exit `1` |
| Cost | a duplicated hit | `redundant_hits` increases |
| Latency | a known-slow injected phase | the phase is named |
| Grounding | text absent from every ingested row | `grounded_fraction < 1.0`, exit `1` |

If a self-check fails to detect its defect, the harness exits `2` — the metric is
broken, which is worse than the defect it was looking for.

---

## 4. Stability

- `schema: "memoratum-eval-axes-v1"` is stable for the feature.
- **New keys may be added**; existing keys will not be removed or retyped within
  `v1`. A consumer must ignore unknown keys.
- `null` is a legal value wherever a metric may be unexercised. Consumers must
  treat `null` as failure-or-absent, **never** as `0`.
- The extension to the dataset manifest is additive and keeps the seven existing
  keys byte-identical, so older committed results remain readable by newer tooling.
- No change to the `search()` response shape. Scope attribution is derived, never
  returned (invariant `A2`, research D1).