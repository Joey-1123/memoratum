# HotPotQA — evidence retrieval

dataset: hotpotqa-evidence (fullwiki)
gate metric: (title, sent_id) recall, scored by exact-set

- questions scored: 50 of 50
- evidence_recall: 0.620
- evidence_precision: 0.146
- evidence_f1: 0.233
- joint_evidence_rate: 0.340  (both hops found in every question)

- paragraphs indexed: 490
- sentences indexed: 2052 (one sentence = one document)
- retrieval limit: 10 sentences

## What this does not measure

**Answers are not scored.** Phase C is admitted for evidence retrieval only. Answer accuracy was the basis for rejecting this dataset during planning, and adding it back would reopen a decision the spec closed.

**This is not full-wiki scale.** Sentences are indexed from the union of provided contexts (~74K paragraphs), which is NOT true full-wiki scale (~5M articles). This is multi-hop evidence recall under a ~74K-paragraph distractor load.

A hop is a document, not a sentence: finding two sentences of one title is one hop.

## What this figure is conditional on

- questions excluded (gold outside the provided context): 5317 of 7405 (71.8%)

The figures above are **conditional**: they hold for the 50 questions where the dataset's own fullwiki retrieval placed every gold fact inside the provided context. Questions where it did not are excluded and counted above, never scored 0.0 — scoring them would measure the dataset's retriever, not the memory layer under test.

## By question type

| type | n | evidence_recall |
|---|---:|---:|
| bridge | 41 | 0.599 |
| comparison | 9 | 0.713 |

## Questions with missed evidence

- 5ae32ce75542992f92d8223d (bridge): 1/2 hops, recall 0.500, gold [('Nikita Gross', 0), ('Nikita Gross', 3), ('Perfect 10', 0), ('Perfect 10', 2)]
- 5a8e494e5542995a26add4c8 (bridge): 1/2 hops, recall 0.500, gold [("MacLellan's Castle", 0), ("MacLellan's Castle", 1), ('Thomas Maclellan of Bombie', 0), ('Thomas Maclellan of Bombie', 1)]
- 5ac02c6b554299294b218f38 (bridge): 1/2 hops, recall 0.333, gold [('Hey Ya!', 0), ('Hey Ya!', 1), ('The Ed Sullivan Show', 0)]
- 5a905d085542995651fb5147 (bridge): 2/2 hops, recall 0.500, gold [('Lockheed C-130 Hercules', 4), ('Mercedes-Benz Zetros', 3)]
- 5a75d35d5542992d0ec05ffe (comparison): 2/2 hops, recall 0.250, gold [("America's Incredible Pizza Company", 0), ("America's Incredible Pizza Company", 2), ('Toppers Pizza (American restaurant)', 0), ('Toppers Pizza (American restaurant)', 2)]
- 5add311b5542994ed6169cc5 (bridge): 2/2 hops, recall 0.500, gold [('Raymond Ochoa', 1), ('The Good Dinosaur', 2)]
- 5ac056385542997d64295a08 (bridge): 2/2 hops, recall 0.500, gold [('Ambrose Mendy', 0), ('Ambrose Mendy', 1), ('Chris Eubank Jr.', 0), ('Chris Eubank Jr.', 2)]
- 5a89ab1355429946c8d6e996 (comparison): 1/2 hops, recall 0.500, gold [('Carnegie Hall Tower', 0), ('Staten Island', 0)]
- 5aba89d555429901930fa816 (bridge): 1/2 hops, recall 0.500, gold [('Dru Hill', 1), ('Woody Rock', 2)]
- 5ab56b3a554299637185c585 (bridge): 2/2 hops, recall 0.500, gold [('Matt McKenzie', 3), ("That '70s Show", 1)]
- 5a75227955429929fddd8508 (bridge): 1/2 hops, recall 0.500, gold [('Modbury, South Australia', 0), ('Westfield Tea Tree Plaza', 3)]
- 5ae184185542990adbacf7ac (comparison): 2/2 hops, recall 0.667, gold [('Idaho Falls Regional Airport', 0), ('Waco Regional Airport', 0), ('Waco Regional Airport', 1)]
- 5ac370ce5542995ef918c18c (comparison): 1/2 hops, recall 0.500, gold [('Henry E. Rohlsen Airport', 0), ('Southwest Florida International Airport', 0)]
- 5ae3c0245542990afbd1e1c3 (bridge): 2/2 hops, recall 0.500, gold [('Mortdecai (film)', 2), ('Olivia Munn', 0)]
- 5ae1d32a554299492dc91b9c (bridge): 1/2 hops, recall 0.333, gold [('Fifty Shades (novel series)', 0), ('Fifty Shades (novel series)', 1), ('Fifty Shades Freed', 0)]
- 5abafb6d5542992ccd8e7e92 (bridge): 1/2 hops, recall 0.500, gold [('Starship Entertainment', 0), ('White Love (Starship Planet song)', 0)]
- 5ae81032554299540e5a572a (bridge): 0/2 hops, recall 0.000, gold [('Electoral district of Bass', 1), ('Electoral district of Bass', 2), ('Pakenham, Victoria', 0)]
- 5ac06e24554299294b218fd1 (bridge): 2/2 hops, recall 0.000, gold [("1891 Cincinnati Kelly's Killers season", 1), ('King Kelly', 0)]
- 5a8a1b7a5542992d82986ec0 (bridge): 2/2 hops, recall 0.750, gold [('David Walker (abolitionist)', 2), ('Edward G. Walker', 0), ('Edward G. Walker', 1), ('Edward G. Walker', 2)]
- 5a7a650355429941d65f2615 (bridge): 1/2 hops, recall 0.500, gold [('Gawvi', 0), ('Lecrae', 0)]
- ... and 13 more

## Regenerate this figure

```bash
python -m memoratum.eval_hotpotqa --data data/hotpot_dev_fullwiki_v1.json --n 50 --seed 42 --k 10 --allow-unmeasurable-gold --baseline eval/BASELINES.md --out-md eval/RESULTS-evidence-real.md --out-json eval/evidence-real.json
```
