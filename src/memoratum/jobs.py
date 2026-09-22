# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable job queue over SQLite. Atomic claim lets N workers share one DB."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any


def enqueue(conn: sqlite3.Connection, *, kind: str, payload: dict[str, Any]) -> str:
    job_id = uuid.uuid4().hex
    now = time.time()
    conn.execute(
        "INSERT INTO jobs(id, kind, payload, status, attempts, result, error, worker, created_at, updated_at)"
        " VALUES (?, ?, ?, 'queued', 0, NULL, NULL, NULL, ?, ?)",
        (job_id, kind, json.dumps(payload), now, now),
    )
    conn.commit()
    return job_id


def claim(conn: sqlite3.Connection, *, worker: str) -> dict[str, Any] | None:
    """Atomically move one queued job to running. Returns None when empty."""
    now = time.time()
    row = conn.execute(
        "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    changed = conn.execute(
        "UPDATE jobs SET status = 'running', worker = ?, attempts = attempts + 1, updated_at = ?"
        " WHERE id = ? AND status = 'queued'",
        (worker, now, row["id"]),
    ).rowcount
    conn.commit()
    if not changed:
        return None
    return get(conn, row["id"])


def complete(
    conn: sqlite3.Connection, job_id: str, *, result: dict[str, Any] | None = None
) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'done', result = ?, updated_at = ? WHERE id = ?",
        (json.dumps(result or {}), time.time(), job_id),
    )
    conn.commit()


def fail(conn: sqlite3.Connection, job_id: str, *, error: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
        (error[:2000], time.time(), job_id),
    )
    conn.commit()


def get(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["payload"] = json.loads(job["payload"] or "{}")
    job["result"] = json.loads(job["result"] or "{}")
    return job


def pending(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        job = dict(r)
        job["payload"] = json.loads(job["payload"] or "{}")
        out.append(job)
    return out
