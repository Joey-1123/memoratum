# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Session-level retrieval metrics (LongMemEval-style harness support)."""


def session_ids_of(ranked_sessions: list[str]) -> list[str]:
    seen: list[str] = []
    for s in ranked_sessions:
        if s not in seen:
            seen.append(s)
    return seen


def partial_recall(ranked: list[str], gold: set[str], *, k: int) -> float:
    top = set(session_ids_of(ranked)[:k])
    return 1.0 if top & gold else 0.0


def full_recall(ranked: list[str], gold: set[str], *, k: int) -> float:
    top = set(session_ids_of(ranked)[:k])
    return 1.0 if gold and gold <= top else 0.0


def mrr(ranked: list[str], gold: set[str]) -> float:
    for i, s in enumerate(session_ids_of(ranked), start=1):
        if s in gold:
            return 1.0 / i
    return 0.0
