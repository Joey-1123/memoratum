# Scope isolation

gate: PASS

## Scoped queries (the gate)
- scoped_queries: 5
- leaked_hits: 0  <- gate, must be 0
- leaked_queries: 0
- returned_hits: 100
- leak_rate: 0.000 (trend only, never gates)
- query_leak_rate: 0.000 (trend only, never gates)
- unresolvable_hits: 0

## Unscoped queries (informational)
- unscoped_queries: 10
- unscoped_cross_scope_hits: 200
- origins seen: proj-a, proj-b

Excluded from the gate numerator and denominator: `project_id=null` means no scope was requested, so cross-project hits are expected.

## Manifest
- schema: longmemeval-scoped-v2
- projects: proj-a, proj-b
- expected_documents: 80
- embedder: HashEmbedder:64

## Regenerate this figure

```bash
python -m memoratum.eval_isolation --projects 2 --queries 5 --seed 42 --out-md eval/RESULTS-isolation.md --out-json eval/isolation.json
```
