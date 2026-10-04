# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local, opt-in webhook subscriptions and signed delivery."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import secrets
import socket
import sqlite3
import ssl
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

_SECRET_PREFIX = "enc:v1:"
_KEY_FILE_NAME = ".webhook-encryption-key"
_DATA_DIR_OVERRIDE: str | None = None


def set_encryption_data_dir(path: str) -> None:
    global _DATA_DIR_OVERRIDE
    _DATA_DIR_OVERRIDE = path


def _encryption_keys() -> list[str]:
    configured = os.environ.get("MEMORATUM_WEBHOOK_ENCRYPTION_KEY", "").strip()
    if not configured:
        data_dir = (
            os.environ.get("MEMORATUM_DATA_DIR")
            or _DATA_DIR_OVERRIDE
            or os.path.join(os.getcwd(), ".memoratum-data")
        )
        path = os.path.join(data_dir, _KEY_FILE_NAME)
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(data_dir, 0o700)
        except OSError:
            pass
        if not os.path.exists(path):
            from cryptography.fernet import Fernet

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(path, flags, 0o600)
            try:
                os.write(fd, Fernet.generate_key() + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        with open(path, encoding="utf-8") as handle:
            configured = handle.read().strip()
    keys = [item.strip() for item in configured.split(",") if item.strip()]
    if not keys:
        raise ValueError("MEMORATUM_WEBHOOK_ENCRYPTION_KEY must contain a Fernet key")
    return keys


def _secret_cipher():
    from cryptography.fernet import Fernet, MultiFernet

    return MultiFernet([Fernet(key) for key in _encryption_keys()])


def encrypt_secret(secret: str) -> str:
    """Encrypt a signing secret before it is written to SQLite."""
    return _SECRET_PREFIX + _secret_cipher().encrypt(secret.encode()).decode()


def decrypt_secret(value: str) -> str:
    """Decrypt a stored secret, accepting pre-encryption databases for migration."""
    if not value.startswith(_SECRET_PREFIX):
        return value
    return _secret_cipher().decrypt(value[len(_SECRET_PREFIX) :].encode()).decode()


def migrate_plaintext_secrets(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT id, secret FROM webhooks WHERE secret NOT LIKE ?", (f"{_SECRET_PREFIX}%",)
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE webhooks SET secret = ? WHERE id = ?",
            (encrypt_secret(str(row["secret"])), row["id"]),
        )
    if rows:
        conn.commit()
    return len(rows)


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


# IANA special-use ranges that Python's ipaddress does NOT classify as non-global.
# `is_global` alone is not a sufficient allowlist: it is True for all of these.
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "100.64.0.0/10",  # CGNAT -- Tailscale, cloud and container internals
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "192.88.99.0/24",  # 6to4 relay anycast (deprecated, RFC 7526)
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "2001::/23",  # IETF protocol assignments (covers ORCHID 2001:10::/28)
        "2001:20::/28",  # ORCHIDv2
        "2002::/16",  # 6to4
        "2620:4f:8000::/48",  # AS112 anycast DNS
        "3fff::/20",  # documentation
        "5f00::/16",  # segment routing (SRv6)
        "64:ff9b::/96",  # NAT64 -- unwrapping is done too, but never trust it here
    )
)

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")


