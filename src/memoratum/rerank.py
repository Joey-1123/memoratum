# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Relevance re-scoring providers. Heuristic is built-in/offline; cross-encoder
is an optional lazy dependency (pip install sentence-transformers)."""

from __future__ import annotations

from typing import Protocol


class Reranker(Protocol):
    def score(self, query: str, docs: list[str]) -> list[float]: ...


STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "which",
        "what",
        "who",
        "whom",
        "whose",
        "where",
        "when",
        "why",
        "how",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "do",
        "does",
        "did",
        "will",
        "would",
        "can",
        "could",
        "should",
        "have",
        "has",
        "had",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "there",
        "their",
        "my",
        "your",
        "his",
        "her",
        "our",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "from",
        "with",
        "by",
        "and",
        "or",
        "not",
        "no",
    }
)


def _tokens(text: str) -> set[str]:
    return {
        t.strip(".,;:!?()[]{}\"'").lower()
        for t in text.split()
        if t.strip(".,;:!?()[]{}\"'").lower() not in STOPWORDS
    }


class HeuristicReranker:
    """Precision of query-token coverage per doc. Deterministic, offline."""

    def score(self, query: str, docs: list[str]) -> list[float]:
        qtokens = _tokens(query)
        if not qtokens:
            return [0.0] * len(docs)
        out = []
        for doc in docs:
            dtokens = _tokens(doc)
            out.append(len(qtokens & dtokens) / len(qtokens))
        return out


class CrossEncoderReranker:
    """query,doc cross-encoder via sentence-transformers (lazy import)."""

    def __init__(self, model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2") -> None:
        self.model = model
        self._ranker = None

    def _load(self):
        if self._ranker is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError:
                raise RuntimeError(
                    "sentence-transformers is not installed; pip install sentence-transformers"
                ) from None
            try:
                self._ranker = CrossEncoder(self.model)
            except Exception as exc:
                raise RuntimeError(f"cross-encoder model unavailable: {exc}") from exc
        return self._ranker

    def score(self, query: str, docs: list[str]) -> list[float]:
        return [float(s) for s in self._load().predict([[query, d] for d in docs])]


def build_reranker(kind: str, model: str = "") -> Reranker:
    if kind == "crossencoder":
        return CrossEncoderReranker(model or "cross-encoder/ms-marco-MiniLM-L-6-v2")
    return HeuristicReranker()
