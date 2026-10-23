# HotPotQA — evidence retrieval

dataset: hotpotqa-evidence (fullwiki)
gate metric: (title, sent_id) recall, scored by exact-set

- questions scored: 2 of 2
- evidence_recall: 0.583
- evidence_precision: 0.300
- evidence_f1: 0.393
- joint_evidence_rate: 0.000  (both hops found in every question)

- paragraphs indexed: 5
- sentences indexed: 14 (one sentence = one document)
- retrieval limit: 5 sentences

## What this does not measure

**Answers are not scored.** Phase C is admitted for evidence retrieval only. Answer accuracy was the basis for rejecting this dataset during planning, and adding it back would reopen a decision the spec closed.

**This is not full-wiki scale.** Sentences are indexed from the union of provided contexts (~74K paragraphs), which is NOT true full-wiki scale (~5M articles). This is multi-hop evidence recall under a ~74K-paragraph distractor load.

A hop is a document, not a sentence: finding two sentences of one title is one hop.

## By question type

| type | n | evidence_recall |
|---|---:|---:|
| bridge | 1 | 0.500 |
| comparison | 1 | 0.667 |

## Questions with missed evidence

- fixture-q1 (bridge): 1/2 hops, recall 0.500, gold [('City of Aldmere', 2), ('Northern Observatory', 0)]
- fixture-q2 (comparison): 2/3 hops, recall 0.667, gold [('Aldmere Transit', 1), ('Harbour Bridge', 0), ('Northern Observatory', 0)]

## Regenerate this figure

```bash
python -m memoratum.eval_hotpotqa --data eval/fixtures/hotpotqa_evidence_smoke.json --n 2 --seed 42 --k 5 --baseline eval/BASELINES.md --out-json /tmp/opencode/evidence.json --out-md /tmp/opencode/RESULTS-evidence.md
```
