# Quickstart: Evaluation Harness — Scope, Cost, Latency and Faithfulness

**Feature**: `002-evaluation-harness` | **Date**: 2026-10-04
**Plan**: [`plan.md`](./plan.md) | **Contract**: [`contracts/eval-axes-v1.md`](./contracts/eval-axes-v1.md)

This is the **validation procedure**. Commands in §1 run today against `main` and
establish the baseline the feature must reproduce. Commands in §2 onward require
the implementation and are what each phase is verified with.

Everything runs **offline**, with **SQLite only** and **no provider extra
installed** (FR-011). Nothing uploads benchmark data (FR-010).

---

## 0. Prerequisites

```bash
cd /home/joey/projects/memoratum
git checkout 002-evaluation-harness
uv sync
```

Dataset, already present locally and **not committed** (only `data/download.log`
is tracked):

```bash
ls -la data/longmemeval_s_cleaned.json    # ~277 MB
```

No install step. The harness adds **zero** runtime dependencies. In particular there
is no tokenizer, no `pyarrow` and no `jsonschema` — see §6.

---

## 1. Baseline probes — runnable now

These establish what the feature must preserve. Each was executed during Phase 0
and its output is quoted, so a regression is detectable rather than assumed.

### 1.1 Isolation currently holds, and is unmeasured

```bash
uv run python - <<'PY'
import sys; sys.path.insert(0, "src")
import tempfile, os
from memoratum import db
from memoratum.embeddings import HashEmbedder
from memoratum.ingest import process_all
from memoratum.search import search

d = tempfile.mkdtemp()
conn = db.connect(os.path.join(d, "probe.db"))
emb = HashEmbedder(dims=64)
for pid, body in (("proj-a", "the quarterly budget review is scheduled for tuesday"),
                  ("proj-b", "the quarterly budget review is scheduled for tuesday")):
    db.create_document(conn, container_tag="bench", content=body,
                       custom_id="shared-id", project_id=pid)
process_all(conn, emb)

hits = search(conn, emb, "quarterly budget review",
              container_tag="bench", project_id="proj-a", limit=10)
print("scoped query as project A ->", len(hits), "hits")
print("hit keys :", sorted(hits[0].keys()) if hits else "n/a")
print("carries project_id:", "project_id" in (hits[0] if hits else {}))
print("per-project:", [tuple(r) for r in conn.execute(
    "SELECT project_id, COUNT(*) FROM documents GROUP BY project_id")])
conn.close()
PY
```

Expected today — this was executed, not predicted:

```
scoped query as project A -> 1 hits
hit keys : ['chunk', 'id', 'similarity']
carries project_id: False
per-project: [('proj-a', 1), ('proj-b', 1)]
```

`process_all` lives in `memoratum.ingest`, not `memoratum.db` — `eval_longmemeval`
imports it from there.

The `per-project` line matters as much as the leak count: reusing `custom_id`
across two projects produced **two rows, not a collision**, and no third
NULL-scope row. That is the correct behaviour, and the assertion the harness must
make on every run.

Two facts this pins down:

- Cross-scope hits for a scoped query: **0**. The invariant holds — the metric
  simply never measured it.
- Hits carry **no** scope attribution, which is why attribution re-resolves ids
  through the indexed join (`db.py:480` uses the same join in production).

Note the deliberate trap in this probe: `custom_id` is **reused across both
projects**. Because `create_document`'s conflict lookup is
`(container_tag, custom_id, org_clause, project_clause)`, that creates two rows
rather than colliding. Omit `project_id` on one side and it *also* does not
collide — it silently creates a third NULL-scope document. This is why the harness
asserts expected per-project document counts and never infers them from a zero leak
count (invariant `S3`).

### 1.2 The committed `R@k` numbers — and why they are not reproducible

```bash
head -14 eval/RESULTS-n10.md
grep -n "limit=max(max(ks)" src/memoratum/eval_longmemeval.py
```

`R@5` equals `R@10` in the committed n=10 file. **This is not the defect's
signature**, despite being the original evidence for it — the equality persists
after the fix, because the ranking has a sharp head on this corpus. The real
signature appears at n=50, where v0 `partial-R@10` (0.780) exceeded v0
`partial-R@5` (0.740) by crediting a gold session found *beyond* the 5th hit.