def _unwrap_v4_in_v6(address: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """Reduce IPv4-in-IPv6 encodings to the IPv4 address they actually name.

    An attacker controls the AAAA record, so an encoding must never be a way to
    smuggle 127.0.0.1 past the policy. The stdlib already unwraps ``ipv4_mapped``
    for is_private/is_loopback, but that is implementation behaviour rather than a
    documented guarantee, and NAT64/6to4 need handling regardless.
    """
    for _ in range(4):  # bounded: each iteration strictly reduces the address
        if address.version != 6:
            break
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address in _NAT64:
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        elif address in _SIX_TO_FOUR:
            address = ipaddress.IPv4Address((int(address) >> 80) & 0xFFFFFFFF)
        else:
            break
    return address


def _is_public_ip(value: str) -> bool:
    """True only for globally routable unicast addresses.

    This is a positive allowlist (``is_global``) plus an explicit denylist of
    special-use ranges the stdlib still reports as global. The previous version was
    a disjunction of negatives, which let CGNAT through because ``is_private`` is
    documented False for 100.64.0.0/10.
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    address = _unwrap_v4_in_v6(address)
    if not address.is_global or address.is_multicast:
        return False
    return not any(address in network for network in _BLOCKED_NETWORKS)


def _addresses_from_entries(entries: Iterable[Any]) -> list[str]:
    """Pull host addresses out of getaddrinfo-style results."""
    resolved: list[str] = []
    for item in entries:
        if isinstance(item, tuple) and len(item) >= 5:
            sockaddr = item[4]
            if isinstance(sockaddr, tuple) and sockaddr:
                resolved.append(str(sockaddr[0]))
        elif isinstance(item, tuple) and len(item) >= 2 and isinstance(item[0], str):
            resolved.append(item[0])
        elif isinstance(item, str):
            resolved.append(item)
    return resolved


def _validated_entries(
    hostname: str,
    port: int,
    *,
    allow_private: bool,
    resolver: Callable[..., Iterable[Any]] | None = None,
) -> list[tuple[int, int, int, tuple]]:
    """Resolve once and return only the sockaddrs that passed the policy.

    The whole sockaddr is carried, not just the address string: IPv6 entries are
    4-tuples whose scope_id selects the interface, and discarding it (as the
    previous validate-then-connect code did) both loses that information and
    invites a second, unpinned resolution at connect time.
    """
    lookup = resolver or socket.getaddrinfo
    try:
        entries = lookup(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeWebhookTarget("webhook host could not be resolved") from exc

    allowed: list[tuple[int, int, int, tuple]] = []
    for item in entries:
        entry = _normalize_entry(item, port)
        if entry is None:
            continue
        family, socktype, proto, sockaddr = entry
        if not allow_private and not _is_public_ip(str(sockaddr[0])):
            raise UnsafeWebhookTarget("webhook host resolves to a private or reserved address")
        allowed.append((family, socktype, proto, sockaddr))
    if not allowed:
        raise UnsafeWebhookTarget("webhook host has no usable address")
    return allowed


def _normalize_entry(item: Any, default_port: int) -> tuple[int, int, int, tuple] | None:
    """Coerce one resolver result into a dialable (family, type, proto, sockaddr).

    Accepts the full getaddrinfo 5-tuple, a bare (address, port) pair, or a plain
    address string, because tests and injected resolvers legitimately use the
    simpler shapes. IPv6 keeps whatever scope_id the resolver supplied, since that
    is what selects the interface for link-local targets.
    """
    if isinstance(item, str):
        address: Any = item
        port_value: Any = default_port
    elif isinstance(item, tuple) and len(item) >= 5 and isinstance(item[4], tuple) and item[4]:
        family, socktype, proto = item[0], item[1], item[2]
        if socktype is not socket.SOCK_STREAM:
            return None
        return (family, socktype, proto, item[4])
    elif isinstance(item, tuple) and len(item) >= 2 and isinstance(item[0], str):
        address, port_value = item[0], item[1]
    else:
        return None

    if not isinstance(address, str):
        return None
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    if isinstance(port_value, str):
        try:
            port_value = int(port_value)
        except ValueError:
            return None
    if parsed.version == 6:
        return (
            socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            (address, port_value, 0, 0),
        )
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (address, port_value))


def _connect_pinned(
    entries: list[tuple[int, int, int, tuple]], timeout: float, source_address
) -> socket.socket:
    """socket.create_connection replacement that only dials pre-validated addresses.

    Retries across the validated set on transport errors only. A TLS failure is not
    retried: it is either an attack signal or a broken endpoint, and retrying costs
    N x handshake time against an adversary while burying the diagnostic.
    """
    last: OSError | None = None
    for family, socktype, proto, sockaddr in entries:
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)  # no DNS here: the address is already fixed
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise last or OSError("no validated address was reachable")


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that dials a pre-validated IP but keeps Host/SNI/TLS intact.

    The hostname stays in the URL, so ``self.host`` remains the name, SNI is sent
    for that name, and the certificate is validated against the name rather than
    the IP. Only the socket address changes.

    ``_create_connection`` is overridden as an INSTANCE attribute on purpose:
    ``HTTPConnection.__init__`` assigns ``self._create_connection =
    socket.create_connection``, so a same-named method on a subclass is silently
    shadowed and DNS still happens.
    """

    def __init__(self, host: str, *, pinned: list[tuple[int, int, int, tuple]] | None = None, **kw):
        self._pinned = list(pinned or [])
        super().__init__(host, **kw)
        self._create_connection = (  # type: ignore[method-assign]
            lambda address, timeout, source_address: _connect_pinned(
                self._pinned, timeout, source_address
            )
        )


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """Route urllib's HTTPS through :class:`PinnedHTTPSConnection`."""

    def __init__(self, pinned: list[tuple[int, int, int, tuple]] | None = None, **kw):
        self._pinned = list(pinned or [])
        super().__init__(**kw)

    def https_open(self, req):
        # context must be passed explicitly: without it urllib falls back to
        # http.client._create_https_context() and silently discards ours.
        return self.do_open(
            lambda host, **kw: PinnedHTTPSConnection(host, pinned=self._pinned, **kw),
            req,
            context=self._context,
        )


def _normalize_webhook_url(url: str, *, allow_private: bool = False) -> tuple[str, str, int]:
    """Syntax and policy checks that need no DNS. Returns (url, hostname, port)."""
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
        raise UnsafeWebhookTarget("webhook url must be https")
    if parsed.username or parsed.password or parsed.fragment:
        raise UnsafeWebhookTarget("webhook url must not contain credentials or a fragment")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeWebhookTarget("webhook url must include a host")
    if hostname.lower() in _RESERVED_HOSTS and not allow_private:
        raise UnsafeWebhookTarget("webhook host is not allowed")
    # Judge a literal IP in the URL directly rather than trusting DNS to echo it
    # back. Real DNS does echo it, so this is defence in depth -- but a policy that
    # asks the resolver to police the URL it was handed is one resolver mistake
    # away from being wrong.
    literal = hostname[1:-1] if hostname.startswith("[") and hostname.endswith("]") else hostname
    try:
        ipaddress.ip_address(literal)
        is_literal = True
    except ValueError:
        is_literal = False
    if is_literal and not allow_private and not _is_public_ip(literal):
        raise UnsafeWebhookTarget("webhook url must not target a literal internal address")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeWebhookTarget("webhook url has an invalid port") from exc
    normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    return normalized, hostname, port


def resolve_webhook_target(
    url: str,
    *,
    allow_private: bool = False,
    resolver: Callable[..., Iterable[Any]] | None = None,
) -> tuple[str, str, int, list[tuple[int, int, int, tuple]]]:
    """Validate and resolve a webhook endpoint in a single DNS lookup.

    Returns ``(normalized_url, hostname, port, entries)``, where ``entries`` are the
    sockaddrs that passed the address policy and must be used verbatim when
    connecting.

    Resolution happens exactly once, here. Anything that resolves again later --
    including urllib's own connect path -- reopens the DNS-rebinding window this
    function exists to close.
    """
    normalized, hostname, port = _normalize_webhook_url(url, allow_private=allow_private)
    entries = _validated_entries(hostname, port, allow_private=allow_private, resolver=resolver)
    return normalized, hostname, port, entries


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

    For delivery, prefer :func:`resolve_webhook_target`, which additionally returns
    the validated addresses so the connection can be pinned to them.
    """
    normalized, _hostname, _port, _entries = resolve_webhook_target(
        url, allow_private=allow_private, resolver=resolver
    )
    return normalized


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
        result["secret"] = decrypt_secret(str(row["secret"]))
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
            encrypt_secret(secret_value),
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
    is_active: bool | None = None,
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
    if is_active is not None:
        if not isinstance(is_active, bool):
            raise ValueError("is_active must be a boolean")
        values["is_active"] = is_active
    conn.execute(
        "UPDATE webhooks SET name = ?, url = ?, event_types = ?, is_active = ?, updated_at = ?"
        " WHERE id = ? AND project_id = ?",
        (
            values["name"],
            values["url"],
            json.dumps(values["event_types"], separators=(",", ":")),
            int(values["is_active"]),
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
    # Project-scoped rows can be removed by a concurrent purge or by an
    # operator cleaning a legacy database. Retention/maintenance work must not
    # fail merely because its event target no longer exists.
    if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
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
    return decrypt_secret(str(row["secret"])) if row is not None else None


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
        "SELECT id, run_after FROM jobs WHERE kind = 'webhook_delivery' AND status = 'queued'"
        " AND payload = ? ORDER BY created_at, id LIMIT 1",
        (payload,),
    ).fetchone()
    if row is None:
        _enqueue_delivery_job(conn, delivery_id, run_at)
    else:
        current_run_after = float(row["run_after"] or 0)
        conn.execute(
            "UPDATE jobs SET run_after = ?, updated_at = ? WHERE id = ?",
            (
                min(current_run_after, run_at) if current_run_after > 0 else run_at,
                time.time(),
                row["id"],
            ),
        )
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
    # Resolve and validate ONCE here. The previous code validated and then let
    # urllib resolve the hostname again to connect, which is a textbook DNS
    # rebinding window: a name that answers public during validation and
    # link-local at connect time reached the internal network.
    try:
        url, _hostname, _port, pinned = resolve_webhook_target(
            row["url"], allow_private=allow_private
        )
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
    signature = sign_payload(decrypt_secret(str(row["secret"])), timestamp, body)
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
        # PinnedHTTPSHandler dials the addresses validated above, so no second
        # DNS lookup happens on this path.
        opener = urllib.request.build_opener(
            PinnedHTTPSHandler(pinned, context=ssl.create_default_context()), _NoRedirect
        )
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
    nested = conn.in_transaction
    savepoint = "memoratum_replay_delivery"
    if nested:
        conn.execute(f"SAVEPOINT {savepoint}")
    else:
        conn.execute("BEGIN IMMEDIATE")

    def finish() -> None:
        if nested:
            conn.execute(f"RELEASE {savepoint}")
        else:
            conn.commit()

    def abort() -> None:
        if nested:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        else:
            conn.rollback()

    try:
        row = conn.execute(
            "SELECT id FROM webhook_deliveries WHERE id = ? AND status IN ('dead', 'queued')",
            (delivery_id,),
        ).fetchone()
        if row is None:
            abort()
            return None
        payload = json.dumps({"delivery_id": delivery_id}, separators=(",", ":"))
        running = conn.execute(
            "SELECT 1 FROM jobs WHERE kind = 'webhook_delivery' AND status = 'running'"
            " AND payload = ? LIMIT 1",
            (payload,),
        ).fetchone()
        if running is not None:
            abort()
            return None
        queued_rows = conn.execute(
            "SELECT id FROM jobs WHERE kind = 'webhook_delivery' AND status = 'queued'"
            " AND payload = ? ORDER BY created_at, id",
            (payload,),
        ).fetchall()
        job_id = str(queued_rows[0]["id"]) if queued_rows else None
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
            for duplicate in queued_rows[1:]:
                conn.execute("DELETE FROM jobs WHERE id = ?", (duplicate["id"],))
        finish()
    except Exception:
        abort()
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
        (encrypt_secret(secret), time.time(), webhook_id, project_id),
    )
    conn.commit()
    return secret


def delivery_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Public alias used by contract tests and local receivers."""
    return sign_payload(secret, timestamp, body)
