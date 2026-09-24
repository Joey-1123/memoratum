# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Temporal fact graph. Contradictions supersede (close validity), never delete."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any


def add_fact(
    conn: sqlite3.Connection,
    *,
    container_tag: str,
    subject: str,
    predicate: str,
    object: str,
    document_id: str | None,
    metadata: dict[str, Any] | None = None,
    supersede: bool = True,
    expires_at: float | None = None,
    memory_type: str = "semantic",
    org_id: str | None = None,
) -> dict[str, Any]:
    """Add a fact. Same (s,p,o) re-asserts (reviving a superseded row).
    Same (s,p) with a different object supersedes live rows only when
    supersede=True (functional relations); multi-valued relations
    (calls/contains/imports) pass supersede=False and coexist."""
    now = time.time()
    org_clause = "org_id IS NULL" if org_id is None else "org_id = ?"
    org_params: tuple[Any, ...] = () if org_id is None else (org_id,)
    same = conn.execute(
        "SELECT id, valid_to FROM facts WHERE container_tag = ? AND subject = ? AND predicate = ? AND object = ?"
        f" AND {org_clause} ORDER BY created_at DESC LIMIT 1",
        (container_tag, subject, predicate, object, *org_params),
    ).fetchone()
    if same is not None:
        if same["valid_to"] is not None:
            conn.execute(
                "UPDATE facts SET valid_to = NULL, superseded_by = NULL WHERE id = ?", (same["id"],)
            )
            conn.commit()
        return get_fact(conn, same["id"])
    current = conn.execute(
        "SELECT id, object FROM facts WHERE container_tag = ? AND subject = ? AND predicate = ? AND valid_to IS NULL"
        f" AND {org_clause}",
        (container_tag, subject, predicate, *org_params),
    ).fetchall()
    fact_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO facts(id, container_tag, subject, predicate, object, document_id, valid_from, valid_to,"
        " superseded_by, created_at, metadata, expires_at, memory_type, org_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)",
        (
            fact_id,
            container_tag,
            subject,
            predicate,
            object,
            document_id,
            now,
            now,
            json.dumps(metadata or {}),
            expires_at,
            memory_type,
            org_id,
        ),
    )
    for row in current:
        if supersede:
            conn.execute(
                "UPDATE facts SET valid_to = ?, superseded_by = ? WHERE id = ?",
                (now, fact_id, row["id"]),
            )
    conn.commit()
    return get_fact(conn, fact_id)


def delete_fact(conn: sqlite3.Connection, fact_id: str) -> bool:
    """Hard-delete one fact. Returns True if it existed."""
    cur = conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
    conn.commit()
    return cur.rowcount > 0


def get_fact(conn: sqlite3.Connection, fact_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if row is None:
        raise KeyError(fact_id)
    fact = dict(row)
    fact["metadata"] = json.loads(fact.get("metadata") or "{}")
    return fact


def _matches(meta: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    if not filters:
        return True
    return all(meta.get(k) == v for k, v in filters.items())


def list_facts(
    conn: sqlite3.Connection,
    container_tag: str,
    *,
    include_superseded: bool = False,
    filters: dict[str, Any] | None = None,
    memory_type: str | None = None,
    org_id: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    now = time.time()
    params: list[Any] = [container_tag]
    where = "container_tag = ?"
    if not include_superseded:
        where += " AND valid_to IS NULL AND (expires_at IS NULL OR expires_at > ?)"
        params.append(now)
    if memory_type is not None:
        where += " AND memory_type = ?"
        params.append(memory_type)
    if org_id is not None:
        where += " AND org_id = ?"
        params.append(org_id)
    query = f"SELECT * FROM facts WHERE {where} ORDER BY created_at"
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params += (max(0, limit), max(0, offset))
    rows = conn.execute(query, params).fetchall()
    out = []
    for r in rows:
        fact = dict(r)
        fact["metadata"] = json.loads(fact.get("metadata") or "{}")
        if _matches(fact["metadata"], filters):
            out.append(fact)
    return out


def count_facts(
    conn: sqlite3.Connection,
    container_tag: str,
    *,
    include_superseded: bool = False,
    memory_type: str | None = None,
    org_id: str | None = None,
) -> int:
    """Count facts using the same visibility scope as list_facts."""
    now = time.time()
    params: list[Any] = [container_tag]
    where = "container_tag = ?"
    if not include_superseded:
        where += " AND valid_to IS NULL AND (expires_at IS NULL OR expires_at > ?)"
        params.append(now)
    if memory_type is not None:
        where += " AND memory_type = ?"
        params.append(memory_type)
    if org_id is not None:
        where += " AND org_id = ?"
        params.append(org_id)
    return int(
        conn.execute(f"SELECT COUNT(*) AS n FROM facts WHERE {where}", params).fetchone()["n"]
    )