Two separate problems live in these files, and conflating them would misattribute
one to the other:

| | cause | size |
|---|---|---|
| `R@k` crediting sessions past the k-th hit | metric definition | 0.000 at n=10; ≤0.040 at n=50 |
| `MRR 0.599` vs `0.583` today | feature 001's search stages | MRR −0.016 at n=10; −0.020 at n=50 |

The second is the larger problem and is **not** addressed by the metric fix. Both
committed files are marked v0 and left unedited; the three-way comparison is in
[`eval/MIGRATION-metric-v1.md`](../../eval/MIGRATION-metric-v1.md).

### 1.3 The corrected metric, verified

```bash
uv run pytest tests/test_eval_metrics.py -q --no-cov
```

Expected: **6 passed**. Two of these fail against the pre-fix code — the metric
change shipped with a failing-first regression test.

The decisive case: a gold session at hit-rank 5 is credited at `k=2` under v0 and
correctly refused under v1.

```bash
uv run python -c "
import sys; sys.path.insert(0,'src')
from memoratum.eval_metrics import partial_recall
r=['s1','s1','s1','s1','s2']
print('k=2 ->', partial_recall(r,{'s2'},k=2), '(must be 0.0: s2 is at hit-rank 5)')
print('k=5 ->', partial_recall(r,{'s2'},k=5), '(must be 1.0)')
"
```

Scores can only move **down** after the fix, because v1's visible session set is
always a subset of v0's. A decrease is a correction, not a retrieval regression,
and must never be "fixed" by re-inflating the retrieval budget.

### 1.4 Token-proxy calibration

The corpus is ~277 MB of JSON and `eval_datasets.load_records` reads it as one
string, so the naive form OOMs on a small box. This streaming probe is stdlib-only
and reads only a prefix:

```bash
uv run python - <<'PY'
import json, sys; sys.path.insert(0, "src")

def head_records(path, limit):
    """Stream the first `limit` objects from a top-level JSON array. Stdlib only."""
    dec, out, i = json.JSONDecoder(), [], 0
    with open(path) as fh:
        buf = fh.read(1 << 20)
        while buf[i] in " \t\r\n,": i += 1
        if buf[i] != "[": raise ValueError("expected a top-level JSON array")
        i += 1
        while len(out) < limit:
            while i < len(buf) and buf[i] in " \t\r\n,": i += 1
            try:
                obj, end = dec.raw_decode(buf, i)
            except ValueError:
                chunk = fh.read(1 << 20)
                if not chunk: break
                buf, i = buf[i:] + chunk, 0
                continue
            out.append(obj); i = end
            if i > (1 << 19):
                buf, i = buf[i:], 0
    return out

from memoratum.eval_datasets import normalize_longmemeval
from memoratum.eval_longmemeval import format_session
norm = normalize_longmemeval(head_records("data/longmemeval_s_cleaned.json", 20))
chars = toks = 0
for r in norm:
    for s in r.get("sessions", []):
        text = format_session(s["turns"])
        chars += len(text); toks += len(text.split())
print(f"chars={chars} ws_tokens={toks} ratio={chars/toks:.2f}")
PY
```

Executed output:

```
chars=9859379 ws_tokens=1573914 ratio=6.26
```

The Phase 0 research measured **6.27** over 690 sessions — this 20-record prefix
agrees, so the ratio is stable across sample sizes and belongs in the manifest
rather than being hardcoded (`C2`).

---

## 2. Phase B — isolation

```bash
uv run python -m memoratum.eval_isolation \
  --projects 3 --sessions-per-project 40 --queries 40 --k 5,10 \
  --modes hybrid,documents --seed 42 \
  --out-md eval/RESULTS-isolation.md --out-json eval/isolation.json
echo "exit=$?"
```

**Pass** (SC-001): `scoped_queries=20`, `leaked_hits=0`, `leaked_queries=0`,
`leak_rate=0.0`, `unscoped_queries=20`, `unscoped_cross_scope_hits > 0`, exit `0`.

