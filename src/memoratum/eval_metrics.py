# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Session-level retrieval metrics (LongMemEval-style harness support).

Unit conventions
----------------
``ranked`` is one session id **per retrieved hit**, in rank order. The two
conventions in this module are deliberately different:

- ``recall@k`` slices the *hit* list first, then deduplicates. It answers "was a
  gold session among the first ``k`` retrieval positions?".
- ``mrr`` deduplicates first and has no ``k``. It answers "how many distinct
  sessions precede the first gold session?", which is the standard session-space
  formulation.

Slicing after deduplication (the v0 behaviour, shipped in the v0 results files)
made ``recall@k`` mean "are there ``k`` distinct sessions anywhere in the
retrieved set". On a corpus averaging ~7.6 chunks per session a 20-hit budget
yields only ~2.6 distinct sessions, so ``R@5`` and ``R@10`` were identical.
Because the sessions visible under the corrected definition are always a
*subset* of those visible under v0, corrected scores can only be lower or equal
-- a decrease is a correction, not a retrieval regression.
"""


def session_ids_of(ranked_sessions: list[str]) -> list[str]:
    seen: list[str] = []
    for s in ranked_sessions:
        if s not in seen:
            seen.append(s)
    return seen


def partial_recall(ranked: list[str], gold: set[str], *, k: int) -> float:
    top = set(session_ids_of(ranked[:k]))
    return 1.0 if top & gold else 0.0


def full_recall(ranked: list[str], gold: set[str], *, k: int) -> float:
    top = set(session_ids_of(ranked[:k]))
    return 1.0 if gold and gold <= top else 0.0


def mrr(ranked: list[str], gold: set[str]) -> float:
    for i, s in enumerate(session_ids_of(ranked), start=1):
        if s in gold:
            return 1.0 / i
    return 0.0
