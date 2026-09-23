# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Embedding providers. HashEmbedder is deterministic/offline; ApiEmbedder speaks
OpenAI-compatible `/embeddings` over stdlib HTTP with timeout + backoff."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import sys
import time
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol

from memoratum.llm import ProviderUnavailable


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


class LiteLLMEmbedder:
    """Optional LiteLLM embedding adapter using its OpenAI-shaped response."""

    batch_size = 64

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        endpoint: str = "",
        dims: int = 0,
        timeout: float = 30.0,
        retries: int = 3,
        client: Any | None = None,
    ) -> None:
        if not model:
            raise ValueError("a model is required for the LiteLLM embedding provider")
        self.model = model
        self.api_key = api_key
        self.endpoint = endpoint
        self.dims = dims
        self.timeout = timeout
        self.retries = max(1, retries)
        self._client = client

    @staticmethod
    def _field(value: Any, name: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    @staticmethod
    def _disable_telemetry(client: Any) -> Any:
        if hasattr(client, "telemetry"):
            client.telemetry = False
        return client

    def _load_client(self) -> Any:
        if self._client is not None:
            return self._disable_telemetry(self._client)
        try:
            import litellm
        except ImportError as exc:
            raise ProviderUnavailable(
                "the litellm package is not installed; install the llm extra "
                "or configure the OpenAI-compatible endpoint"
            ) from exc
        self._client = self._disable_telemetry(litellm)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._load_client()
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            window = texts[start : start + self.batch_size]
            kwargs: dict[str, Any] = {
                "model": self.model,
                "input": window,
                "timeout": self.timeout,
                "num_retries": self.retries,
            }
            if self.endpoint:
                kwargs["api_base"] = self.endpoint
            if self.api_key:
                kwargs["api_key"] = self.api_key
            response = client.embedding(**kwargs)
            data = self._field(response, "data")
            if not isinstance(data, list):
                raise TypeError("embedding response has no data list")
            ordered = sorted(data, key=lambda item: int(self._field(item, "index") or 0))
            vectors = [
                [float(value) for value in (self._field(item, "embedding") or [])]
                for item in ordered
            ]
            if len(vectors) != len(window):
                raise ValueError("embedding response count does not match input count")
            out.extend(vectors)
        if out:
            width = len(out[0])
            if self.dims and width != self.dims:
                raise ValueError(f"embedding dimension mismatch: expected {self.dims}, got {width}")
            self.dims = width
        return out


class FastEmbedEmbedder:
    """Lazy local ONNX embedding adapter."""

    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-small-en-v1.5",
        dims: int = 0,
        model: Any | None = None,
    ) -> None:
        self.model_name = model_name
        self.dims = dims
        self._model = model

    def _load_model(self) -> Any:
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise ProviderUnavailable(
                    "the fastembed package is not installed; install the embeddings extra"
                ) from exc
            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load_model()
        vectors = [[float(value) for value in vector] for vector in model.embed(texts)]
        if vectors:
            width = len(vectors[0])
            if self.dims and width != self.dims:
                raise ValueError(f"embedding dimension mismatch: expected {self.dims}, got {width}")
            self.dims = width
        return vectors


def _optional_installed(name: str) -> bool:
    module = sys.modules.get(name)
    if module is not None:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def build_embedder(
    provider: str,
    *,
    endpoint: str,
    model: str,
    api_key: str = "",
    dims: int = 0,
    timeout: float = 30.0,
) -> Embedder:
    """Build an embedding provider while keeping optional packages lazy."""
    normalized = provider.strip().lower()
    if normalized in {"", "hash", "offline"}:
        return HashEmbedder(dims or 64)
    if normalized in {"api", "openai", "openai-compatible", "ollama", "vllm"}:
        if not endpoint or not model:
            return HashEmbedder(dims or 64)
        return ApiEmbedder(
            endpoint=endpoint, model=model, api_key=api_key, dims=dims, timeout=timeout
        )
    if normalized in {"litellm", "gateway"}:
        if not model:
            return HashEmbedder(dims or 64)
        if _optional_installed("litellm"):
            return LiteLLMEmbedder(
                model=model,
                endpoint=endpoint,
                api_key=api_key,
                dims=dims,
                timeout=timeout,
            )
        if endpoint:
            return ApiEmbedder(
                endpoint=endpoint, model=model, api_key=api_key, dims=dims, timeout=timeout
            )
        raise ProviderUnavailable(
            "litellm is not installed and no OpenAI-compatible endpoint was configured"
        )
    if normalized in {"fastembed", "local"}:
        return FastEmbedEmbedder(model_name=model or "BAAI/bge-small-en-v1.5", dims=dims)
    raise ValueError(f"unknown embedding provider: {provider}")
