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

# Rebuild the unique key first, then restore the legacy dreaming column as a
# separate migration so databases interrupted during the rebuild can recover.
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
    """
    CREATE TABLE IF NOT EXISTS revoked_keys(
      key_hash TEXT PRIMARY KEY,
      revoked_at REAL NOT NULL
    );
    """,
    """
    ALTER TABLE facts ADD COLUMN embedding BLOB;
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs(
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,
      payload TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL DEFAULT 'queued',
      attempts INTEGER NOT NULL DEFAULT 0,
      result TEXT,
      error TEXT,
      worker TEXT,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
    """,
    """
    ALTER TABLE documents ADD COLUMN expires_at REAL;
    ALTER TABLE facts ADD COLUMN expires_at REAL;
    """,
    """
    ALTER TABLE facts ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'semantic';
    """,
    """
    ALTER TABLE api_keys ADD COLUMN org_id TEXT;
    ALTER TABLE documents ADD COLUMN org_id TEXT;
    ALTER TABLE facts ADD COLUMN org_id TEXT;
    """,
    """
    PRAGMA foreign_keys=OFF;
    DROP TABLE IF EXISTS documents_org_scoped;
    CREATE TABLE documents_org_scoped(
      id TEXT PRIMARY KEY,
      container_tag TEXT NOT NULL,
      custom_id TEXT,
      content TEXT NOT NULL,
      status TEXT NOT NULL,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      metadata TEXT,
      expires_at REAL,
      org_id TEXT,
      UNIQUE(container_tag, custom_id, org_id)
    );
    INSERT INTO documents_org_scoped(
      id, container_tag, custom_id, content, status, created_at, updated_at, metadata, expires_at, org_id
    )
    SELECT id, container_tag, custom_id, content, status, created_at, updated_at, metadata, expires_at, org_id
    FROM documents;
    DROP TABLE documents;
    ALTER TABLE documents_org_scoped RENAME TO documents;
    CREATE INDEX IF NOT EXISTS idx_documents_tag ON documents(container_tag);
    PRAGMA foreign_keys=ON;
    """,
    """
    ALTER TABLE documents ADD COLUMN dreamed_at REAL;
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events(
      id TEXT PRIMARY KEY,
      created_at REAL NOT NULL,
      actor_kind TEXT NOT NULL,
      actor_key_hash TEXT,
      container_tag TEXT,
      org_id TEXT,
      action TEXT NOT NULL,
      resource_type TEXT,
      resource_id TEXT,
      outcome TEXT NOT NULL DEFAULT 'succeeded',
      metadata TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_audit_scope ON audit_events(container_tag, org_id, created_at DESC);
    CREATE TABLE IF NOT EXISTS usage_counters(
      key_hash TEXT NOT NULL,
      container_tag TEXT NOT NULL,
      org_id TEXT NOT NULL,
      requests INTEGER NOT NULL DEFAULT 0,
      searches INTEGER NOT NULL DEFAULT 0,
      document_writes INTEGER NOT NULL DEFAULT 0,
      fact_writes INTEGER NOT NULL DEFAULT 0,
      input_chars INTEGER NOT NULL DEFAULT 0,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      PRIMARY KEY(key_hash, container_tag, org_id)
    );
    CREATE INDEX IF NOT EXISTS idx_usage_updated ON usage_counters(updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_usage_scope ON usage_counters(container_tag, org_id, updated_at DESC);
    """,
    """
    CREATE TABLE IF NOT EXISTS vector_points(
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,
      text TEXT NOT NULL,
      vector BLOB NOT NULL,
      container_tag TEXT NOT NULL,
      org_id TEXT,
      metadata TEXT NOT NULL DEFAULT '{}',
      created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_vector_scope ON vector_points(container_tag, org_id);
    CREATE INDEX IF NOT EXISTS idx_vector_created ON vector_points(created_at DESC);
    """,
    """
    CREATE TABLE IF NOT EXISTS share_links(
      id TEXT PRIMARY KEY,
      token_hash TEXT UNIQUE NOT NULL,
      document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      created_by TEXT,
      created_at REAL NOT NULL,
      expires_at REAL NOT NULL,
      revoked_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_share_document ON share_links(document_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_share_expiry ON share_links(expires_at, revoked_at);
    """,
)


