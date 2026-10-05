# Scope isolation

gate: PASS

## Scoped queries (the gate)
- scoped_queries: 12
- leaked_hits: 0  <- gate, must be 0
- leaked_queries: 0
- returned_hits: 120
- leak_rate: 0.000 (trend only, never gates)
- query_leak_rate: 0.000 (trend only, never gates)
- unresolvable_hits: 0

## Unscoped queries (informational)
- unscoped_queries: 4
- unscoped_cross_scope_hits: 40
- origins seen: proj-a, proj-b, proj-c

Excluded from the gate numerator and denominator: `project_id=null` means no scope was requested, so cross-project hits are expected.

## Falsifiability self-check
- detected injected leak: True
- injected_leaks: 12
- false positives on a clean query: 0
- detection proven: 12 injected cross-scope hits found, 0 false positives on a correctly scoped query

## Manifest
- schema: longmemeval-scoped-v2
- projects: proj-a, proj-b, proj-c
- expected_documents: 60
- embedder: HashEmbedder:64