The unscoped bucket being non-zero is **correct and required**: every scope filter
is conditional on `is not None`, so `project_id=null` genuinely means "no scope
requested". It appears in neither the gate numerator nor denominator.

### 2.1 Falsifiability — the check that matters most

```bash
uv run python -m memoratum.eval_isolation --require-clean --out-json /tmp/iso.json
echo "exit=$?"     # MUST be 0: the self-check must detect its injected leak
```

`--require-clean` injects a document that *should* leak. Expected: the harness
**detects** it, reports a non-zero count, and exits `0` because the detection
succeeded. Exit `2` means the metric is broken — worse than the leak it was looking
for, because it means a green result means nothing.

To confirm the gate is real, add an unscoped hit to a scoped run by hand and confirm
exit `1` with the offending `hit_id`, `originating_project_id` and `rank` in
`leaks_detail`.

---

## 3. Phase C — cost

```bash
uv run python -m memoratum.eval_cost \
  --data data/longmemeval_s_cleaned.json \
  --n 50 --seed 42 --k 5,10 \
  --out-md eval/RESULTS-cost.md --out-json eval/cost.json
```

**Pass**: `retrieved_chars_mean`, `retrieved_chars_p95`,
`retrieved_chars_total`, `redundant_hits` all present and non-null;
`chars_per_ws_token` present and near 6.27; `tokenizer: null`.

### 3.1 The metric fix must not need a bigger budget

```bash
uv run python -m memoratum.eval_longmemeval \
  --data data/longmemeval_s_cleaned.json --n 50 --seed 42 --k 5,10 --modes hybrid
```

**Pass**: `partial-R@5` and `partial-R@10` are now **equal** (both 0.740), because
nothing relevant enters between hits 5 and 10. Under v0 they read 0.740 and 0.780 —
a climb produced entirely by crediting a gold session found past the 5th hit.

The budget stays at `max(2*max(ks), 10)`. It previously existed to give `R@k` enough
hits to contain `k` *distinct sessions*; under the corrected definition `R@k` reads
the first `k` hits, so no inflation is warranted. Re-inflating it now would mask a
real signal. See `eval/MIGRATION-metric-v1.md`.

### 3.2 Cost gating

```bash
uv run python -m memoratum.eval_cost \
  --data data/longmemeval_s_cleaned.json --n 50 --baseline eval/BASELINES.md
echo "exit=$?"     # 0 within baseline | 1 regression with delta | 2 baseline missing
```

Exit `2` on a missing baseline is **correct**: `null` fails the gate, it never
silently passes (`B3`, `M3`).

---

## 4. Phase D — latency

```bash
uv run python -m memoratum.eval_latency \
  --ladder 100,200,400 --samples 30 --warmup 5 \
  --out-md eval/RESULTS-latency.md --out-json eval/latency.json
```

**Pass**: four phases per corpus size (FR-004); `samples_ms` holds **raw** samples;
`median_ms`, `p95_ms`, `us_per_chunk` present; `hardware` recorded; `exit 0`.

**Per-chunk cost must fall, not rise.** Expected shape from Phase 0, all below the
`PREFILTER_MIN_CANDIDATES = 512` threshold:

| chunks | 100 | 200 | 400 |
|---|---:|---:|---:|
| µs/chunk | ~35.6 | ~33 | ~30.1 |

A **rising** `us_per_chunk` means a regression to O(n).

To measure across the threshold deliberately, pin it and record `prefilter_min`:

```bash
MEMORATUM_SEARCH_PREFILTER_MIN=512 uv run python -m memoratum.eval_latency \
  --ladder 100,400,1600,6400 --samples 30 --pin-prefilter
```

Without the pin, a straddling ladder is **rejected** rather than silently measured,
because that step changes the algorithm rather than the corpus size (`L7`).

### 4.1 Assert the prefilter actually engaged

```bash
uv run python -c "
import json; d=json.load(open('eval/latency.json'))
for e in d['latency']['ladder']:
    print(e['corpus_chunks'], e['phases']['retrieve']['prefilter_engaged'])
"
```

