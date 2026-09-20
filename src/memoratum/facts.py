# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Temporal fact graph. Contradictions supersede (close validity), never delete."""

from __future__ import annotations

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
) -> dict[str, Any]:
    now = time.time()
    current = conn.execute(
        "SELECT id, object FROM facts WHERE container_tag = ? AND subject = ? AND predicate = ? AND valid_to IS NULL",
        (container_tag, subject, predicate),
    ).fetchall()
    for row in current:
        if row["object"] == object:
            return dict(row)
    fact_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO facts(id, container_tag, subject, predicate, object, document_id, valid_from, valid_to,"
        " superseded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
        (fact_id, container_tag, subject, predicate, object, document_id, now, now),
    )
    for row in current:
        conn.execute(
            "UPDATE facts SET valid_to = ?, superseded_by = ? WHERE id = ?",
            (now, fact_id, row["id"]),
        )
    conn.commit()
    return get_fact(conn, fact_id)


def get_fact(conn: sqlite3.Connection, fact_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if row is None:
        raise KeyError(fact_id)
    return dict(row)


def list_facts(
    conn: sqlite3.Connection, container_tag: str, *, include_superseded: bool = False
) -> list[dict[str, Any]]:
    if include_superseded:
        rows = conn.execute(
            "SELECT * FROM facts WHERE container_tag = ? ORDER BY created_at", (container_tag,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM facts WHERE container_tag = ? AND valid_to IS NULL ORDER BY created_at",
            (container_tag,),
        ).fetchall()
    return [dict(r) for r in rows]
