# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The versioned contract for Memoratum's local Mem0-compatible routes.

This module describes the currently implemented self-hosted profile. It is not
an assertion of full compatibility with the hosted service or every SDK release.
"""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "mem0-self-hosted-v0.2"
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
        "status": "supported",
        "routes": "GET /v1/memories/{memory_id}/",
        "notes": "Canonical scoped memory lookup.",
    },
    {
        "name": "update_memory",
        "status": "supported",
        "routes": "PUT /v1/memories/{memory_id}/",
        "notes": "Partial text, metadata, timestamp, and expiration updates.",
    },
    {
        "name": "delete_memory",
        "status": "supported",
        "routes": "DELETE /v1/memories/{memory_id}/",
        "notes": "Redacted tombstone history and provider cleanup.",
    },
    {
        "name": "delete_all_memories",
        "status": "supported",
        "routes": "DELETE /v1/memories/",
        "notes": "Explicit entity filters only; returns an asynchronous event_id.",
    },
    {
        "name": "history",
        "status": "supported",
        "routes": "GET /v1/memories/{memory_id}/history/",
        "notes": "List-shaped append-only local history.",
    },
    {
        "name": "bulk_operations",
        "status": "supported",
        "routes": "PUT /v1/batch/; DELETE /v1/batch/",
        "notes": "Atomic batches up to 1000 items.",
    },
    {
        "name": "ping_projects_members",
        "status": "supported",
        "routes": "GET /v1/ping/; /api/v1/orgs/organizations/{org_id}/projects/",
        "notes": "Local default tenant plus project/member compatibility routes.",
    },
    {
        "name": "webhooks",
        "status": "supported",
        "routes": "/api/v1/webhooks/projects/{project_id}/; /api/v1/webhooks/{webhook_id}/",
        "notes": "Opt-in signed local delivery; managed remote billing is not included.",
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


def validate_update_request(payload: Any) -> dict[str, Any]:
    values = _require_mapping(payload, "update request")
    allowed = {"text", "metadata", "timestamp", "expiration_date"}
    if not values or set(values) - allowed:
        raise ValueError("update must contain only text, metadata, timestamp, or expiration_date")
    if "text" in values:
        _require_text(values["text"], "text")
    if "metadata" in values and not isinstance(values["metadata"], dict):
        raise ValueError("metadata must be an object")
    if "expiration_date" in values and values["expiration_date"] is not None:
        value = values["expiration_date"]
        if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
            raise ValueError("expiration_date must be YYYY-MM-DD")
    return values


def validate_batch_request(payload: Any, *, update: bool) -> dict[str, Any]:
    values = _require_mapping(payload, "batch request")
    items = values.get("memories")
    if not isinstance(items, list) or not items:
        raise ValueError("memories must be a non-empty list")
    if len(items) > 1000:
        raise ValueError("a batch may contain at most 1000 memories")
    allowed = {"memory_id", "text", "metadata"} if update else {"memory_id"}
    seen: set[str] = set()
    for index, item in enumerate(items):
        item = _require_mapping(item, f"memories[{index}]")
        if set(item) - allowed:
            raise ValueError(f"memories[{index}] contains unsupported fields")
        memory_id = _require_text(item.get("memory_id"), f"memories[{index}].memory_id")
        if memory_id in seen:
            raise ValueError("memory_id values must be unique within a batch")
        seen.add(memory_id)
        if update:
            if "text" not in item and "metadata" not in item:
                raise ValueError(f"memories[{index}] must include text or metadata")
            if "text" in item:
                _require_text(item["text"], f"memories[{index}].text")
            if "metadata" in item and not isinstance(item["metadata"], dict):
                raise ValueError(f"memories[{index}].metadata must be an object")
    return values


def validate_webhook_request(payload: Any) -> dict[str, Any]:
    values = _require_mapping(payload, "webhook request")
    _require_text(values.get("url"), "url")
    _require_text(values.get("name"), "name")
    events = values.get("event_types")
    if not isinstance(events, list) or not events:
        raise ValueError("event_types must be a non-empty list")
    if any(not isinstance(event, str) or not event for event in events):
        raise ValueError("event_types must contain strings")
    return values


def validate_get_all_request(payload: Any) -> dict[str, Any]:
    values = _require_mapping(payload, "get-all request")
    _entity_scope(_require_mapping(values.get("filters"), "filters"))
    return values


def validate_list_query(payload: Any) -> dict[str, Any]:
    values = _require_mapping(payload, "list query")
    _entity_scope(values)
    limit = values.get("limit", 100)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    return values


def validate_response(payload: Any, *, kind: str) -> dict[str, Any] | list[Any]:
    """Validate the stable response envelope used by the local profile."""
    if kind == "history":
        if not isinstance(payload, list):
            raise TypeError("history response must be a list")
        for index, item in enumerate(payload):
            entry = _require_mapping(item, f"history[{index}]")
            _require_text(entry.get("event"), f"history[{index}].event")
        return payload
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
    elif kind in {"get", "update"}:
        _require_text(values.get("id"), "id")
        _require_text(values.get("memory"), "memory")
        if "metadata" in values and not isinstance(values["metadata"], dict):
            raise ValueError("metadata must be an object")
    elif kind == "delete":
        _require_text(values.get("message"), "message")
    elif kind == "delete_all":
        _require_text(values.get("message"), "message")
        _require_text(values.get("event_id"), "event_id")
    elif kind == "batch":
        _require_text(values.get("message"), "message")
    elif kind == "get_all":
        _validate_result_items(values.get("results"), require_created_at=True)
        for key in ("count", "next", "previous"):
            if key not in values:
                raise ValueError(f"get-all response must contain {key}")
    elif kind == "ping":
        if values.get("status") != "ok":
            raise ValueError("ping response status must be ok")
        _require_text(values.get("org_id"), "org_id")
        _require_text(values.get("project_id"), "project_id")
    elif kind == "webhook":
        _require_text(values.get("id"), "id")
        _require_text(values.get("name"), "name")
        _require_text(values.get("url"), "url")
        if not isinstance(values.get("event_types"), list):
            raise ValueError("webhook event_types must be a list")
    elif kind == "webhook_event":
        details = _require_mapping(values.get("event_details"), "event_details")
        _require_text(details.get("id"), "event_details.id")
        _require_text(details.get("event"), "event_details.event")
        _require_mapping(details.get("data"), "event_details.data")
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
        if require_created_at and not isinstance(item.get("created_at"), (int, float, str)):
            raise ValueError(f"results[{index}].created_at must be numeric or ISO-8601")