`prefilter_engaged` is asserted via `set_trace_callback` — the technique already
used in `tests/test_search_perf.py` — and never assumed. A `null` where the corpus
exceeds the prefilter threshold is a defect.

### 4.2 Sampling floor

```bash
uv run python -m memoratum.eval_latency --ladder 100,200,400 --samples 5
echo "exit=$?"     # MUST be non-zero: below n=20 the gate is theatre
```

Rejecting `--samples 5` is part of the contract. At n=5 a 1.0x gate needs a +69%
band and cannot detect anything smaller than a 70% regression.

### 4.3 A straddling ladder is rejected

```bash
uv run python -m memoratum.eval_latency --ladder 100,400,1600,6400
echo "exit=$?"     # MUST be non-zero
```

The default threshold is 512, so the 400 → 1600 step changes the *algorithm* rather
than the corpus size. Measured, that step produced a **3.38x → 1.09x → 3.55x** wobble,
and this repository has already shipped one false result from this class. Reject the
ladder, or pin the threshold and record `prefilter_min` (see §4).

---

## 5. Phase E — grounding

```bash
uv run python -m memoratum.eval_grounding \
  --data data/longmemeval_s_cleaned.json --n 50 --seed 42 \
  --out-md eval/RESULTS-grounding.md --out-json eval/grounding.json
```

All **25** questions' sessions are ingested into **one** corpus before any question is
evaluated, so a hit from question 3 is resolved against a corpus that also contains
questions 1 and 2. Grounding each question against only its own haystack would make the
metric trivially true — a retriever could return the wrong question's chunk and still
pass.

**Pass**: `grounded_fraction == 1.0`, `ungrounded == []`, exit `0`. An ungrounded
hit is a **defect**, not a curiosity.

Executed output on the real corpus (`--n 25`):

```
questions evaluated: 25
hits_checked: 250        grounded_hits: 250       grounded_fraction: 1.000
rows resolved: 250 (one keyed lookup per hit)
chunk:  checked 250, grounded 250, fraction 1.000
memory: checked 0,   grounded 0,   fraction n/a
fact:   checked 0,   grounded 0,   fraction n/a
```

`memory` and `fact` report `fraction n/a` and not `0.0`: this corpus produces no
fact-derived hits, and an unexercised metric is not a passing metric (M3). The fact leg
is exercised by `test_a_fact_hit_uses_the_fact_rule_not_the_memory_rule`.

### 5.1 Falsifiability (SC-007)

```bash
uv run python -m memoratum.eval_grounding --inject-ungrounded --out-json /tmp/g.json
echo "exit=$?"     # MUST be 0: the self-check must detect its injected text
```

The harness injects text absent from every ingested row and must report it. Two
details make this check meaningful rather than decorative, and both are asserted by
tests because a self-check that passes for the wrong reason is worse than none — it turns
a broken metric green:

- the injected hit **borrows a real row's id**, so only the text comparison can catch it.
  An unresolvable id would be caught by the row-identity check and would prove nothing;
- the injected text is **non-empty**, so it cannot be caught by the empty-text guard.

The self-check also has a **negative control in the other direction**
(`force_noisy`): a detector that reports *everything* ungrounded must exit `2` too. A
metric that fails everything teaches operators to ignore the gate.

### 5.2 The two grounding traps, measured on real content

**Correction to an earlier draft of this section.** It claimed the `G1` trap needed
5,268 real corpus chunks, quoting "99.7% exact substring, 18 failures, longest in-document
prefix 68 of 1,498 characters". Two things about that are wrong, and both were found by
running the probes rather than reading them:

1. The LongMemEval corpus **cannot** exhibit the trap. `eval_longmemeval.format_session`
   emits `role: content` lines with no headings, so `split_markdown`'s heading rewrite never
   fires. Measured over the first 120 records (690 sessions, 5,268 chunks): **zero** chunks
   failed exact substring match against their parent document. A limit of 1–10 records
   gives 8–14 chunks and also zero failures.
2. Markdown **does** exhibit it, dramatically. Over this repository's own 47 markdown files
   (596 chunks): **138 of 596 (23.2%)** are not exact substrings of their parent, and the
   worst in-document prefix is **1% of the chunk**.

