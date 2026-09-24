# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The versioned contract for Memoratum's local Mem0-compatible routes.

This module describes the currently implemented self-hosted profile. It is not
an assertion of full compatibility with the hosted service or every SDK release.
"""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "mem0-self-hosted-v0.1"
ENTITY_FIELDS = ("user_id", "agent_id", "app_id", "run_id")
CAPABILITY_STATUSES = {"supported", "partial", "planned", "out_of_scope"}

CAPABILITIES: tuple[dict[str, str], ...] = (
    {
        "name": "add_memories",
        "status": "supported",
        "routes": "POST /v3/memories/add/; POST /v1/memories/",
        "notes": "Asynchronous document ingest; entity scope is required.",
    },
    {
        "name": "event_polling",
        "status": "supported",
        "routes": "GET /v1/event/{event_id}/",
        "notes": "PENDING, SUCCEEDED, and FAILED status mapping.",
    },
    {
        "name": "search_memories",
        "status": "supported",
        "routes": "POST /v3/memories/search/; POST /v1/memories/search/",
        "notes": "Entity-scoped hybrid search with optional rerank.",
    },
    {
        "name": "list_memories",
        "status": "supported",
        "routes": "GET /v1/memories/",
        "notes": "Entity-scoped current-fact listing.",
    },
    {
        "name": "python_sdk_add_search",
        "status": "supported",
        "routes": "POST /v3/memories/add/; POST /v3/memories/search/",
        "notes": "Official mem0ai Python client contract exercised in CI.",
    },
    {
        "name": "typescript_sdk_add_search",
        "status": "supported",
        "routes": "POST /v3/memories/add/; POST /v3/memories/search/",
        "notes": "Dependency-free TypeScript probe for the official mem0ai route shape.",
    },
    {
        "name": "get_memory",
        "status": "planned",
        "routes": "—",
        "notes": "Not implemented by this compatibility profile.",
    },
    {
        "name": "delete_all_memories",
        "status": "planned",
        "routes": "—",
        "notes": "Not implemented by this compatibility profile.",
    },
    {
        "name": "update_memory",
        "status": "planned",
        "routes": "—",
        "notes": "Not implemented by this compatibility profile.",
    },
    {
        "name": "delete_memory",
        "status": "planned",
        "routes": "—",
        "notes": "Not implemented by this compatibility profile.",
    },
    {
        "name": "history",
        "status": "planned",
        "routes": "—",
        "notes": "Not implemented by this compatibility profile.",
    },
    {
        "name": "bulk_operations",
        "status": "planned",
        "routes": "—",
        "notes": "Not implemented by this compatibility profile.",
    },
    {
        "name": "managed_billing",
        "status": "out_of_scope",
        "routes": "—",
        "notes": "Memoratum remains self-hosted and has no managed billing API.",
    },
)


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _entity_scopes(values: dict[str, Any]) -> list[str]:
    scopes: list[str] = []
    for key in ENTITY_FIELDS:
        if key in values:
            scopes.append(f"{key}:{_require_text(values[key], key)}")
    for key in ("AND", "OR"):
        nested = values.get(key)
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    scopes.extend(_entity_scopes(item))
    return scopes


def _entity_scope(values: dict[str, Any]) -> str:
    scopes = _entity_scopes(values)
    if not scopes:
        raise ValueError("an entity scope (user_id, agent_id, app_id, or run_id) is required")
    if len(scopes) > 1:
        raise ValueError("exactly one entity scope is required")
    return scopes[0]


def validate_add_request(payload: Any) -> dict[str, Any]:
    values = _require_mapping(payload, "add request")
    messages = values.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    for index, message in enumerate(messages):
        item = _require_mapping(message, f"messages[{index}]")
        role = item.get("role", "user")
        if role not in {"user", "assistant", "system"}:
            raise ValueError(f"messages[{index}].role is invalid")
        _require_text(item.get("content"), f"messages[{index}].content")
    if "containerTag" in values:
        container_tag = _require_text(values["containerTag"], "containerTag")
        if not container_tag.startswith("mem0:"):
            raise ValueError("containerTag must use the mem0 namespace")
        if any(key in values for key in ENTITY_FIELDS):
            raise ValueError("containerTag cannot be combined with an entity id")
        if not container_tag.startswith("mem0:") or container_tag.count(":") < 2:
            raise ValueError("containerTag must identify a mem0 entity")
    else:
        _entity_scope(values)
    return values


def validate_search_request(payload: Any) -> dict[str, Any]:
    values = _require_mapping(payload, "search request")
    _require_text(values.get("query"), "query")
    filters = _require_mapping(values.get("filters", {}), "filters")
    _entity_scope(filters)
    top_k = values.get("top_k", 10)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 100:
        raise ValueError("top_k must be an integer between 1 and 100")
    threshold = values.get("threshold", 0.0)
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or not 0 <= threshold <= 1
    ):
        raise ValueError("threshold must be between 0 and 1")
    return values


def validate_list_query(payload: Any) -> dict[str, Any]:
    values = _require_mapping(payload, "list query")
    _entity_scope(values)
    limit = values.get("limit", 100)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    return values


def validate_response(payload: Any, *, kind: str) -> dict[str, Any]:
    """Validate the stable response envelope used by the local profile."""
    values = _require_mapping(payload, f"{kind} response")
    if kind == "add":
        _require_text(values.get("event_id"), "event_id")
        if values.get("status") != "PENDING":
            raise ValueError("add response status must be PENDING")
    elif kind == "search":
        results = values.get("results")
        if not isinstance(results, list):
            raise ValueError("search response results must be a list")
        _validate_result_items(results, require_created_at=False)
    elif kind == "list":
        results = values.get("results")
        if not isinstance(results, list):
            raise ValueError("list response results must be a list")
        _validate_result_items(results, require_created_at=True)
    elif kind == "event":
        if values.get("status") not in {"PENDING", "SUCCEEDED", "FAILED"}:
            raise ValueError("event response status is invalid")
    elif kind == "error":
        error = values.get("error")
        if not isinstance(error, dict):
            raise ValueError("error response must contain an error object")
        _require_text(error.get("code"), "error.code")
        _require_text(error.get("message"), "error.message")
    else:
        raise ValueError(f"unknown response kind: {kind}")
    return values


def _validate_result_items(results: list[Any], *, require_created_at: bool) -> None:
    for index, result in enumerate(results):
        item = _require_mapping(result, f"results[{index}]")
        _require_text(item.get("id"), f"results[{index}].id")
        if not isinstance(item.get("memory"), str) and not isinstance(item.get("chunk"), str):
            raise TypeError(f"results[{index}] must contain a memory or chunk string")
        if "score" in item and (
            not isinstance(item["score"], (int, float)) or isinstance(item["score"], bool)
        ):
            raise ValueError(f"results[{index}].score must be numeric")
        if "metadata" in item and not isinstance(item["metadata"], dict):
            raise ValueError(f"results[{index}].metadata must be an object")
        if require_created_at and not isinstance(item.get("created_at"), (int, float)):
            raise ValueError(f"results[{index}].created_at must be numeric")
