# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Embedding providers. HashEmbedder is deterministic/offline; ApiEmbedder speaks
OpenAI-compatible `/embeddings` over stdlib HTTP with timeout + backoff."""

from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.request
from typing import Protocol


class Embedder(Protocol):
    dims: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Deterministic unit-norm vectors. Offline default for dev/test — not for quality."""

    def __init__(self, dims: int = 64) -> None:
        self.dims = dims

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
            rng = random.Random(seed)
            vec = [rng.gauss(0.0, 1.0) for _ in range(self.dims)]
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out


class ApiEmbedder:
    """OpenAI-compatible embeddings client (works with Ollama, vLLM, proxies)."""

    batch_size = 64

    def __init__(
        self, *, endpoint: str, model: str, api_key: str = "", dims: int = 0, timeout: float = 30.0
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.dims = dims
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._embed_batch(texts[i : i + self.batch_size]))
        return out

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        last: Exception | None = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    self.endpoint + "/embeddings",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as res:
                    data = json.loads(res.read().decode())
                items = sorted(data["data"], key=lambda d: d["index"])
                return [list(map(float, d["embedding"])) for d in items]
            except Exception as exc:  # noqa: BLE001 — retry transient failures, raise after
                last = exc
                time.sleep(2**attempt)
        raise last  # type: ignore[misc]
