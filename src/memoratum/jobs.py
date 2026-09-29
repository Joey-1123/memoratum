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


def enqueue(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: dict[str, Any],
    run_after: float | None = None,
    commit: bool = True,
) -> str:
    job_id = uuid.uuid4().hex
    now = time.time()
    project_id = payload.get("project_id")
    conn.execute(
        "INSERT INTO jobs(id, kind, payload, project_id, status, attempts, result, error, worker,"
        " created_at, updated_at, run_after)"
        " VALUES (?, ?, ?, ?, 'queued', 0, NULL, NULL, NULL, ?, ?, ?)",
        (
            job_id,
            kind,
            json.dumps(payload),
            project_id if isinstance(project_id, str) else None,
            now,
            now,
            run_after or 0.0,
        ),
    )
    if commit:
        conn.commit()
    return job_id


def claim(conn: sqlite3.Connection, *, worker: str) -> dict[str, Any] | None:
    """Atomically move one queued job to running. Returns None when empty."""
    now = time.time()
    row = conn.execute(
        "SELECT id FROM jobs WHERE status = 'queued' AND run_after <= ? ORDER BY created_at LIMIT 1",
        (now,),
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


def update_result(conn: sqlite3.Connection, job_id: str, result: dict[str, Any]) -> None:
    """Persist intermediate progress for a long-running job."""
    conn.execute(
        "UPDATE jobs SET result = ?, updated_at = ? WHERE id = ?",
        (json.dumps(result, separators=(",", ":")), time.time(), job_id),
    )
    conn.commit()


def fail(conn: sqlite3.Connection, job_id: str, *, error: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
        (error[:2000], time.time(), job_id),
    )
    conn.commit()


def requeue(conn: sqlite3.Connection, job_id: str, *, run_after: float | None = None) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'queued', worker = NULL, run_after = ?, updated_at = ? WHERE id = ?",
        (run_after or 0.0, time.time(), job_id),
    )
    conn.commit()


def cancel(conn: sqlite3.Connection, job_id: str) -> bool:
    """Cancel a queued job; running work is never interrupted."""
    changed = conn.execute(
        "UPDATE jobs SET status = 'cancelled', worker = NULL, updated_at = ?"
        " WHERE id = ? AND status = 'queued'",
        (time.time(), job_id),
    ).rowcount
    conn.commit()
    return changed == 1


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
