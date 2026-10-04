# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable job queue over SQLite. Atomic claim lets N workers share one DB.

Every claim carries a lease. Without one, an uncatchable worker kill leaves a job
``running`` forever: a fresh ``claim()`` skips it, ``cancel()`` refuses it, and for
``purge_project`` that wedges a project permanently — writes return 409 and the
delete retry hands back the same dead job id. The lease is what makes that state
recoverable.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any

# Lease length. Must exceed the slowest single unit of work between heartbeats,
# not the whole batch: long jobs heartbeat as they report progress.
DEFAULT_LEASE_SECONDS = 300


def lease_seconds() -> int:
    """Configured lease length, clamped to a sane range."""
    raw = os.environ.get("MEMORATUM_JOB_LEASE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_LEASE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LEASE_SECONDS
    return max(30, min(86_400, value))


def _clear_lease(conn: sqlite3.Connection, job_id: str, now: float) -> None:
    conn.execute(
        "UPDATE jobs SET lease_expires_at = 0, heartbeat_at = NULL, updated_at = ? WHERE id = ?",
        (now, job_id),
    )


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
        " created_at, updated_at, run_after, lease_expires_at, heartbeat_at)"
        " VALUES (?, ?, ?, ?, 'queued', 0, NULL, NULL, NULL, ?, ?, ?, 0, NULL)",
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
    """Atomically move one due queued job to running under a fresh lease.

    The SELECT and the UPDATE run inside ``BEGIN IMMEDIATE``. Without it this is a
    read-then-write race: two workers can select the same row and both believe they
    won. Returns None when the queue is empty.
    """
    now = time.time()
    deadline = now + lease_seconds()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        # Already inside a transaction owned by a caller; join it rather than fail.
        pass
    try:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status = 'queued' AND run_after <= ?"
            " ORDER BY created_at LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        changed = conn.execute(
            "UPDATE jobs SET status = 'running', worker = ?, attempts = attempts + 1,"
            " lease_expires_at = ?, heartbeat_at = ?, updated_at = ?"
            " WHERE id = ? AND status = 'queued'",
            (worker, deadline, now, now, row["id"]),
        ).rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if not changed:
        return None
    return get(conn, row["id"])


def heartbeat(conn: sqlite3.Connection, job_id: str, *, ttl_seconds: int | None = None) -> bool:
    """Extend a running job's lease forward. Never shortens it.

    Returns False when the job is not running or its lease already lapsed, which is
    how a worker discovers it has lost the claim.
    """
    now = time.time()
    ttl = lease_seconds() if ttl_seconds is None else ttl_seconds
    # MAX() makes this monotonic: a heartbeat can only ever push the deadline out.
    # A lease that already lapsed is left alone so the reaper still sees it.
    changed = conn.execute(
        "UPDATE jobs SET lease_expires_at = MAX(lease_expires_at, ?), heartbeat_at = ?,"
        " updated_at = ?"
        " WHERE id = ? AND status = 'running' AND lease_expires_at >= ?",
        (now + ttl, now, now, job_id, now),
    ).rowcount
    conn.commit()
    return changed == 1


def reap_stale(conn: sqlite3.Connection, *, ttl_seconds: int | None = None) -> list[dict[str, Any]]:
    """Return jobs whose worker died to the queue. Their attempts are NOT reset.

    The lease deadline on each row is authoritative: a job is reaped once its own
    deadline has passed. ``ttl_seconds`` is deliberately not re-applied, because that
    would park a dead job for another full TTL after it had already expired. Rows
    written before leases existed carry ``lease_expires_at = 0`` and are therefore
    treated as dead, which is the correct reading of a claim nobody can prove is
    still alive.

    Attempts are not reset: doing so would let a permanently failing job loop
    forever instead of reaching MAX_ATTEMPTS and dying.
    """
    del ttl_seconds  # each row's own deadline is what matters, not a fresh TTL
    now = time.time()
    rows = conn.execute(
        "SELECT id, kind FROM jobs WHERE status = 'running' AND lease_expires_at < ?", (now,)
    ).fetchall()
    if not rows:
        return []
    conn.execute(
        "UPDATE jobs SET status = 'queued', worker = NULL, lease_expires_at = 0,"
        " heartbeat_at = NULL, updated_at = ?"
        " WHERE status = 'running' AND lease_expires_at < ?",
        (now, now),
    )
    conn.commit()
    return [{"id": r["id"], "kind": r["kind"]} for r in rows]


