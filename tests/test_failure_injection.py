"""Failure-injection harnesses.

Constitution Principle IV: a green suite is not evidence. These harnesses inject
the failures the existing suite never produced -- a killed worker, a crash between
commits, a rebinding resolver, and a database re-opened after a crash.

Every test here MUST fail before its corresponding fix and pass after.
"""

import contextlib
import json
import sqlite3
import time

import pytest
from helpers import use_tmp_data_dir


class CrashInjected(RuntimeError):
    """Raised by the crash_between_commits harness."""


@pytest.fixture
def conn():
    """A migrated database in an isolated data directory."""
    use_tmp_data_dir()
    from memoratum import db
    from memoratum.config import Settings

    connection = db.connect(Settings.load().db_path)
    yield connection
    connection.close()


@pytest.fixture
def crash_between_commits():
    """Fail the Nth COMMIT issued during the block.

    Usage::

        with crash_between_commits(2) as reached:
            client.post("/v3/memories/add/", ...)

    ``reached`` counts the commits that were allowed through before the crash.
    """
    from memoratum import db

    state = {"commits": 0, "target": 0}

    @contextlib.contextmanager
    def arm(target: int):
        state["target"] = target
        state["commits"] = 0
        original = db.connect

        def traced(path):
            real = original(path)

            def on_trace(statement: str) -> None:
                if not statement.strip().upper().startswith("COMMIT"):
                    return
                state["commits"] += 1
                if state["commits"] == state["target"]:
                    raise CrashInjected(f"crash before commit #{state['commits']}")

            real.set_trace_callback(on_trace)
            return real

        db.connect = traced
        try:
            yield state
        finally:
            db.connect = original

    return arm


@pytest.fixture
def rebinding_resolver(monkeypatch):
    """Answer public on the first lookup, link-local on every lookup after.

    Models an attacker-controlled DNS record. Any code path that resolves more
    than once per delivery is exploitable: the fix must resolve exactly once and
    connect to that validated address.
    """
    import socket

    calls = {"n": 0}
    public = "93.184.216.34"
    internal = "169.254.169.254"

    def resolver(host, port, *args, **kwargs):
        calls["n"] += 1
        address = public if calls["n"] == 1 else internal
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    return {"calls": calls, "public": public, "internal": internal}


@pytest.fixture
def reopen_db():
    """Re-open the database from disk, discarding all in-process state."""

    def _reopen():
        from memoratum import db
        from memoratum.config import Settings

        return db.connect(Settings.load().db_path)

    return _reopen


def strand_running_job(conn, *, lease_seconds: float | None = None) -> str:
    """Leave a job ``running`` the way an uncatchable kill does.

    A SIGKILL releases nothing: the lease is not cleared and no heartbeat is sent.
    ``lease_seconds`` sets the deadline when the lease column exists; otherwise the
    row is left exactly as a pre-lease implementation would leave it.
    """
    row = conn.execute("SELECT id FROM jobs WHERE status = 'running' LIMIT 1").fetchone()
    if row is None:
        raise AssertionError("expected a running job to strand")
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "lease_expires_at" in columns:
        deadline = time.time() if lease_seconds is None else time.time() + lease_seconds
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ?, heartbeat_at = ? WHERE id = ?",
            (deadline, time.time(), row["id"]),
        )
    else:
        deadline = None
    conn.commit()
    return json.dumps({"job_id": row["id"], "lease_expires_at": deadline})


def assert_no_orphan_documents(conn, *, project_id: str | None = None) -> None:
    """Assert no document is stranded without the job that would process it."""
    sql = (
        "SELECT COUNT(*) FROM documents d WHERE NOT EXISTS ("
        "  SELECT 1 FROM jobs j WHERE j.kind = 'ingest'"
        "   AND json_extract(j.payload, '$.document_id') = d.id)"
    )
    params: tuple = ()
    if project_id is not None:
        sql += " AND d.project_id = ?"
        params = (project_id,)
    orphans = conn.execute(sql, params).fetchone()[0]
    assert orphans == 0, f"{orphans} document(s) stranded with no ingest job"


def integrity_ok(path: str) -> bool:
    try:
        with sqlite3.connect(path) as check:
            return check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        return False