So the correct measurement is the markdown one, and the "66x" corpus-index speedup that
justified building a corpus-wide text index does not hold either: re-measured on the real
5,268 chunks, a nested substring scan costs 2.56 ms/hit median (70.6 ms worst) and the
"indexed" variant is **slower**, because normalising the whole corpus dominates. The scan is
also **redundant** — a row that came out of the database is corpus membership by definition,
so nothing can fail a corpus-wide scan after passing row identity. `CorpusTextIndex` was
removed; grounding is now one keyed lookup per hit (invariant `G4` rewritten to say so).

Reproduce the trap against real markdown:

```bash
uv run python - <<'PY'
import sys; sys.path.insert(0, "src")
import glob, os, tempfile
from memoratum import db
from memoratum.embeddings import HashEmbedder
from memoratum.ingest import process_all

corpus = []
for path in sorted(glob.glob("**/*.md", recursive=True)):
    if "/.venv/" in path or "/node_modules/" in path:
        continue
    text = open(path, encoding="utf-8").read()
    if len(text) >= 200:
        corpus.append((os.path.relpath(path), text))
print(f"markdown files: {len(corpus)}")

d = tempfile.mkdtemp(); conn = db.connect(os.path.join(d, "md.db"))
for custom_id, text in corpus:
    db.create_document(conn, container_tag="bench", content=text, custom_id=custom_id)
process_all(conn, HashEmbedder(dims=64))

total = mismatched = 0
worst = (1.0, "")
for row in conn.execute("SELECT document_id, text FROM chunks"):
    total += 1
    parent = conn.execute("SELECT content FROM documents WHERE id = ?",
                          (row["document_id"],)).fetchone()["content"]
    text = row["text"]
    if not text.strip() or text in parent:
        continue
    mismatched += 1
    longest = 0
    for end in range(1, len(text) + 1):
        if text[:end] in parent:
            longest = end
        else:
            break
    if longest / len(text) < worst[0]:
        worst = (longest / len(text), text[:70])

print(f"chunks={total} not_exact_substring={mismatched} ({mismatched / total:.1%})")
print(f"worst in-document prefix: {worst[0]:.0%}  {worst[1]!r}")
conn.close()
PY
```

Executed output:

```
markdown files: 47
chunks=596 not_exact_substring=138 (23.2%)
worst in-document prefix: 1%  '# Added\n\n- Canonical `mem_<uuid>` lifecycle across native and…'
```

That first character of the worst chunk is `# Added`, from a document that contained
`## Added`. The heading level was rewritten, so the chunk is **not contiguous** in
`documents.content` — yet it came straight out of `chunks` and is perfectly grounded.
A per-item containment ratio would score it 0.01.

Grade against `chunks.text` — the indexed row the retriever actually returns. Both axes of
the claim are asserted in `tests/test_eval_grounding.py`:
`test_real_chunks_can_have_a_tiny_prefix_in_their_parent_document` measures the failure,
and `test_chunk_text_is_grounded_against_chunks_text_not_documents_content` proves the
axis reports `grounded_fraction == 1.0` for the very chunk with the 1% prefix.

### 5.2.1 The fact leg (G2)

```bash
uv run python - <<'PY'
import sys; sys.path.insert(0, "src")
from memoratum.search import _fact_text
body = "# Notes\n\nAlice moved to Lisbon last spring."
print("fact rendering :", repr(_fact_text(
    {"subject": "Alice", "predicate": "lives in", "object": "Lisbon"})))
print("in corpus      :", _fact_text(
    {"subject": "Alice", "predicate": "lives in", "object": "Lisbon"}) in body)
print("-> a single global substring rule would score the fact leg 0.0 (G2)")
PY
```

`_fact_text` takes a **dict** of `subject`/`predicate`/`object`, not a tuple. Executed
output:

```
fact rendering : 'Alice lives in Lisbon'
in corpus      : False
```

