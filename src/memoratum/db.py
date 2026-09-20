# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SQLite store: WAL + FTS5, forward-only migrations, parameterized queries."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from typing import Any

_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS documents(
      id TEXT PRIMARY KEY,
      container_tag TEXT NOT NULL,
      custom_id TEXT,
      content TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      UNIQUE(container_tag, custom_id)
    );
    CREATE INDEX IF NOT EXISTS idx_documents_tag ON documents(container_tag);
    CREATE TABLE IF NOT EXISTS chunks(
      id INTEGER PRIMARY KEY,
      document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      idx INTEGER NOT NULL,
      text TEXT NOT NULL,
      embedding BLOB,
      created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='id');
    CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
      INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
    END;
    CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
      INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END;
    CREATE TABLE IF NOT EXISTS api_keys(
      key_hash TEXT PRIMARY KEY,
      container_tag TEXT,
      created_at REAL NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS facts(
      id TEXT PRIMARY KEY,
      container_tag TEXT NOT NULL,
      subject TEXT NOT NULL,
      predicate TEXT NOT NULL,
      object TEXT NOT NULL,
      document_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
      valid_from REAL NOT NULL,
      valid_to REAL,
      superseded_by TEXT REFERENCES facts(id),
      created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_facts_tag ON facts(container_tag);
    CREATE INDEX IF NOT EXISTS idx_facts_spo ON facts(container_tag, subject, predicate);
    """,
    """
    ALTER TABLE documents ADD COLUMN dreamed_at REAL;
    """,
    """
    ALTER TABLE documents ADD COLUMN metadata TEXT;
    ALTER TABLE facts ADD COLUMN metadata TEXT;
    """,
)


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    current = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    for i, sql in enumerate(_MIGRATIONS, start=1):
        if i > current:
            db.executescript(sql)
            db.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (i, time.time())
            )
    db.commit()
    return db


def _now() -> float:
    return time.time()


def create_document(
    db: sqlite3.Connection,
    *,
    container_tag: str,
    content: str,
    custom_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _now()
    meta = json.dumps(metadata or {})
    if custom_id is not None:
        row = db.execute(
            "SELECT id FROM documents WHERE container_tag = ? AND custom_id = ?",
            (container_tag, custom_id),
        ).fetchone()
        if row is not None:
            db.execute(
                "UPDATE documents SET content = ?, status = 'queued', updated_at = ?, metadata = ?, dreamed_at = NULL"
                " WHERE id = ?",
                (content, now, meta, row["id"]),
            )
            db.execute("DELETE FROM chunks WHERE document_id = ?", (row["id"],))
            db.commit()
            return get_document(db, row["id"])
    doc_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO documents(id, container_tag, custom_id, content, status, created_at, updated_at, metadata)"
        " VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
        (doc_id, container_tag, custom_id, content, now, now, meta),
    )
    db.commit()
    return get_document(db, doc_id)


def get_document(db: sqlite3.Connection, doc_id: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        raise KeyError(doc_id)
    doc = dict(row)
    doc["metadata"] = json.loads(doc.get("metadata") or "{}")
    return doc


def set_status(db: sqlite3.Connection, doc_id: str, status: str) -> None:
    db.execute(
        "UPDATE documents SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), doc_id)
    )
    db.commit()


def add_chunks(
    db: sqlite3.Connection, doc_id: str, texts: list[str], embeddings: list[bytes] | None = None
) -> list[int]:
    ids: list[int] = []
    now = _now()
    for i, text in enumerate(texts):
        emb = embeddings[i] if embeddings is not None else None
        cur = db.execute(
            "INSERT INTO chunks(document_id, idx, text, embedding, created_at) VALUES (?, ?, ?, ?, ?)",
            (doc_id, i, text, emb, now),
        )
        ids.append(cur.lastrowid)
    db.commit()
    return ids


def fts_query(query: str) -> str | None:
    """Sanitize free text into an FTS5 MATCH expression (OR of quoted tokens)."""
    tokens = re.findall(r"[A-Za-z0-9_]+", query)
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


def keyword_search(
    db: sqlite3.Connection, query: str, *, container_tag: str | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    match = fts_query(query)
    if match is None:
        return []
    if container_tag is None:
        rows = db.execute(
            "SELECT c.id, c.document_id, c.text, rank FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
            " WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT c.id, c.document_id, c.text, rank FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
            " JOIN documents d ON d.id = c.document_id WHERE chunks_fts MATCH ? AND d.container_tag = ?"
            " ORDER BY rank LIMIT ?",
            (match, container_tag, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_api_key(db: sqlite3.Connection, *, container_tag: str | None = None) -> str:
    raw = "mm_" + secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO api_keys(key_hash, container_tag, created_at) VALUES (?, ?, ?)",
        (_hash_key(raw), container_tag, _now()),
    )
    db.commit()
    return raw


def lookup_key(db: sqlite3.Connection, raw: str) -> dict[str, Any] | None:
    """Return the key row (container_tag None = wildcard) or None if unknown."""
    row = db.execute(
        "SELECT container_tag FROM api_keys WHERE key_hash = ?", (_hash_key(raw),)
    ).fetchone()
    if row is None:
        return None
    return dict(row)