def complete(
    conn: sqlite3.Connection, job_id: str, *, result: dict[str, Any] | None = None
) -> None:
    now = time.time()
    conn.execute(
        "UPDATE jobs SET status = 'done', result = ?, updated_at = ? WHERE id = ?",
        (json.dumps(result or {}), now, job_id),
    )
    _clear_lease(conn, job_id, now)
    conn.commit()


def update_result(conn: sqlite3.Connection, job_id: str, result: dict[str, Any]) -> None:
    """Persist intermediate progress for a long-running job and renew its lease.

    Progress reporting is the natural heartbeat point: a bulk job that reports
    after every item is demonstrably alive, so it must not be reaped mid-batch.
    """
    now = time.time()
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is not None and row["status"] == "running":
        conn.execute(
            "UPDATE jobs SET result = ?, updated_at = ?, heartbeat_at = ?, lease_expires_at = ?"
            " WHERE id = ?",
            (
                json.dumps(result, separators=(",", ":")),
                now,
                now,
                now + lease_seconds(),
                job_id,
            ),
        )
    else:
        conn.execute(
            "UPDATE jobs SET result = ?, updated_at = ? WHERE id = ?",
            (json.dumps(result, separators=(",", ":")), now, job_id),
        )
    conn.commit()


def fail(conn: sqlite3.Connection, job_id: str, *, error: str) -> None:
    now = time.time()
    conn.execute(
        "UPDATE jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
        (error[:2000], now, job_id),
    )
    _clear_lease(conn, job_id, now)
    conn.commit()


def requeue(conn: sqlite3.Connection, job_id: str, *, run_after: float | None = None) -> None:
    now = time.time()
    conn.execute(
        "UPDATE jobs SET status = 'queued', worker = NULL, run_after = ?, lease_expires_at = 0,"
        " heartbeat_at = NULL, updated_at = ? WHERE id = ?",
        (run_after or 0.0, now, job_id),
    )
    conn.commit()


def cancel(conn: sqlite3.Connection, job_id: str) -> bool:
    """Cancel a queued job, or a running one whose worker is gone.

    A running job with a live lease is never interrupted: live work is not
    interruptible. A running job whose lease has expired has no worker at all, so it
    is stuck rather than busy, and an operator must be able to clear it.
    """
    now = time.time()
    changed = conn.execute(
        "UPDATE jobs SET status = 'cancelled', worker = NULL, lease_expires_at = 0,"
        " heartbeat_at = NULL, updated_at = ?"
        " WHERE id = ? AND (status = 'queued'"
        " OR (status = 'running' AND lease_expires_at < ?))",
        (now, job_id, now),
    ).rowcount
    conn.commit()
    return changed == 1


def is_stuck(conn: sqlite3.Connection, job_id: str) -> bool:
    """True when a job is running but its lease has lapsed, i.e. no live worker."""
    row = conn.execute(
        "SELECT status, lease_expires_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None or row["status"] != "running":
        return False
    return float(row["lease_expires_at"] or 0.0) < time.time()


def get(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["payload"] = json.loads(job["payload"] or "{}")
    job["result"] = json.loads(job["result"] or "{}")
    job["lease_expires_at"] = float(job.get("lease_expires_at") or 0.0)
    return job


def pending(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        job = dict(r)
        job["payload"] = json.loads(job["payload"] or "{}")
        out.append(job)
    return out


def wedged_projects(conn: sqlite3.Connection) -> list[str]:
    """Projects flagged deleting that no live worker is actually purging.

    These are the tenants an operator has to rescue by hand.
    """
    now = time.time()
    wedged: list[str] = []
    rows = conn.execute("SELECT id FROM projects WHERE deleting = 1 AND id != 'local-project'")
    for row in rows.fetchall():
        active = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind = 'purge_project'"
            " AND json_extract(payload, '$.project_id') = ?"
            " AND (status = 'queued' OR (status = 'running' AND lease_expires_at >= ?))",
            (row["id"], now),
        ).fetchone()[0]
        if not active:
            wedged.append(row["id"])
    return wedged