def connect(path: str) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI runs dependency setup and endpoint bodies
    # on different worker threads; each request still gets its own connection.
    db = sqlite3.connect(path, check_same_thread=False)
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
    expires_at: float | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    now = _now()
    meta = json.dumps(metadata or {})
    if custom_id is not None:
        org_clause = "org_id IS NULL" if org_id is None else "org_id = ?"
        org_params: tuple[Any, ...] = () if org_id is None else (org_id,)
        row = db.execute(
            f"SELECT id FROM documents WHERE container_tag = ? AND custom_id = ? AND {org_clause}",
            (container_tag, custom_id, *org_params),
        ).fetchone()
        if row is not None:
            db.execute(
                "UPDATE documents SET content = ?, status = 'queued', updated_at = ?, metadata = ?, dreamed_at = NULL,"
                " expires_at = ?, org_id = ? WHERE id = ?",
                (content, now, meta, expires_at, org_id, row["id"]),
            )
            db.execute("DELETE FROM chunks WHERE document_id = ?", (row["id"],))
            db.commit()
            return get_document(db, row["id"])
    doc_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO documents(id, container_tag, custom_id, content, status, created_at, updated_at, metadata,"
        " expires_at, org_id) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
        (doc_id, container_tag, custom_id, content, now, now, meta, expires_at, org_id),
    )
    db.commit()
    return get_document(db, doc_id)


