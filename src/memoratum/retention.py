# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Retention policy registry and timed sweep.

Every table carries an explicit policy, stored as data rather than prose so that
"every table has one" is a test assertion instead of something a reader has to
notice. A future migration that adds a table without a policy fails
``tests/test_retention.py::test_every_table_has_a_policy``.

Ordering matters: ``webhook_deliveries`` references ``domain_events``, so
deliveries are swept first. Only *terminal* rows are ever eligible -- a queued
delivery is an outstanding obligation, not a log line.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

# table -> (policy_class, retention_days, rationale)
POLICIES: dict[str, tuple[str, int | None, str]] = {
    "organizations": ("retain_indefinitely", None, "Top-level tenant; operator removes it."),
    "projects": ("retain_indefinitely", None, "Tenant; the deleting flag gates purge."),
    "project_members": ("cascaded", None, "Cascades from projects."),
    "api_keys": ("retain_indefinitely", None, "Must outlive the key's own usefulness."),
    "revoked_keys": (
        "timed",
        90,
        "Kept well past any realistic credential-reuse window, then aged out.",
    ),
    "webhooks": ("retain_indefinitely", None, "Operator-configured, low volume."),
    "documents": ("retain_indefinitely", None, "User content; expires_at governs visibility."),
    "chunks": ("cascaded", None, "Cascades from documents; rebuildable."),
    "memories": ("retain_indefinitely", None, "User content; expires_at governs visibility."),
    "memory_history": (
        "timed",
        365,
        "One year of audit value, then unbounded growth becomes a liability.",
    ),
    "facts": ("retain_indefinitely", None, "User content."),
    "vector_points": ("cascaded", None, "Derived; rebuilt from memories/chunks."),
    "jobs": ("timed", 30, "Terminal jobs only. Queued and running are live work."),
    "domain_events": (
        "timed",
        90,
        "Terminal once every delivery for the event has resolved.",
    ),
    "webhook_deliveries": (
        "timed",
        90,
        "Only succeeded/dead. A queued delivery is an obligation, not a log line.",
    ),
    "idempotency_keys": ("timed", 24, "Bounded by expires_at; the sweep enforces it."),
    "share_links": ("timed", 30, "Bounded by expires_at."),
    "audit_events": ("timed", 365, "Accountability window."),
    "usage_counters": ("retain_indefinitely", None, "Accounting record, not a log."),
    "retention_policies": (
        "retain_indefinitely",
        None,
        "Configuration, not tenant data; the same rule it describes applies to it.",
    ),
}

# Timed tables in sweep order. Children before parents.
SWEEP_ORDER: tuple[str, ...] = (
    "webhook_deliveries",
    "domain_events",
    "memory_history",
    "jobs",
    "share_links",
    "idempotency_keys",
    "audit_events",
    "revoked_keys",
)

# Only terminal statuses are eligible for deletion.
TERMINAL = {
    "webhook_deliveries": ("succeeded", "dead"),
    "domain_events": (),
    "memory_history": (),
    "jobs": ("done", "failed", "cancelled"),
    "share_links": (),
    "idempotency_keys": (),
    "audit_events": (),
    "revoked_keys": (),
}


def seed_policies(conn: sqlite3.Connection) -> None:
    """Write the policy registry. Safe to call repeatedly."""
    for table, (policy_class, days, rationale) in POLICIES.items():
        conn.execute(
            "INSERT INTO retention_policies(table_name, policy_class, retention_days, rationale)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(table_name) DO UPDATE SET"
            " policy_class=excluded.policy_class,"
            " retention_days=excluded.retention_days,"
            " rationale=excluded.rationale",
            (table, policy_class, days, rationale),
        )


def _cutoff_days(conn: sqlite3.Connection, table: str, override: int | None) -> int | None:
    if override is not None:
        return override
    row = conn.execute(
        "SELECT retention_days FROM retention_policies WHERE table_name = ?", (table,)
    ).fetchone()
    if row is None:
        return None
    return row["retention_days"]


def sweep(
    conn: sqlite3.Connection, *, dry_run: bool = False, days: int | None = None
) -> dict[str, Any]:
    """Delete rows past their retention window.

    ``days`` overrides every timed policy, which is what
    ``MEMORATUM_RETENTION_DAYS`` does. ``dry_run`` reports without deleting.
    """
    now = time.time()
    deleted: dict[str, int] = {}

    for table in SWEEP_ORDER:
        window = _cutoff_days(conn, table, days)
        if window is None:
            continue
        cutoff = now - window * 86400

        if table == "domain_events":
            # An event is only collectable once nothing references it.
            sql = (
                "DELETE FROM domain_events WHERE created_at < ?"
                " AND NOT EXISTS (SELECT 1 FROM webhook_deliveries d"
                "                 WHERE d.event_id = domain_events.id)"
            )
        elif table == "idempotency_keys":
            sql = "DELETE FROM idempotency_keys WHERE expires_at < ?"
        elif table == "share_links":
            sql = (
                "DELETE FROM share_links WHERE expires_at < ?"
                " OR (revoked_at IS NOT NULL AND revoked_at < ?)"
            )
        elif table == "revoked_keys":
            # Keyed by revoked_at; there is no created_at on this table.
            sql = "DELETE FROM revoked_keys WHERE revoked_at < ?"
        elif TERMINAL.get(table):
            placeholders = ",".join("?" * len(TERMINAL[table]))
            sql = f"DELETE FROM {table} WHERE created_at < ? AND status IN ({placeholders})"
        else:
            sql = f"DELETE FROM {table} WHERE created_at < ?"

        params: tuple[Any, ...]
        if sql.count("?") == 2:
            params = (cutoff, cutoff)
        elif TERMINAL.get(table):
            params = (cutoff, *TERMINAL[table])
        else:
            params = (cutoff,)

        if dry_run:
            probe = sql.replace("DELETE FROM", "SELECT COUNT(*) FROM", 1)
            deleted[table] = int(conn.execute(probe, params).fetchone()[0])
            continue

        cursor = conn.execute(sql, params)
        deleted[table] = max(0, cursor.rowcount)

    if not dry_run:
        conn.commit()
        # Reclaim space rather than only bounding rows.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return (
        {"dry_run": dry_run, "would_delete": deleted}
        if dry_run
        else {
            "dry_run": False,
            "deleted": deleted,
        }
    )


def checkpoint_and_vacuum(conn: sqlite3.Connection, *, incremental_vacuum: bool = True) -> None:
    """Return free pages to the filesystem."""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if incremental_vacuum:
        conn.execute("PRAGMA incremental_vacuum")
    conn.commit()


def report(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The policy registry, for docs and operators."""
    rows = conn.execute(
        "SELECT table_name, policy_class, retention_days, rationale FROM retention_policies"
        " ORDER BY policy_class, table_name"
    ).fetchall()
    return [dict(r) for r in rows]
