"""Atomic outbox behavior for memory events."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from memoratum import db, webhooks


def _conn() -> sqlite3.Connection:
    return db.connect(os.path.join(tempfile.mkdtemp(), "memoratum.db"))


def test_domain_event_rejects_unknown_type_before_writing() -> None:
    conn = _conn()
    project_id = "project-atomic"
    now = db._now()
    conn.execute(
        "INSERT INTO projects(id, org_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (project_id, "local-org", "Atomic", now, now),
    )
    conn.commit()
    webhooks.create_webhook(
        conn,
        project_id=project_id,
        name="Atomic",
        url="https://example.com/hook",
        event_types=["memory_add"],
        secret="whsec_atomic",
    )
    with pytest.raises(ValueError, match="unsupported webhook event"):
        webhooks.record_event(
            conn,
            project_id=project_id,
            event_type="not-supported",
            memory_id="mem-atomic",
        )
    assert conn.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0] == 0
    conn.close()