def update_document(
    db: sqlite3.Connection,
    doc_id: str,
    *,
    content: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """In-place content/metadata replacement: re-queues, drops chunks, clears dream state."""
    doc = get_document(db, doc_id)
    new_content = content if content is not None else doc["content"]
    new_meta = json.dumps(metadata if metadata is not None else doc.get("metadata") or {})
    db.execute(
        "UPDATE documents SET content = ?, metadata = ?, status = 'queued', updated_at = ?, dreamed_at = NULL"
        " WHERE id = ?",
        (new_content, new_meta, _now(), doc_id),
    )
    db.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
    db.commit()
    return get_document(db, doc_id)


def get_document(db: sqlite3.Connection, doc_id: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        raise KeyError(doc_id)
    doc = dict(row)
    doc["metadata"] = json.loads(doc.get("metadata") or "{}")
    return doc


def create_share_link(
    db: sqlite3.Connection,
    *,
    token_hash: str,
    document_id: str,
    created_by: str | None,
    expires_at: float,
) -> dict[str, Any]:
    link_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO share_links(id, token_hash, document_id, created_by, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (link_id, token_hash, document_id, created_by, _now(), expires_at),
    )
    db.commit()
    link = get_share_link(db, link_id)
    if link is None:
        raise RuntimeError("share link creation failed")
    return link


def get_share_link(db: sqlite3.Connection, link_id: str) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM share_links WHERE id = ?", (link_id,)).fetchone()
    return dict(row) if row else None


def get_share_link_by_token(db: sqlite3.Connection, token_hash: str) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM share_links WHERE token_hash = ?", (token_hash,)).fetchone()
    return dict(row) if row else None


def revoke_share_link(db: sqlite3.Connection, link_id: str) -> bool:
    cur = db.execute(
        "UPDATE share_links SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (_now(), link_id),
    )
    db.commit()
    return cur.rowcount > 0


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
    db: sqlite3.Connection,
    query: str,
    *,
    container_tag: str | None = None,
    org_id: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    match = fts_query(query)
    if match is None:
        return []
    joins = " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
    conditions = ["chunks_fts MATCH ?"]
    params: list[Any] = [match]
    if container_tag is not None or org_id is not None:
        joins += " JOIN documents d ON d.id = c.document_id"
    if container_tag is not None:
        conditions.append("d.container_tag = ?")
        params.append(container_tag)
    if org_id is not None:
        conditions.append("d.org_id = ?")
        params.append(org_id)
    params.append(limit)
    rows = db.execute(
        f"SELECT c.id, c.document_id, c.text, rank{joins}"
        f" WHERE {' AND '.join(conditions)} ORDER BY rank LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def hash_key(raw: str) -> str:
    """Return the non-reversible identifier used for local accounting records."""
    return _hash_key(raw)


def create_api_key(
    db: sqlite3.Connection, *, container_tag: str | None = None, org_id: str | None = None
) -> str:
    raw = "mm_" + secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO api_keys(key_hash, container_tag, created_at, org_id) VALUES (?, ?, ?, ?)",
        (_hash_key(raw), container_tag, _now(), org_id),
    )
    db.commit()
    return raw


def lookup_key(db: sqlite3.Connection, raw: str) -> dict[str, Any] | None:
    """Return the key row (container_tag/org_id None = wildcard/default), None if unknown/revoked."""
    h = _hash_key(raw)
    if db.execute("SELECT 1 FROM revoked_keys WHERE key_hash = ?", (h,)).fetchone() is not None:
        return None
    row = db.execute(
        "SELECT key_hash, container_tag, org_id FROM api_keys WHERE key_hash = ?", (h,)
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def revoke_key(db: sqlite3.Connection, raw: str) -> bool:
    """Revoke a key by its raw value. Returns True if a known key was revoked."""
    h = _hash_key(raw)
    if db.execute("SELECT 1 FROM api_keys WHERE key_hash = ?", (h,)).fetchone() is None:
        return False
    db.execute(
        "INSERT OR IGNORE INTO revoked_keys(key_hash, revoked_at) VALUES (?, ?)", (h, time.time())
    )
    db.commit()
    return True


def prune_expired(conn: sqlite3.Connection) -> dict[str, int]:
    """Hard-delete expired documents (chunks cascade) and facts. Returns counts."""
    now = time.time()
    facts = conn.execute(
        "DELETE FROM facts WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
    ).rowcount
    docs = conn.execute(
        "DELETE FROM documents WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
    ).rowcount
    conn.commit()
    return {"facts": facts, "documents": docs}


def purge_tag(
    db: sqlite3.Connection, container_tag: str, *, org_id: str | None = None
) -> dict[str, int]:
    """Delete everything in a tag, optionally limited to one organization."""
    where = "container_tag = ?"
    params: tuple[Any, ...] = (container_tag,)
    if org_id is not None:
        where += " AND org_id = ?"
        params += (org_id,)
    facts = db.execute(f"DELETE FROM facts WHERE {where}", params).rowcount
    docs = db.execute(f"DELETE FROM documents WHERE {where}", params).rowcount
    keys = db.execute(f"DELETE FROM api_keys WHERE {where}", params).rowcount
    db.commit()
    return {"facts": facts, "documents": docs, "keys": keys}


_USAGE_COLUMNS = {
    "request": None,
    "search": "searches",
    "document": "document_writes",
    "fact": "fact_writes",
}


def _scope_value(value: str | None) -> str:
    return value or ""


def _scope_filter(
    *, container_tag: str | None, org_id: str | None, prefix: str = ""
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if container_tag is not None:
        conditions.append(f"{prefix}container_tag = ?")
        params.append(_scope_value(container_tag))
    if org_id is not None:
        conditions.append(f"{prefix}org_id = ?")
        params.append(_scope_value(org_id))
    return (" AND ".join(conditions), params)


def record_usage(
    db: sqlite3.Connection,
    *,
    key_hash: str | None,
    container_tag: str | None,
    org_id: str | None,
    operation: str,
    units: int = 1,
    input_chars: int = 0,
) -> None:
    """Increment one local per-key usage row; no data leaves the process."""
    if operation not in _USAGE_COLUMNS:
        raise ValueError(f"unknown usage operation: {operation}")
    if units < 0 or input_chars < 0:
        raise ValueError("usage units and input_chars must be non-negative")
    counter_column = _USAGE_COLUMNS[operation]
    now = _now()
    key = _scope_value(key_hash) or "anonymous"
    tag = _scope_value(container_tag)
    org = _scope_value(org_id)
    values: dict[str, int] = {
        "requests": units,
        "searches": 0,
        "document_writes": 0,
        "fact_writes": 0,
        "input_chars": input_chars,
    }
    if counter_column is not None:
        values[counter_column] = units
    db.execute(
        """
        INSERT INTO usage_counters(
          key_hash, container_tag, org_id, requests, searches,
          document_writes, fact_writes, input_chars, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key_hash, container_tag, org_id) DO UPDATE SET
          requests = requests + excluded.requests,
          searches = searches + excluded.searches,
          document_writes = document_writes + excluded.document_writes,
          fact_writes = fact_writes + excluded.fact_writes,
          input_chars = input_chars + excluded.input_chars,
          updated_at = excluded.updated_at
        """,
        (
            key,
            tag,
            org,
            values["requests"],
            values["searches"],
            values["document_writes"],
            values["fact_writes"],
            values["input_chars"],
            now,
            now,
        ),
    )
    db.commit()


def list_usage(
    db: sqlite3.Connection,
    *,
    container_tag: str | None = None,
    org_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, params = _scope_filter(container_tag=container_tag, org_id=org_id)
    clause = f" WHERE {where}" if where else ""
    rows = db.execute(
        "SELECT key_hash, container_tag, org_id, requests, searches, document_writes,"
        " fact_writes, input_chars, created_at, updated_at"
        f" FROM usage_counters{clause}"
        " ORDER BY updated_at DESC, key_hash LIMIT ? OFFSET ?",
        [*params, max(1, min(limit, 500)), max(0, offset)],
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["container_tag"] = item["container_tag"] or None
        item["org_id"] = item["org_id"] or None
        out.append(item)
    return out


def count_usage(
    db: sqlite3.Connection, *, container_tag: str | None = None, org_id: str | None = None
) -> int:
    where, params = _scope_filter(container_tag=container_tag, org_id=org_id)
    clause = f" WHERE {where}" if where else ""
    return int(
        db.execute(f"SELECT COUNT(*) AS n FROM usage_counters{clause}", params).fetchone()["n"]
    )


def append_audit_event(
    db: sqlite3.Connection,
    *,
    actor_kind: str,
    actor_key_hash: str | None,
    container_tag: str | None,
    org_id: str | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    outcome: str = "succeeded",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Append an accountability event without storing credentials or payloads."""
    if actor_kind not in {"admin", "key", "anonymous", "oidc"}:
        raise ValueError(f"unknown audit actor kind: {actor_kind}")
    event_id = uuid.uuid4().hex
    db.execute(
        """
        INSERT INTO audit_events(
          id, created_at, actor_kind, actor_key_hash, container_tag, org_id,
          action, resource_type, resource_id, outcome, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            _now(),
            actor_kind,
            # Hash again at the persistence boundary in case a caller passes a raw value.
            hashlib.sha256(actor_key_hash.encode()).hexdigest()[:16] if actor_key_hash else None,
            container_tag,
            org_id,
            action,
            resource_type,
            resource_id,
            outcome,
            json.dumps(metadata or {}, separators=(",", ":")),
        ),
    )
    db.commit()
    return event_id


def list_audit_events(
    db: sqlite3.Connection,
    *,
    container_tag: str | None = None,
    org_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, params = _scope_filter(container_tag=container_tag, org_id=org_id)
    clause = f" WHERE {where}" if where else ""
    rows = db.execute(
        "SELECT id, created_at, actor_kind, actor_key_hash, container_tag, org_id, action,"
        " resource_type, resource_id, outcome, metadata"
        f" FROM audit_events{clause}"
        " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        [*params, max(1, min(limit, 200)), max(0, offset)],
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item["metadata"] or "{}")
        out.append(item)
    return out


def count_audit_events(
    db: sqlite3.Connection, *, container_tag: str | None = None, org_id: str | None = None
) -> int:
    where, params = _scope_filter(container_tag=container_tag, org_id=org_id)
    clause = f" WHERE {where}" if where else ""
    return int(
        db.execute(f"SELECT COUNT(*) AS n FROM audit_events{clause}", params).fetchone()["n"]
    )