The `fact` line needs no caveat: `'Alice lives in Lisbon'` is genuinely absent from the
corpus, because `_fact_text` renders `"{subject} {predicate} {object}"`. A single global
substring rule would score the fact leg **0.0** while scoring the document leg 1.0 for
identical evidence. Each kind therefore has its own rule, and the manifest records it.

### 5.3 Tier 2 stays non-gating, and a judge is never required

```bash
uv run python -m memoratum.eval_grounding --tier2 --judge none --out-json /tmp/g2.json
echo "exit=$?"     # MUST be 0 — an absent judge must not fail the run (FR-006)
```

`--tier2` adds LCS ratio, 3-gram shingle overlap and FTS5 `snippet()` trace.
`snippet(chunks_fts, 0, '[', ']', '...', 8)` works on this project's
external-content FTS5 table through stdlib `sqlite3`. `matchinfo()` does **not**
work in `SELECT` context; `bm25()`/`rank` do.

---

## 6. Phase F — comparison, docs, baselines

### 6.1 Manifest mismatch hard-fails

```bash
uv run python -m memoratum.eval_compare eval/cost.json eval/cost-hash768.json
echo "exit=$?"     # MUST be non-zero, listing the mismatched keys
```

A comparison computed across mismatched embedder, vector store, modes, seed,
corpus hash or corpus size produces a confident number that means nothing, which is
worse than producing none (`M2`).

### 6.2 Latency baseline without hardware is rejected

```bash
uv run python -m memoratum.eval_latency --baseline /tmp/no-hardware.json
echo "exit=$?"     # MUST be 2
```

FR-008 requires the hardware. `B1` makes its absence invalid.

### 6.3 Contract conformance without a schema library

`jsonschema` is deliberately **not** a dependency (Principle V). Conformance is
enforced by `tests/test_eval_axes.py`, which asserts the required keys and types
structurally. The schema file is the normative contract; the test is the
enforcement.

### 6.4 Docs conformance

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/test_eval_docs_conformance.py -q --no-cov
```

`docs/EVALUATION.md` must describe all four axes, the gating semantics, the
hardware requirement, and the `chars`-not-tokens unit. A documented behaviour that
is not implemented is a defect, as is an implemented behaviour that contradicts the
docs.

---

## 7. Full gate suite

Run verbatim. Do **not** add flags — subset runs need `--no-cov` because the
coverage floor is a full-suite property, and two CI-only failures in the previous
feature came from not running the commands as written.

```bash
uv run python scripts/check_no_telemetry.py
MEM0_TELEMETRY=false uv run pytest tests/test_mem0_official_sdk.py \
  tests/test_mem0_official_lifecycle.py tests/test_mem0_webhooks.py -q --no-cov
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
npm test --prefix clients/ts
node --check clients/opencode/memoratum.js
npm ci --prefix dashboard && npm run build --prefix dashboard
uv run pip-audit --local
npm audit --audit-level=high --prefix clients/ts
npm audit --audit-level=high --prefix dashboard
```

CI gate for the axes themselves:

```bash
uv run python -m memoratum.eval_isolation --require-clean
uv run python -m memoratum.eval_cost --baseline eval/BASELINES.md
uv run python -m memoratum.eval_latency --baseline eval/BASELINES.md --pin-prefilter
uv run python -m memoratum.eval_grounding --inject-ungrounded
uv run python scripts/check_no_new_suppressions.py
```

Coverage floor is `fail_under=74`, branch-aware; baseline 77.25%. This feature
must not lower it.

---

## 8. Success criteria verification matrix

Every SC maps to a command and an observable result. Any row that cannot be
executed means that criterion is unverified, not passing.

| SC | Criterion | Verify with | Pass signal |
|---|---|---|---|
| **SC-001** | Two projects → leak count `0`; an injected leak → non-zero and fails | §2, §2.1 | `leaked_hits: 0`, exit `0`; self-check exit `0` after detecting its injection; manual leak → exit `1` with `leaks_detail` populated |
| **SC-002** | Every results file under `eval/` reports all four axes alongside recall and MRR | `uv run python -m memoratum.eval_longmemeval --data data/longmemeval_s_cleaned.json --n 10 --scope both --latency-baseline eval/BASELINES.md --cost-baseline eval/BASELINES.md` | emitted Markdown contains `partial-R@k`, `MRR`, an isolation block, a cost block, a latency block and a grounding block for the same run |
| **SC-003** | Re-running a committed results command reproduces its metric values and manifest on the recorded hardware | §7b | re-run diff over the manifest is empty; metric values match |
| **SC-004** | A latency regression beyond tolerance fails CI and names the phase | §4 then inject a 20 ms sleep into one phase, re-run | exit `1`, and `gate.checks[].phase` names `index` (not just "failed") |
| **SC-005** | A cost regression beyond tolerance fails CI and reports the delta | `uv run python -m memoratum.eval_cost --baseline eval/BASELINES.md`, then tighten `--retrieval-limit` upward | exit `1` and `gate.checks[].delta` is present and non-zero — a failure that reports no delta does not satisfy SC-005 |
| **SC-006** | Runs with SQLite only, no provider extra, no data leaving the host | §7c | completes with the provider extras **uninstalled**; `scripts/check_no_telemetry.py` clean; zero outbound connections |
| **SC-007** | An added ungrounded document is detected and reported | §5.1 | self-check exit `0` after detection; manual injection → `grounded_fraction < 1.0` and a populated `ungrounded[]` |

### 8.1. Reproducibility re-run (SC-003)

```bash
uv run python -m memoratum.eval_cost \
  --data data/longmemeval_s_cleaned.json --n 50 --seed 42 --out-json /tmp/run1.json
