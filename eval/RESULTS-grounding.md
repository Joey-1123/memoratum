# Grounding

gate: PASS

- questions evaluated: 25
- hits_checked: 250
- grounded_hits: 250
- ungrounded_hits: 0
- unresolvable_hits: 0
- grounded_fraction: 1.000
- rows resolved: 250 (one keyed lookup per hit)

## Per-kind rules

- normalization: nfkc+whitespace+casefold
- chunk: chunks.text | checked 250, grounded 250, fraction 1.000
- fact: subject predicate object | checked 0, grounded 0, fraction n/a
- memory: memories.text | checked 0, grounded 0, fraction n/a

## Tier 2 diagnostics (never gate)
- fts5_snippet: '...The fox cannot be left alone [with] the...'

Grounding proves **provenance only** — that a hit came from this corpus. It never
establishes correctness or relevance; recall and MRR own those, and a grounded
answer built from the wrong document is caught by the isolation and recall axes.
- external judge: none (optional, non-gating)

## Falsifiability self-check
- detected injected ungrounded text: True
- injected_ungrounded: 1
- false positives on real hits: 0
- detection proven: 1 injected ungrounded hit(s) found, 0 false positives on real hits

## Regenerate this figure

```bash
python -m memoratum.eval_grounding --data data/longmemeval_s_cleaned.json --n 25 --seed 42 --inject-ungrounded --tier2 --out-md eval/RESULTS-grounding.md --out-json /tmp/memoratum-regen-nh7jr3qe/regen.json
```
