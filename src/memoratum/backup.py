# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local SQLite backup and restore helpers.

The server must be stopped for restore. Backup uses SQLite's online backup API,
writes to a temporary file, checks integrity, and atomically renames the result.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path


def _check_integrity(path: str | Path) -> None:
    try:
        with sqlite3.connect(path) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"SQLite integrity check failed: {exc}") from exc
    if not result or result[0] != "ok":
        raise ValueError(f"SQLite integrity check failed: {result[0] if result else 'no result'}")


def backup_database(source: str | Path, destination: str | Path, *, force: bool = False) -> Path:
    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("source and destination must be different files")
    if destination_path.exists() and not force:
        raise FileExistsError(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    _check_integrity(source_path)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            dir=destination_path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        with sqlite3.connect(source_path) as source_conn, sqlite3.connect(temporary) as target_conn:
            source_conn.backup(target_conn)
            target_conn.commit()
        _check_integrity(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination_path


def restore_database(snapshot: str | Path, destination: str | Path, *, force: bool = False) -> Path:
    snapshot_path = Path(snapshot)
    destination_path = Path(destination)
    if not snapshot_path.is_file():
        raise FileNotFoundError(snapshot_path)
    if snapshot_path.resolve() == destination_path.resolve():
        raise ValueError("snapshot and destination must be different files")
    if destination_path.exists() and not force:
        raise FileExistsError(destination_path)
    _check_integrity(snapshot_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination_path.name}.",
            suffix=".restore",
            dir=destination_path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        shutil.copyfile(snapshot_path, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination_path)
        temporary = None
        for suffix in ("-wal", "-shm"):
            Path(str(destination_path) + suffix).unlink(missing_ok=True)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Local SQLite backup/restore")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--source", required=True)
    backup.add_argument("--destination", required=True)
    backup.add_argument("--force", action="store_true")
    restore = subparsers.add_parser("restore")
    restore.add_argument("--snapshot", required=True)
    restore.add_argument("--destination", required=True)
    restore.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.command == "backup":
        result = backup_database(args.source, args.destination, force=args.force)
    else:
        result = restore_database(args.snapshot, args.destination, force=args.force)
    print(result)


if __name__ == "__main__":
    main()