uv run python -m memoratum.eval_cost \
  --data data/longmemeval_s_cleaned.json --n 50 --seed 42 --out-json /tmp/run2.json

uv run python - <<'PY'
import json
a = json.load(open("/tmp/run1.json")); b = json.load(open("/tmp/run2.json"))
assert a["manifest"] == b["manifest"], "manifest drift"
assert a["cost"] == b["cost"], "metric drift"
print("manifest + metrics reproduce exactly")
print("hardware recorded:", "hardware" in a["manifest"].get("latency_config", {}))
PY
```

A non-empty diff is a defect: every reported figure MUST be reconstructible from
the recorded command and manifest (FR-007).

### 8.2. SQLite-only, offline verification (SC-006)

Run in an environment where the provider extras are **absent**, not merely unused:

```bash
uv run python -c "import memoratum.app" 2>&1 | tail -1   # must not require a provider
uv run python -m memoratum.eval_isolation --projects 2 --queries 10 --require-clean
uv run python -m memoratum.eval_grounding --n 5 --inject-ungrounded
uv run python scripts/check_no_telemetry.py
```

Expected: both axes complete on SQLite alone, and the telemetry check is clean.
The harness must not require `MEMORATUM_EMBEDDING_PROVIDER`, must not read any
API key, and must not open an outbound socket. Cost and grounding are pure
`sqlite3` + stdlib; latency uses `HashEmbedder` only, because network jitter
(100 ms ± 80 ms) would dwarf everything it measures (`L5`).

---

These are **correct** behaviours. Do not "fix" them.

## 9. Expected failure modes

These are **correct** behaviours. Do not "fix" them.

| Exit | Meaning |
|---|---|
| `eval_isolation` → `2` | The injected leak was **not** detected. The metric is broken. |
| `eval_isolation` → `1` | A real leak, with attribution in `leaks_detail`. |
| `eval_grounding` → `0` with `--inject-ungrounded` | The self-check detected its injection. |
| `eval_cost`/`eval_latency` → `2` | Baseline missing, or a latency baseline lacks hardware. |
| `eval_latency` → `3` | `--samples` below 20, or fewer than 3 ladder sizes. |
| `unscoped_cross_scope_hits > 0` | Correct. `project_id=null` means no scope requested. |
| `grounded_fraction` `null` for a kind | That kind was not exercised. Not a pass. |

## 10. Scope note

HotPotQA is **not** implemented here. Phase 0 research argued it should be admitted
for *evidence retrieval* rather than answer correctness; that was approved on
2026-10-04 and recorded as a scope amendment in `spec.md` and `plan.md`. It is
**Phase C, deferred**, and ships only after the four axes are merged and green.