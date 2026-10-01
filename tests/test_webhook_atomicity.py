"""Atomicity checks for outbox fan-out on real memory writes."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from memoratum import db, webhooks


def _conn() -> sqlite3.Connection:
    return db.connect(os.path.join(tempfile.mkdtemp(), "memoratum.db"))


def test_fanout_failure_rolls_back_memory_creation() -> None:
    conn = _conn()
    now = db._now()
    conn.execute(
        "INSERT INTO projects(id, org_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("project-atomic", "local-org", "Atomic", now, now),
    )
    conn.commit()
    webhooks.create_webhook(
        conn,
        project_id="project-atomic",
        name="Atomic",
        url="https://example.com/hook",
        event_types=["memory_add"],
        secret="whsec_atomic",
    )
    conn.execute(
        "CREATE TRIGGER reject_delivery BEFORE INSERT ON webhook_deliveries"
        " BEGIN SELECT RAISE(ABORT, 'injected delivery failure'); END"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected delivery failure"):
        db.create_memory(
            conn,
            text="must roll back",
            container_tag="mem0:user_id:alice",
            project_id="project-atomic",
            org_id="local-org",
        )
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memory_history").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    conn.close()
