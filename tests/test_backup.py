"""Local backup and restore contract (RED)."""

import sqlite3

import pytest


def _database(path: str) -> str:
    from memoratum import db

    conn = db.connect(path)
    conn.execute(
        "INSERT INTO documents(id, container_tag, content, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("d1", "t", "hello", "done", 1, 1),
    )
    conn.commit()
    conn.close()
    return path


def test_backup_uses_sqlite_backup_and_restores(tmp_path) -> None:
    from memoratum.backup import backup_database, restore_database

    source = _database(str(tmp_path / "live.db"))
    backup = tmp_path / "backups" / "snapshot.db"
    backup_database(source, backup)
    assert backup.exists()
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT content FROM documents WHERE id='d1'").fetchone()[0] == "hello"

    target = tmp_path / "restored.db"
    restore_database(backup, target, force=True)
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT content FROM documents WHERE id='d1'").fetchone()[0] == "hello"


def test_backup_refuses_overwrite_without_force(tmp_path) -> None:
    from memoratum.backup import backup_database

    source = _database(str(tmp_path / "live.db"))
    destination = tmp_path / "snapshot.db"
    destination.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        backup_database(source, destination)
    backup_database(source, destination, force=True)
    assert destination.read_bytes() != b"existing"


def test_restore_rejects_invalid_snapshot(tmp_path) -> None:
    from memoratum.backup import restore_database

    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite")
    with pytest.raises(ValueError, match="integrity"):
        restore_database(bad, tmp_path / "restored.db", force=True)
