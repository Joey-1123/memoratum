# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: MIT (see LICENSE-MIT; client code exception to repo AGPL)

"""Python SDK: thin typed wrapper over the HTTP API (stdlib only)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


class MemoratumError(Exception):
    """Raised for transport failures and API error envelopes."""


class _RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class Client:
    def __init__(self, *, base_url: str, api_key: str = "", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._with_retries("POST", path, body)

    def profile(self, *, container_tag: str = "default") -> dict[str, Any]:
        return self._with_retries("GET", f"/v4/profile?containerTag={container_tag}", None)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = "" if not params else "?" + "&".join(f"{k}={v}" for k, v in params.items())
        return self._with_retries("GET", path + query, None)

    def delete(self, path: str) -> Any:
        return self._with_retries("DELETE", path, None)

    def _with_retries(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        waits = 0
        last: Exception | None = None
        for attempt in range(3):
            try:
                return self._send(method, path, body)
            except _RateLimited as e:
                waits += 1
                if waits > 30:
                    raise MemoratumError("rate limited") from e
                time.sleep(e.retry_after)
            except Exception as exc:  # noqa: BLE001 — retry transient failures
                last = exc
                time.sleep(2**attempt)
        raise MemoratumError(str(last))

    def _send(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        data = json.dumps(body or {}).encode() if method == "POST" else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = 1
                raw = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    retry_after = max(1, min(60, int(str(raw))))
                except (TypeError, ValueError):
                    pass
                raise _RateLimited(retry_after)
            try:
                payload = json.loads(exc.read().decode())
                raise MemoratumError(
                    payload.get("error", {}).get("message", f"HTTP {exc.code}")
                ) from exc
            except (ValueError, KeyError):
                raise MemoratumError(f"HTTP {exc.code}") from exc

    def add(
        self,
        content: str,
        *,
        container_tag: str = "default",
        custom_id: str | None = None,
        dreaming: str = "dynamic",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.post(
            "/v3/documents",
            {
                "content": content,
                "containerTag": container_tag,
                "customId": custom_id,
                "dreaming": dreaming,
                "metadata": metadata,
            },
        )

    def search(
        self,
        q: str,
        *,
        container_tag: str = "default",
        search_mode: str = "hybrid",
        limit: int = 10,
        threshold: float = 0.0,
        filters: dict[str, Any] | None = None,
        rerank: bool = False,
    ) -> dict[str, Any]:
        return self.post(
            "/v4/search",
            {
                "q": q,
                "containerTag": container_tag,
                "searchMode": search_mode,
                "limit": limit,
                "threshold": threshold,
                "filters": filters,
                "rerank": rerank,
            },
        )
