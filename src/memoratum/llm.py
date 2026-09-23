# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Provider-neutral chat models.

The built-in adapter uses the OpenAI-compatible HTTP shape. The optional
LiteLLM adapter follows the documented ``completion(model, messages, ...)``
contract and is loaded lazily, so the default installation remains dependency-light.

Sources:
- https://docs.litellm.ai/docs/completion/input
- https://docs.litellm.ai/docs/providers/openai_compatible
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol


class ChatModel(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class ProviderUnavailable(RuntimeError):
    """Raised when an explicitly selected optional provider cannot be loaded."""


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _response_content(response: Any) -> str:
    choices = _field(response, "choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("chat response has no choices")
    message = _field(choices[0], "message")
    content = _field(message, "content") if message is not None else None
    if not isinstance(content, str) or not content:
        raise ValueError("chat response has no message content")
    return content


class OpenAICompatibleChat:
    """Small stdlib client for OpenAI-compatible chat-completions endpoints."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout: float = 60.0,
        retries: int = 3,
    ) -> None:
        if not endpoint:
            raise ValueError("an endpoint is required for the OpenAI-compatible provider")
        if not model:
            raise ValueError("a model is required for the OpenAI-compatible provider")
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.retries = max(1, retries)

    def complete(self, system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
            }
        ).encode()
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                request = urllib.request.Request(
                    self.endpoint + "/chat/completions",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode())
                return _response_content(payload)
            except Exception as exc:  # noqa: BLE001 — retry transient provider failures
                last = exc
                if attempt + 1 < self.retries:
                    time.sleep(2**attempt)
        raise last or RuntimeError("chat completion failed")


class LiteLLMChat:
    """Optional LiteLLM-backed chat adapter with a small, testable surface."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        api_base: str = "",
        timeout: float = 60.0,
        retries: int = 3,
        client: Any | None = None,
    ) -> None:
        if not model:
            raise ValueError("a model is required for the LiteLLM provider")
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.timeout = timeout
        self.retries = max(1, retries)
        self._client = client

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
                "the litellm package is not installed; install the provider extra "
                "or configure the OpenAI-compatible endpoint"
            ) from exc
        self._client = self._disable_telemetry(litellm)
        return self._client

    def complete(self, system: str, user: str) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "timeout": self.timeout,
            "num_retries": self.retries,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        response = self._load_client().completion(**kwargs)
        return _response_content(response)


def _litellm_installed() -> bool:
    module = sys.modules.get("litellm")
    if module is not None:
        return True
    try:
        return importlib.util.find_spec("litellm") is not None
    except (ImportError, ValueError):
        return False


def build_chat(
    provider: str,
    *,
    endpoint: str,
    model: str,
    api_key: str = "",
    timeout: float = 60.0,
    retries: int = 3,
) -> ChatModel | None:
    """Build a configured chat model without making the optional dependency mandatory."""
    normalized = provider.strip().lower()
    if normalized in {"", "openai", "openai-compatible", "ollama", "vllm"}:
        if not model or not endpoint:
            return None
        return OpenAICompatibleChat(
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            timeout=timeout,
            retries=retries,
        )
    if normalized in {"litellm", "gateway"}:
        if not model:
            return None
        if _litellm_installed():
            return LiteLLMChat(
                model=model,
                api_key=api_key,
                api_base=endpoint,
                timeout=timeout,
                retries=retries,
            )
        if endpoint:
            return OpenAICompatibleChat(
                endpoint=endpoint,
                model=model,
                api_key=api_key,
                timeout=timeout,
                retries=retries,
            )
        raise ProviderUnavailable(
            "litellm is not installed and no OpenAI-compatible endpoint was configured"
        )
    raise ValueError(f"unknown LLM provider: {provider}")
