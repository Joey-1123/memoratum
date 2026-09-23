# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Relevance re-scoring providers. Heuristic is built-in/offline; cross-encoder
is an optional lazy dependency (pip install sentence-transformers)."""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol


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


class HttpReranker:
    """Shared JSON reranker client for the documented v2/v1 HTTP shapes."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout: float = 30.0,
        retries: int = 3,
        opener: Any | None = None,
    ) -> None:
        if not endpoint:
            raise ValueError("a reranker endpoint is required")
        if not model:
            raise ValueError("a reranker model is required")
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.retries = max(1, retries)
        self.opener = opener or urllib.request.urlopen

    @staticmethod
    def _field(value: Any, name: str, default: Any = None) -> Any:
        if isinstance(value, Mapping):
            return value.get(name, default)
        return getattr(value, name, default)

    def score(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        body = json.dumps({"model": self.model, "query": query, "documents": docs}).encode()
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                request = urllib.request.Request(
                    self.endpoint,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
                    },
                    method="POST",
                )
                with self.opener(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode())
                results = self._field(payload, "results")
                if not isinstance(results, list):
                    raise TypeError("rerank response has no results list")
                scores = [0.0] * len(docs)
                for result in results:
                    index = int(self._field(result, "index", -1))
                    if 0 <= index < len(scores):
                        scores[index] = float(self._field(result, "relevance_score", 0.0) or 0.0)
                return scores
            except Exception as exc:  # noqa: BLE001 — retry transient provider failures
                last = exc
                if attempt + 1 < self.retries:
                    time.sleep(2**attempt)
        raise last or RuntimeError("rerank request failed")


class CohereReranker(HttpReranker):
    def __init__(
        self,
        *,
        model: str = "rerank-v3.5",
        api_key: str = "",
        endpoint: str = "https://api.cohere.com/v2/rerank",
        timeout: float = 30.0,
        retries: int = 3,
        opener: Any | None = None,
    ) -> None:
        super().__init__(
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            timeout=timeout,
            retries=retries,
            opener=opener,
        )


class VoyageReranker(HttpReranker):
    def __init__(
        self,
        *,
        model: str = "rerank-2.5",
        api_key: str = "",
        endpoint: str = "https://api.voyageai.com/v1/rerank",
        timeout: float = 30.0,
        retries: int = 3,
        opener: Any | None = None,
    ) -> None:
        super().__init__(
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            timeout=timeout,
            retries=retries,
            opener=opener,
        )


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


def build_reranker(
    kind: str,
    model: str = "",
    *,
    endpoint: str = "",
    api_key: str = "",
) -> Reranker:
    normalized = kind.strip().lower()
    if normalized in {"crossencoder", "cross-encoder", "local"}:
        return CrossEncoderReranker(model or "cross-encoder/ms-marco-MiniLM-L-6-v2")
    if normalized == "cohere":
        return CohereReranker(
            model=model or "rerank-v3.5",
            api_key=api_key,
            endpoint=endpoint or "https://api.cohere.com/v2/rerank",
        )
    if normalized == "voyage":
        return VoyageReranker(
            model=model or "rerank-2.5",
            api_key=api_key,
            endpoint=endpoint or "https://api.voyageai.com/v1/rerank",
        )
    return HeuristicReranker()
