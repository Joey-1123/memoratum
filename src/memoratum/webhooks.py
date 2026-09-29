# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local, opt-in webhook subscriptions and signed delivery."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from typing import Any

MEMORY_EVENTS = frozenset({"memory_add", "memory_update", "memory_delete", "memory_categorize"})
INGEST_EVENTS = frozenset(
    {
        "ingest_job_completed",
        "ingest_job_partially_completed",
        "ingest_job_failed",
        "ingest_job_cancelled",
    }
)
SUPPORTED_EVENTS = MEMORY_EVENTS | INGEST_EVENTS

_RESERVED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata",
        "instance-data",
    }
)


class UnsafeWebhookTarget(ValueError):
    """Raised when a webhook URL violates the outbound network policy."""


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_webhook_url(
    url: str,
    *,
    allow_private: bool = False,
    resolver: Callable[..., Iterable[Any]] | None = None,
) -> str:
    """Validate an endpoint and return its normalized URL.

    HTTPS is required by default. Private and loopback destinations are only
    accepted when the caller explicitly enables the development-only policy.
    Every resolved address is checked, and redirects are disabled by delivery.
    """
    from urllib.parse import urlsplit, urlunsplit

    if not isinstance(url, str) or not url.strip():
        raise UnsafeWebhookTarget("webhook url is required")
    raw = url.strip()
    if len(raw) > 2048:
        raise UnsafeWebhookTarget("webhook url is too long")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"https", "http"}:
        raise UnsafeWebhookTarget("webhook url must use http or https")
    if parsed.scheme == "http" and not allow_private:
        raise UnsafeWebhookTarget("webhook url must use https")
    if parsed.username or parsed.password or parsed.fragment:
        raise UnsafeWebhookTarget("webhook url must not contain credentials or a fragment")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeWebhookTarget("webhook url must include a host")
    if hostname.lower() in _RESERVED_HOSTS and not allow_private:
        raise UnsafeWebhookTarget("webhook host is not allowed")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeWebhookTarget("webhook url has an invalid port") from exc
    lookup = resolver or socket.getaddrinfo
    try:
        addresses = lookup(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeWebhookTarget("webhook host could not be resolved") from exc
    resolved: list[str] = []
    for item in addresses:
        if isinstance(item, tuple) and len(item) >= 5:
            sockaddr = item[4]
            if isinstance(sockaddr, tuple) and sockaddr:
                resolved.append(str(sockaddr[0]))
        elif isinstance(item, tuple) and len(item) >= 2 and isinstance(item[0], str):
            resolved.append(item[0])
        elif isinstance(item, str):
            resolved.append(item)
    if not resolved:
        raise UnsafeWebhookTarget("webhook host has no usable address")
    if not allow_private and any(not _is_public_ip(address) for address in resolved):
        raise UnsafeWebhookTarget("webhook host resolves to a private or reserved address")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _secret_bytes(secret: str) -> bytes:
    return secret.encode()


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    """Return the documented sha256 HMAC signature for a delivery body."""
    signed = timestamp.encode() + b"." + body
    digest = hmac.new(_secret_bytes(secret), signed, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def _event_row_to_delivery(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "webhook_id": row["webhook_id"],
        "project_id": row["project_id"],
        "event_type": row["event_type"],
        "memory_id": row["memory_id"],
        "status": row["status"],
        "attempts": row["attempts"],
        "next_attempt_at": row["next_attempt_at"],
        "last_error": row["last_error"],
        "response_status": row["response_status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _webhook_row(row: sqlite3.Row, *, include_secret: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": row["id"],
        "webhook_id": row["id"],
        "project_id": row["project_id"],
        "name": row["name"],
        "url": row["url"],
        "event_types": json.loads(row["event_types"]),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_secret:
        result["secret"] = row["secret"]
    return result


def list_webhooks(conn: sqlite3.Connection, *, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM webhooks WHERE project_id = ? ORDER BY created_at, id", (project_id,)
    ).fetchall()
    return [_webhook_row(row) for row in rows]


def get_webhook(
    conn: sqlite3.Connection, webhook_id: str, *, project_id: str | None = None
) -> dict[str, Any] | None:
    if project_id is None:
        row = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM webhooks WHERE id = ? AND project_id = ?", (webhook_id, project_id)
        ).fetchone()
    return _webhook_row(row) if row is not None else None


def create_webhook(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    name: str,
    url: str,
    event_types: list[str],
    secret: str | None = None,
    allow_private: bool = False,
    resolver: Callable[..., Iterable[Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValueError("name must be a non-empty string of at most 200 characters")
    if not isinstance(event_types, list) or not event_types:
        raise ValueError("event_types must be a non-empty list")
    if len(event_types) > len(SUPPORTED_EVENTS) or any(
        not isinstance(item, str) or item not in SUPPORTED_EVENTS for item in event_types
    ):
        raise ValueError("event_types contains an unsupported event")
    normalized_url = validate_webhook_url(url, allow_private=allow_private, resolver=resolver)
    secret_value = secret or _new_secret()
    if not isinstance(secret_value, str) or len(secret_value) < 8:
        raise ValueError("secret must be at least 8 characters")
    webhook_id = "wh_" + uuid.uuid4().hex
    now = time.time()
    conn.execute(
        "INSERT INTO webhooks(id, project_id, name, url, event_types, secret, is_active, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (
            webhook_id,
            project_id,
            name.strip(),
            normalized_url,
            json.dumps(sorted(set(event_types)), separators=(",", ":")),
            secret_value,
            now,
            now,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
    assert row is not None
    return _webhook_row(row, include_secret=True)


def update_webhook(
    conn: sqlite3.Connection,
    *,
    webhook_id: str,
    project_id: str,
    name: str | None = None,
    url: str | None = None,
    event_types: list[str] | None = None,
    allow_private: bool = False,
    resolver: Callable[..., Iterable[Any]] | None = None,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM webhooks WHERE id = ? AND project_id = ?", (webhook_id, project_id)
    ).fetchone()
    if row is None:
        return None
    values = _webhook_row(row)
    if name is not None:
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("name must be a non-empty string of at most 200 characters")
        values["name"] = name.strip()
    if url is not None:
        values["url"] = validate_webhook_url(url, allow_private=allow_private, resolver=resolver)
    if event_types is not None:
        if (
            not isinstance(event_types, list)
            or not event_types
            or any(item not in SUPPORTED_EVENTS for item in event_types)
        ):
            raise ValueError("event_types contains an unsupported event")
        values["event_types"] = sorted(set(event_types))
    conn.execute(
        "UPDATE webhooks SET name = ?, url = ?, event_types = ?, updated_at = ? WHERE id = ? AND project_id = ?",
        (
            values["name"],
            values["url"],
            json.dumps(values["event_types"], separators=(",", ":")),
            time.time(),
            webhook_id,
            project_id,
        ),
    )
    conn.commit()
    return get_webhook(conn, webhook_id, project_id=project_id)


def delete_webhook(conn: sqlite3.Connection, *, webhook_id: str, project_id: str) -> bool:
    changed = conn.execute(
        "DELETE FROM webhooks WHERE id = ? AND project_id = ?", (webhook_id, project_id)
    ).rowcount
    conn.commit()
    return bool(changed)


def _enqueue_delivery_job(conn: sqlite3.Connection, delivery_id: str, run_after: float) -> str:
    """Insert one durable delivery job inside the caller's transaction."""
    row = conn.execute(
        "SELECT project_id FROM webhook_deliveries WHERE id = ?", (delivery_id,)
    ).fetchone()
    project_id = row["project_id"] if row is not None else None
    job_id = "whj_" + uuid.uuid4().hex
    now = time.time()
    conn.execute(
        "INSERT INTO jobs(id, kind, payload, project_id, status, attempts, result, error, worker,"
        " created_at, updated_at, run_after) VALUES (?, 'webhook_delivery', ?, ?, 'queued', 0,"
        " NULL, NULL, NULL, ?, ?, ?)",
        (
            job_id,
            json.dumps({"delivery_id": delivery_id}, separators=(",", ":")),
            project_id,
            now,
            now,
            run_after,
        ),
    )
    return job_id


def _event_payload(
    *, event_type: str, memory_id: str | None, data: dict[str, Any]
) -> dict[str, Any]:
    return {
        "event_details": {
            "id": memory_id,
            "event": event_type.removeprefix("memory_").upper(),
            "data": data,
        }
    }


def record_event(
    conn: sqlite3.Connection,
    *,
    project_id: str | None,
    event_type: str,
    memory_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> str | None:
    """Append a domain event and enqueue matching webhook deliveries atomically."""
    if event_type not in SUPPORTED_EVENTS:
        raise ValueError(f"unsupported webhook event: {event_type}")
    if project_id is None:
        return None
    payload = _event_payload(event_type=event_type, memory_id=memory_id, data=data or {})
    event_id = "evt_" + uuid.uuid4().hex
    now = time.time()
    conn.execute(
        "INSERT INTO domain_events(id, project_id, event_type, memory_id, payload, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            event_id,
            project_id,
            event_type,
            memory_id,
            json.dumps(payload, separators=(",", ":")),
            now,
        ),
    )
    rows = conn.execute(
        "SELECT id, event_types FROM webhooks WHERE project_id = ? AND is_active = 1",
        (project_id,),
    ).fetchall()
    for row in rows:
        if event_type in json.loads(row["event_types"]):
            delivery_id = "whd_" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO webhook_deliveries(id, event_id, webhook_id, project_id, event_type,"
                " memory_id, payload, status, attempts, next_attempt_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)",
                (
                    delivery_id,
                    event_id,
                    row["id"],
                    project_id,
                    event_type,
                    memory_id,
                    json.dumps(payload, separators=(",", ":")),
                    now,
                    now,
                    now,
                ),
            )
            # Keep the outbox row and its durable work item in the same transaction
            # as the state change that produced the event.
            _enqueue_delivery_job(conn, delivery_id, now)
    return event_id


def list_deliveries(
    conn: sqlite3.Connection,
    *,
    project_id: str | None = None,
    webhook_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    if webhook_id is not None:
        where.append("webhook_id = ?")
        params.append(webhook_id)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        "SELECT id, event_id, webhook_id, project_id, event_type, memory_id, status, attempts,"
        " next_attempt_at, last_error, response_status, created_at, updated_at"
        f" FROM webhook_deliveries{clause} ORDER BY created_at, id LIMIT ? OFFSET ?",
        [*params, max(1, min(limit, 500)), max(0, offset)],
    ).fetchall()
    return [_event_row_to_delivery(row) for row in rows]


def get_delivery_secret(conn: sqlite3.Connection, delivery_id: str) -> str | None:
    row = conn.execute(
        "SELECT w.secret FROM webhook_deliveries d JOIN webhooks w ON w.id = d.webhook_id"
        " WHERE d.id = ?",
        (delivery_id,),
    ).fetchone()
    return str(row["secret"]) if row is not None else None


def _delivery_row(conn: sqlite3.Connection, delivery_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT d.*, w.url, w.secret FROM webhook_deliveries d"
        " JOIN webhooks w ON w.id = d.webhook_id WHERE d.id = ?",
        (delivery_id,),
    ).fetchone()


def _mark_delivery(
    conn: sqlite3.Connection,
    delivery_id: str,
    *,
    status: str,
    attempts: int,
    error: str | None = None,
    response_status: int | None = None,
    next_attempt_at: float | None = None,
) -> None:
    conn.execute(
        "UPDATE webhook_deliveries SET status = ?, attempts = ?, last_error = ?, response_status = ?,"
        " next_attempt_at = ?, updated_at = ? WHERE id = ?",
        (status, attempts, error, response_status, next_attempt_at, time.time(), delivery_id),
    )
    conn.commit()


def _schedule_delivery_retry(conn: sqlite3.Connection, delivery_id: str, run_at: float) -> None:
    """Ensure exactly one future job exists for a retried delivery."""
    payload = json.dumps({"delivery_id": delivery_id}, separators=(",", ":"))
    row = conn.execute(
        "SELECT id FROM jobs WHERE kind = 'webhook_delivery' AND status = 'queued'"
        " AND run_after > ? AND payload = ? LIMIT 1",
        (time.time(), payload),
    ).fetchone()
    if row is None:
        _enqueue_delivery_job(conn, delivery_id, run_at)
    conn.commit()


def _requeue_or_dead(
    conn: sqlite3.Connection,
    delivery_id: str,
    *,
    attempt: int,
    max_attempts: int,
    error: str,
    response_status: int | None = None,
) -> dict[str, Any]:
    dead = attempt >= max_attempts
    next_attempt_at = None if dead else time.time() + min(3600, 2**attempt)
    _mark_delivery(
        conn,
        delivery_id,
        status="dead" if dead else "queued",
        attempts=attempt,
        error=error,
        response_status=response_status,
        next_attempt_at=next_attempt_at,
    )
    if not dead and next_attempt_at is not None:
        _schedule_delivery_retry(conn, delivery_id, next_attempt_at)
    return {
        "status": "dead" if dead else "queued",
        "delivery_id": delivery_id,
        "attempts": attempt,
        "next_attempt_at": next_attempt_at,
        "error": error,
    }


def deliver_delivery(
    conn: sqlite3.Connection,
    delivery_id: str,
    *,
    allow_private: bool = False,
    timeout_seconds: float = 5.0,
    max_response_bytes: int = 64 * 1024,
    max_attempts: int = 5,
    now: float | None = None,
) -> dict[str, Any]:
    """Deliver one queued item synchronously with bounded network I/O."""
    row = _delivery_row(conn, delivery_id)
    if row is None:
        return {"status": "not_found", "delivery_id": delivery_id}
    if row["status"] in {"succeeded", "dead"}:
        return {"status": row["status"], "delivery_id": delivery_id, "attempts": row["attempts"]}
    if row["next_attempt_at"] is not None and row["next_attempt_at"] > (now or time.time()):
        return {
            "status": "deferred",
            "delivery_id": delivery_id,
            "attempts": row["attempts"],
            "next_attempt_at": row["next_attempt_at"],
        }
    current_attempt = int(row["attempts"]) + 1
    try:
        url = validate_webhook_url(row["url"], allow_private=allow_private)
    except UnsafeWebhookTarget as exc:
        return _requeue_or_dead(
            conn,
            delivery_id,
            attempt=current_attempt,
            max_attempts=max_attempts,
            error=str(exc),
        )
    body = str(row["payload"]).encode()
    timestamp = str(int(now if now is not None else time.time()))
    signature = sign_payload(row["secret"], timestamp, body)
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Memoratum-Webhooks/1",
            "X-Memoratum-Signature": signature,
            "X-Memoratum-Timestamp": timestamp,
            "X-Memoratum-Delivery": delivery_id,
            "X-Memoratum-Event": row["event_type"],
        },
    )
    status_code: int | None = None
    response_body = b""
    try:
        # No redirect handler: a redirect is an unvalidated network hop.
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=timeout_seconds) as response:
            status_code = int(response.status)
            response_body = response.read(max_response_bytes + 1)
            if len(response_body) > max_response_bytes:
                raise ValueError("webhook response exceeded the configured size limit")
        if 200 <= status_code < 300:
            _mark_delivery(
                conn,
                delivery_id,
                status="succeeded",
                attempts=current_attempt,
                response_status=status_code,
            )
            return {"status": "succeeded", "delivery_id": delivery_id, "attempts": current_attempt}
        raise urllib.error.HTTPError(url, status_code, "webhook rejected", None, None)
    except Exception as exc:  # noqa: BLE001 — delivery failures are recorded, not raised
        return _requeue_or_dead(
            conn,
            delivery_id,
            attempt=current_attempt,
            max_attempts=max_attempts,
            error=f"{type(exc).__name__}: {exc}"[:2000],
            response_status=status_code,
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects disabled", headers, fp)


def claim_due_deliveries(
    conn: sqlite3.Connection, *, limit: int = 20, now: float | None = None
) -> list[dict[str, Any]]:
    timestamp = now or time.time()
    rows = conn.execute(
        "SELECT id FROM webhook_deliveries WHERE status = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
        " ORDER BY created_at, id LIMIT ?",
        (timestamp, max(1, min(limit, 100))),
    ).fetchall()
    return [dict(row) for row in rows]


def replay_delivery(conn: sqlite3.Connection, delivery_id: str) -> dict[str, Any] | None:
    """Reset a delivery and ensure exactly one durable delivery job exists."""
    now = time.time()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM webhook_deliveries WHERE id = ? AND status IN ('dead', 'queued')",
            (delivery_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        payload = json.dumps({"delivery_id": delivery_id}, separators=(",", ":"))
        queued = conn.execute(
            "SELECT id FROM jobs WHERE kind = 'webhook_delivery' AND status = 'queued'"
            " AND payload = ? ORDER BY created_at, id LIMIT 1",
            (payload,),
        ).fetchone()
        job_id = str(queued["id"]) if queued is not None else None
        if job_id is None:
            running = conn.execute(
                "SELECT 1 FROM jobs WHERE kind = 'webhook_delivery' AND status = 'running'"
                " AND payload = ? LIMIT 1",
                (payload,),
            ).fetchone()
            if running is not None:
                conn.rollback()
                return None
        conn.execute(
            "UPDATE webhook_deliveries SET status = 'queued', attempts = 0, last_error = NULL,"
            " response_status = NULL, next_attempt_at = NULL, updated_at = ? WHERE id = ?",
            (now, delivery_id),
        )
        if job_id is None:
            job_id = _enqueue_delivery_job(conn, delivery_id, now)
        else:
            conn.execute(
                "UPDATE jobs SET status = 'queued', worker = NULL, attempts = 0, result = NULL,"
                " error = NULL, run_after = ?, updated_at = ? WHERE id = ?",
                (now, now, job_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "id": delivery_id,
        "status": "queued",
        "replayed": True,
        "job_id": job_id,
    }


def rotate_secret(conn: sqlite3.Connection, *, webhook_id: str, project_id: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM webhooks WHERE id = ? AND project_id = ?", (webhook_id, project_id)
    ).fetchone()
    if row is None:
        return None
    secret = _new_secret()
    conn.execute(
        "UPDATE webhooks SET secret = ?, updated_at = ? WHERE id = ? AND project_id = ?",
        (secret, time.time(), webhook_id, project_id),
    )
    conn.commit()
    return secret


def delivery_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Public alias used by contract tests and local receivers."""
    return sign_payload(secret, timestamp, body)
