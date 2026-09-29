"""Migration checks for the webhook/outbox schema."""

from __future__ import annotations

import os
import sqlite3
import tempfile

from memoratum import db


def _database_at_version(path: str, version: int) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        for index, script in enumerate(db._MIGRATIONS[:version], start=1):
            conn.executescript(script)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 0)",
                (index,),
            )
        conn.commit()
    finally:
        conn.close()


def test_webhook_schema_is_created_on_a_fresh_database() -> None:
    conn = db.connect(":memory:")
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"webhooks", "domain_events", "webhook_deliveries"} <= tables
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        assert "run_after" in columns
    finally:
        conn.close()


def test_webhook_schema_is_added_to_a_v0_1_database_without_losing_rows() -> None:
    path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    _database_at_version(path, 20)
    legacy = sqlite3.connect(path)
    legacy.execute(
        "INSERT INTO projects(id, org_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("legacy-project", "local-org", "Legacy", 1, 1),
    )
    legacy.execute(
        "INSERT INTO jobs(id, kind, payload, status, attempts, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy-job", "noop", "{}", "done", 1, 1, 1),
    )
    legacy.commit()
    legacy.close()

    conn = db.connect(path)
    try:
        assert "run_after" in {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        assert (
            conn.execute("SELECT name FROM projects WHERE id = 'legacy-project'").fetchone()[0]
            == "Legacy"
        )
        assert (
            conn.execute("SELECT id FROM jobs WHERE id = 'legacy-job'").fetchone()[0]
            == "legacy-job"
        )
        assert {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        } >= {
            "webhooks",
            "domain_events",
            "webhook_deliveries",
        }
    finally:
        conn.close()
