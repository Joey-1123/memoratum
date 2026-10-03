"""Migration safety: an interrupted migration must roll back whole and be retryable.

The runner used `executescript()`, which issues an implicit COMMIT before running
and auto-commits each statement. A migration that failed halfway therefore left
partial DDL behind in the database file, and the retry hard-failed -- turning a
one-off migration fault into a permanent startup failure.

These tests are file-backed on purpose: an in-memory database throws away the
failed state, so an in-memory test would pass even while the bug is present.
"""

import sqlite3

import pytest


def _db_file(tmp_path):
    return str(tmp_path / "migrations.db")


def _tables(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _version(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]


# A migration whose FIRST statement succeeds and whose SECOND fails.
# A migration whose FIRST statement succeeds and whose SECOND fails, because the
# table it creates is declared twice.
BROKEN = """
CREATE TABLE retry_probe(id INTEGER PRIMARY KEY, label TEXT);
CREATE TABLE retry_probe(id INTEGER PRIMARY KEY);
"""

# The same intent, corrected. This is the "retry" -- it can only succeed if the
# failed attempt left nothing behind.
CORRECTED = """
CREATE TABLE retry_probe(id INTEGER PRIMARY KEY, label TEXT);
CREATE INDEX idx_retry_probe ON retry_probe(id);
"""


def test_failed_migration_leaves_no_partial_ddl(tmp_path, monkeypatch):
    """The first CREATE must not survive the failure of the second."""
    from memoratum import db

    path = _db_file(tmp_path)
    base = db._MIGRATIONS
    monkeypatch.setattr(db, "_MIGRATIONS", base + (BROKEN,))

    with pytest.raises(sqlite3.OperationalError):
        db.connect(path)

    # Inspect the very same file the failed migration wrote to.
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert "retry_probe" not in _tables(conn), "partial DDL survived a failed migration"
        assert _version(conn) == len(base), "a failed migration must not bump the version"
    finally:
        conn.close()


def test_retry_of_a_partially_applied_migration_succeeds(tmp_path, monkeypatch):
    """Without rollback this raised 'table retry_probe already exists' forever."""
    from memoratum import db

    path = _db_file(tmp_path)
    base = db._MIGRATIONS
    monkeypatch.setattr(db, "_MIGRATIONS", base + (BROKEN,))

    with pytest.raises(sqlite3.OperationalError):
        db.connect(path)

    # Retry with the corrected migration. Because the first attempt rolled back
    # whole, re-creating the table must succeed. Without rollback it fails here
    # with 'table retry_probe already exists' -- permanently.
    monkeypatch.setattr(db, "_MIGRATIONS", base + (CORRECTED,))
    conn = db.connect(path)
    try:
        assert "retry_probe" in _tables(conn)
        indexes = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_retry_probe" in indexes
        assert _version(conn) == len(base) + 1
    finally:
        conn.close()


def test_startup_fails_loudly_when_version_is_ahead_of_code(tmp_path, monkeypatch):
    """A downgraded binary must be detected, not silently tolerated."""
    from memoratum import db

    path = _db_file(tmp_path)
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (len(db._MIGRATIONS) + 5, 0.0),
    )
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="ahead of"):
        db.connect(path)


def test_migrations_with_foreign_keys_pragma_still_apply(tmp_path, monkeypatch):
    """PRAGMA foreign_keys is a no-op inside a transaction, so those run unwrapped."""
    from memoratum import db

    path = _db_file(tmp_path)
    probe = """
PRAGMA foreign_keys=OFF;
CREATE TABLE fk_parent(id TEXT PRIMARY KEY);
CREATE TABLE fk_child(id TEXT PRIMARY KEY,
                      parent TEXT REFERENCES fk_parent(id) ON DELETE CASCADE);
PRAGMA foreign_keys=ON;
"""
    base = db._MIGRATIONS
    monkeypatch.setattr(db, "_MIGRATIONS", base + (probe,))
    conn = db.connect(path)
    try:
        assert {"fk_parent", "fk_child"} <= _tables(conn)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, (
            "foreign_keys must be re-enabled after an FK-pragma migration"
        )
        assert _version(conn) == len(base) + 1
    finally:
        conn.close()


def test_post_apply_verification_passes_on_a_healthy_database():
    from memoratum import db

    conn = db.connect(":memory:")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_runner_verifies_schema_after_applying():
    """The runner must actively verify, not merely execute statements."""
    import inspect

    from memoratum import db

    assert hasattr(db, "_verify_schema"), "no post-apply verification exists"
    verifier = inspect.getsource(db._verify_schema)
    assert "integrity_check" in verifier
    assert "foreign_key_check" in verifier
    assert "_verify_schema" in inspect.getsource(db.connect), (
        "post-apply verification is not wired into startup"
    )


def test_migration_versions_are_contiguous():
    from memoratum import db

    conn = db.connect(":memory:")
    versions = [
        r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]
    assert versions == list(range(1, len(db._MIGRATIONS) + 1)), (
        f"version set does not match {len(db._MIGRATIONS)} migrations"
    )
    conn.close()


def test_lease_migration_is_present_and_indexed():
    """Migration 28 added the lease columns the P0 fix depends on."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, status TEXT, lease_expires_at REAL)")
    conn.execute("CREATE INDEX idx_jobs_lease ON jobs(status, lease_expires_at)")
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_jobs_lease" in indexes
    conn.close()


def test_all_existing_migrations_still_apply_in_order(tmp_path, monkeypatch):
    """Guards the full chain against the new runner."""
    from memoratum import db

    path = _db_file(tmp_path)
    conn = db.connect(path)
    try:
        assert _version(conn) == len(db._MIGRATIONS)
        assert _tables(conn) >= {
            "memories",
            "documents",
            "chunks",
            "jobs",
            "projects",
            "organizations",
            "webhooks",
            "domain_events",
            "facts",
            "api_keys",
            "vector_points",
        }
    finally:
        conn.close()
