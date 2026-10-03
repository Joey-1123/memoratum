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
from dataclasses import dataclass
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
    """
    CREATE TABLE IF NOT EXISTS memories(
      id TEXT PRIMARY KEY,
      container_tag TEXT NOT NULL,
      text TEXT NOT NULL,
      metadata TEXT NOT NULL DEFAULT '{}',
      org_id TEXT,
      document_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
      fact_id TEXT REFERENCES facts(id) ON DELETE SET NULL,
      expires_at REAL,
      version INTEGER NOT NULL DEFAULT 1,
      state TEXT NOT NULL DEFAULT 'active',
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(container_tag, org_id, state);
    CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC);
    CREATE TABLE IF NOT EXISTS memory_history(
      id TEXT PRIMARY KEY,
      memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
      event TEXT NOT NULL,
      old_text TEXT,
      new_text TEXT,
      metadata TEXT NOT NULL DEFAULT '{}',
      version INTEGER NOT NULL,
      actor_key_hash TEXT,
      created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_memory_history ON memory_history(memory_id, created_at);
    """,
    """
    ALTER TABLE memory_history ADD COLUMN content_hash TEXT;
    ALTER TABLE memory_history ADD COLUMN updated_at REAL;
    INSERT OR IGNORE INTO memories(
      id, container_tag, text, metadata, org_id, document_id, fact_id, expires_at,
      version, state, created_at, updated_at
    )
    SELECT
      'mem_' || f.id,
      f.container_tag,
      trim(f.subject || ' ' || f.predicate || ' ' || f.object),
      COALESCE(f.metadata, '{}'),
      f.org_id,
      f.document_id,
      f.id,
      f.expires_at,
      1,
      'active',
      f.created_at,
      f.created_at
    FROM facts AS f;
    INSERT OR IGNORE INTO memory_history(
      id, memory_id, event, old_text, new_text, metadata, version, actor_key_hash, created_at,
      content_hash, updated_at
    )
    SELECT
      'mh_' || f.id,
      'mem_' || f.id,
      'ADD',
      NULL,
      trim(f.subject || ' ' || f.predicate || ' ' || f.object),
      COALESCE(f.metadata, '{}'),
      1,
      NULL,
      f.created_at,
      NULL,
      f.created_at
    FROM facts AS f;
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys(
      scope TEXT NOT NULL,
      key TEXT NOT NULL,
      request_hash TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'in_flight',
      response TEXT,
      job_id TEXT,
      created_at REAL NOT NULL,
      expires_at REAL NOT NULL,
      PRIMARY KEY(scope, key)
    );
    CREATE INDEX IF NOT EXISTS idx_idempotency_expiry ON idempotency_keys(expires_at);
    """,
    """
    ALTER TABLE memories ADD COLUMN event_at REAL;
    """,
    """
    ALTER TABLE api_keys ADD COLUMN project_id TEXT;
    ALTER TABLE documents ADD COLUMN project_id TEXT;
    ALTER TABLE facts ADD COLUMN project_id TEXT;
    ALTER TABLE memories ADD COLUMN project_id TEXT;
    ALTER TABLE jobs ADD COLUMN project_id TEXT;
    ALTER TABLE vector_points ADD COLUMN project_id TEXT;
    ALTER TABLE audit_events ADD COLUMN project_id TEXT;
    ALTER TABLE usage_counters ADD COLUMN project_id TEXT;
    CREATE TABLE IF NOT EXISTS organizations(
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS projects(
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      description TEXT,
      custom_instructions TEXT,
      custom_categories TEXT,
      agent_custom_instructions TEXT,
      multilingual INTEGER NOT NULL DEFAULT 0,
      decay INTEGER NOT NULL DEFAULT 0,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_projects_org ON projects(org_id, updated_at DESC);
    CREATE TABLE IF NOT EXISTS project_members(
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      email TEXT NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('OWNER', 'READER')),
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      PRIMARY KEY(project_id, email)
    );
    CREATE INDEX IF NOT EXISTS idx_project_members_email ON project_members(email);
    INSERT OR IGNORE INTO organizations(id, name, created_at, updated_at)
      VALUES ('local-org', 'Local Organization', CAST(strftime('%s','now') AS REAL), CAST(strftime('%s','now') AS REAL));
    INSERT OR IGNORE INTO projects(
      id, org_id, name, description, created_at, updated_at
    ) VALUES (
      'local-project', 'local-org', 'Local Project', 'Default self-hosted project',
      CAST(strftime('%s','now') AS REAL), CAST(strftime('%s','now') AS REAL)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS webhooks(
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      url TEXT NOT NULL,
      event_types TEXT NOT NULL,
      secret TEXT NOT NULL,
      is_active INTEGER NOT NULL DEFAULT 1,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_webhooks_project ON webhooks(project_id, created_at);
    CREATE TABLE IF NOT EXISTS domain_events(
      id TEXT PRIMARY KEY,
      project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
      event_type TEXT NOT NULL,
      memory_id TEXT,
      payload TEXT NOT NULL,
      created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_domain_events_project ON domain_events(project_id, created_at);
    CREATE TABLE IF NOT EXISTS webhook_deliveries(
      id TEXT PRIMARY KEY,
      event_id TEXT NOT NULL REFERENCES domain_events(id) ON DELETE CASCADE,
      webhook_id TEXT NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
      project_id TEXT NOT NULL,
      event_type TEXT NOT NULL,
      memory_id TEXT,
      payload TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt_at REAL,
      last_error TEXT,
      response_status INTEGER,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      UNIQUE(event_id, webhook_id)
    );
    CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_due
      ON webhook_deliveries(status, next_attempt_at, created_at);
    CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_project
      ON webhook_deliveries(project_id, created_at DESC);
    """,
    """
    ALTER TABLE jobs ADD COLUMN run_after REAL NOT NULL DEFAULT 0;
    CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_after, created_at);
    """,
    """
    ALTER TABLE api_keys ADD COLUMN role TEXT NOT NULL DEFAULT 'READER';
    """,
    # 28: job leases. A claim without a deadline is unrecoverable -- an
    # uncatchable worker kill (SIGKILL, OOM) leaves the row `running` forever,
    # a fresh claim() returns None, and cancel() refuses it. lease_expires_at
    # defaults to 0, meaning "no lease held", so pre-existing running rows are
    # treated as dead and recovered by the reaper on first pass.
    """
    ALTER TABLE jobs ADD COLUMN lease_expires_at REAL NOT NULL DEFAULT 0;
    ALTER TABLE jobs ADD COLUMN heartbeat_at REAL;
    CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(status, lease_expires_at);
    """,
    """
    PRAGMA foreign_keys=OFF;
    DROP TABLE IF EXISTS documents_project_scoped;
    CREATE TABLE documents_project_scoped(
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
      dreamed_at REAL,
      project_id TEXT,
      UNIQUE(container_tag, custom_id, org_id, project_id)
    );
    INSERT INTO documents_project_scoped(
      id, container_tag, custom_id, content, status, created_at, updated_at,
      metadata, expires_at, org_id, dreamed_at, project_id
    )
    SELECT id, container_tag, custom_id, content, status, created_at, updated_at,
      metadata, expires_at, org_id, dreamed_at, project_id
    FROM documents;
    DROP TABLE documents;
    ALTER TABLE documents_project_scoped RENAME TO documents;
    CREATE INDEX IF NOT EXISTS idx_documents_tag ON documents(container_tag);
    CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id, container_tag);
    PRAGMA foreign_keys=ON;
    """,
    """
    PRAGMA foreign_keys=OFF;
    DROP TABLE IF EXISTS usage_counters_project;
    CREATE TABLE usage_counters_project(
      key_hash TEXT NOT NULL,
      container_tag TEXT NOT NULL,
      org_id TEXT NOT NULL,
      project_id TEXT NOT NULL DEFAULT '',
      requests INTEGER NOT NULL DEFAULT 0,
      searches INTEGER NOT NULL DEFAULT 0,
      document_writes INTEGER NOT NULL DEFAULT 0,
      fact_writes INTEGER NOT NULL DEFAULT 0,
      input_chars INTEGER NOT NULL DEFAULT 0,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      PRIMARY KEY(key_hash, container_tag, org_id, project_id)
    );
    INSERT INTO usage_counters_project(
      key_hash, container_tag, org_id, project_id, requests, searches,
      document_writes, fact_writes, input_chars, created_at, updated_at
    )
    SELECT key_hash, container_tag, org_id, COALESCE(project_id, ''), requests, searches,
      document_writes, fact_writes, input_chars, created_at, updated_at
    FROM usage_counters;
    DROP TABLE usage_counters;
    ALTER TABLE usage_counters_project RENAME TO usage_counters;
    CREATE INDEX IF NOT EXISTS idx_usage_updated ON usage_counters(updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_usage_scope ON usage_counters(container_tag, org_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_counters(project_id, container_tag, updated_at DESC);
    PRAGMA foreign_keys=ON;
    """,
    """
    ALTER TABLE projects ADD COLUMN deleting INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX IF NOT EXISTS idx_projects_deleting ON projects(deleting, updated_at DESC);
    """,
    """
    UPDATE vector_points
    SET project_id = (
      SELECT d.project_id FROM chunks c JOIN documents d ON d.id = c.document_id
      WHERE CAST(c.id AS TEXT) = vector_points.id
    )
    WHERE project_id IS NULL
      AND EXISTS (
        SELECT 1 FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE CAST(c.id AS TEXT) = vector_points.id AND d.project_id IS NOT NULL
      );
    UPDATE vector_points
    SET project_id = (SELECT m.project_id FROM memories m WHERE m.id = vector_points.id)
    WHERE project_id IS NULL
      AND EXISTS (SELECT 1 FROM memories m WHERE m.id = vector_points.id AND m.project_id IS NOT NULL);
    UPDATE vector_points
    SET project_id = (SELECT f.project_id FROM facts f WHERE f.id = vector_points.id)
    WHERE project_id IS NULL
      AND EXISTS (SELECT 1 FROM facts f WHERE f.id = vector_points.id AND f.project_id IS NOT NULL);
    UPDATE vector_points
    SET org_id = (
      SELECT d.org_id FROM chunks c JOIN documents d ON d.id = c.document_id
      WHERE CAST(c.id AS TEXT) = vector_points.id
    )
    WHERE org_id IS NULL
      AND EXISTS (
        SELECT 1 FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE CAST(c.id AS TEXT) = vector_points.id AND d.org_id IS NOT NULL
      );
    UPDATE vector_points
    SET org_id = (SELECT m.org_id FROM memories m WHERE m.id = vector_points.id)
    WHERE org_id IS NULL
      AND EXISTS (SELECT 1 FROM memories m WHERE m.id = vector_points.id AND m.org_id IS NOT NULL);
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
    if current > len(_MIGRATIONS):
        raise RuntimeError(
            f"database schema version {current} is ahead of this build"
            f" ({len(_MIGRATIONS)} migrations); refusing to start against a newer schema"
        )
    for i, sql in enumerate(_MIGRATIONS, start=1):
        if i > current:
            _apply_migration(db, i, sql)
    _verify_schema(db)
    db.commit()
    return db


def _apply_migration(db: sqlite3.Connection, version: int, sql: str) -> None:
    """Apply one migration atomically and record it in the same transaction.

    ``executescript()`` issues an implicit COMMIT before it runs and auto-commits
    each statement, so a migration that failed halfway used to leave partial DDL
    behind and made every later startup attempt fail on the leftover object.

    BEGIN/COMMIT are placed inside the script text rather than around the call,
    because ``executescript`` parses the whole string at once -- that keeps trigger
    bodies containing semicolons intact, which splitting the SQL on ``;`` would not.

    ``version`` and ``applied_at`` are interpolated rather than bound because
    ``executescript`` accepts no parameters. Both are produced here, never
    caller-supplied.
    """
    now = float(time.time())
    stamp = f"INSERT INTO schema_migrations(version, applied_at) VALUES ({int(version)}, {now});"

    if "PRAGMA foreign_keys" in sql:
        # PRAGMA foreign_keys is a no-op inside a transaction, so this migration
        # cannot be wrapped. It runs unwrapped and is covered by the post-apply
        # integrity and foreign-key verification instead.
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            db.executescript(sql)
            db.executescript(stamp)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")
        return

    body = sql.strip().rstrip(";").rstrip()
    try:
        db.executescript(f"BEGIN;\n{body};\n{stamp}\nCOMMIT;")
    except Exception:
        db.rollback()
        raise


def _verify_schema(db: sqlite3.Connection) -> None:
    """Fail startup loudly rather than serving from a broken schema."""
    result = db.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"database integrity check failed after migrate: {result}")
    violations = db.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"foreign key violations after migrate: {violations}")


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
    project_id: str | None = None,
) -> dict[str, Any]:
    now = _now()
    meta = json.dumps(metadata or {})
    if custom_id is not None:
        org_clause = "org_id IS NULL" if org_id is None else "org_id = ?"
        project_clause = "project_id IS NULL" if project_id is None else "project_id = ?"
        org_params: tuple[Any, ...] = () if org_id is None else (org_id,)
        project_params: tuple[Any, ...] = () if project_id is None else (project_id,)
        row = db.execute(
            f"SELECT id FROM documents WHERE container_tag = ? AND custom_id = ? AND {org_clause}"
            f" AND {project_clause}",
            (container_tag, custom_id, *org_params, *project_params),
        ).fetchone()
        if row is not None:
            db.execute(
                "UPDATE documents SET content = ?, status = 'queued', updated_at = ?, metadata = ?, dreamed_at = NULL,"
                " expires_at = ?, org_id = ?, project_id = ? WHERE id = ?",
                (content, now, meta, expires_at, org_id, project_id, row["id"]),
            )
            db.execute("DELETE FROM chunks WHERE document_id = ?", (row["id"],))
            db.commit()
            return get_document(db, row["id"])
    doc_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO documents(id, container_tag, custom_id, content, status, created_at, updated_at, metadata,"
        " expires_at, org_id, project_id) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
        (doc_id, container_tag, custom_id, content, now, now, meta, expires_at, org_id, project_id),
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


_MEMORY_IDENTITY_KEYS = frozenset({"user_id", "agent_id", "app_id", "run_id", "actor_id"})


def clean_memory_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Remove identity/scope keys that callers cannot smuggle through metadata."""
    return {
        key: value for key, value in (metadata or {}).items() if key not in _MEMORY_IDENTITY_KEYS
    }


def _decode_memory(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    memory = dict(row)
    memory["metadata"] = json.loads(memory.get("metadata") or "{}")
    return memory


def create_memory(
    db: sqlite3.Connection,
    *,
    text: str,
    container_tag: str,
    metadata: dict[str, Any] | None = None,
    org_id: str | None = None,
    document_id: str | None = None,
    fact_id: str | None = None,
    expires_at: float | None = None,
    actor_key_hash: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    if not text.strip():
        raise ValueError("memory text must be non-empty")
    metadata = clean_memory_metadata(metadata)
    now = _now()
    memory_id = f"mem_{uuid.uuid4().hex}"
    db.execute(
        "INSERT INTO memories("
        "id, container_tag, text, metadata, org_id, document_id, fact_id, expires_at, project_id,"
        " version, state, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)",
        (
            memory_id,
            container_tag,
            text,
            json.dumps(metadata, separators=(",", ":")),
            org_id,
            document_id,
            fact_id,
            expires_at,
            project_id,
            now,
            now,
        ),
    )
    append_memory_history(
        db,
        memory_id=memory_id,
        event="ADD",
        new_text=text,
        metadata=metadata,
        version=1,
        actor_key_hash=actor_key_hash,
    )
    if project_id is not None:
        from memoratum.webhooks import record_event

        record_event(
            db,
            project_id=project_id,
            event_type="memory_add",
            memory_id=memory_id,
            data={"memory": text, "metadata": metadata},
        )
    db.commit()
    return get_memory(db, memory_id)


def ensure_fact_memory(
    conn: sqlite3.Connection,
    *,
    fact_id: str,
    text: str,
    container_tag: str,
    metadata: dict[str, Any] | None,
    org_id: str | None,
    document_id: str | None,
    expires_at: float | None,
    project_id: str | None = None,
) -> str:
    """Create the canonical memory projection for a fact without committing."""
    metadata = clean_memory_metadata(metadata)
    existing = conn.execute("SELECT id FROM memories WHERE fact_id = ?", (fact_id,)).fetchone()
    if existing is not None:
        return str(existing["id"])
    memory_id = f"mem_{uuid.uuid4().hex}"
    now = _now()
    conn.execute(
        "INSERT INTO memories("
        "id, container_tag, text, metadata, org_id, document_id, fact_id, expires_at, project_id,"
        " version, state, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)",
        (
            memory_id,
            container_tag,
            text,
            json.dumps(metadata, separators=(",", ":")),
            org_id,
            document_id,
            fact_id,
            expires_at,
            project_id,
            now,
            now,
        ),
    )
    append_memory_history(
        conn,
        memory_id=memory_id,
        event="ADD",
        new_text=text,
        metadata=metadata,
        version=1,
    )
    if project_id is not None:
        from memoratum.webhooks import record_event

        record_event(
            conn,
            project_id=project_id,
            event_type="memory_add",
            memory_id=memory_id,
            data={"memory": text, "metadata": metadata},
        )
    return memory_id


def ensure_input_memory(
    conn: sqlite3.Connection,
    *,
    text: str,
    container_tag: str,
    metadata: dict[str, Any] | None,
    org_id: str | None,
    document_id: str,
    project_id: str | None = None,
) -> str:
    """Idempotently project one raw Mem0 input message into a memory record."""
    normalized = dict(metadata or {})
    rows = conn.execute(
        "SELECT id, text FROM memories WHERE document_id = ? AND state != 'deleted'",
        (document_id,),
    ).fetchall()
    for row in rows:
        if row["text"] == text:
            return str(row["id"])
    return create_memory(
        conn,
        text=text,
        container_tag=container_tag,
        metadata=normalized,
        org_id=org_id,
        document_id=document_id,
        project_id=project_id,
    )["id"]


def get_memory(db: sqlite3.Connection, memory_id: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise KeyError(memory_id)
    return _decode_memory(row)


def resolve_memory_reference(db: sqlite3.Connection, reference: str) -> dict[str, Any] | None:
    """Resolve canonical and legacy fact references to one canonical memory.

    Early self-hosted builds returned ``mem_<fact_id>`` from search and raw fact
    IDs from list responses. The migration backfills those projections, but the
    HTTP compatibility layer still accepts both reference forms during the
    transition window.
    """
    try:
        return get_memory(db, reference)
    except KeyError:
        pass
    candidates = [reference]
    if reference.startswith("mem_"):
        candidates.append(reference[4:])
    for candidate in candidates:
        row = db.execute(
            "SELECT * FROM memories WHERE fact_id = ? ORDER BY created_at, id LIMIT 1",
            (candidate,),
        ).fetchone()
        if row is not None:
            return _decode_memory(row)
    return None


_UNSET = object()


def update_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    text: str | None = None,
    metadata: dict[str, Any] | None = None,
    timestamp: float | None = None,
    expires_at: Any = _UNSET,
    actor_key_hash: str | None = None,
) -> dict[str, Any]:
    """Update one active memory and append an UPDATE event without committing."""
    current = get_memory(conn, memory_id)
    if current["state"] == "deleted":
        raise KeyError(memory_id)
    new_text = current["text"] if text is None else text
    if not isinstance(new_text, str) or not new_text.strip():
        raise ValueError("memory text must be non-empty")
    new_metadata = clean_memory_metadata({**current["metadata"], **(metadata or {})})
    new_expires = current.get("expires_at") if expires_at is _UNSET else expires_at
    event_at = current.get("event_at") if timestamp is None else timestamp
    version = int(current["version"]) + 1
    conn.execute(
        "UPDATE memories SET text = ?, metadata = ?, expires_at = ?, event_at = ?,"
        " version = ?, updated_at = ? WHERE id = ?",
        (
            new_text,
            json.dumps(new_metadata, separators=(",", ":")),
            new_expires,
            event_at,
            version,
            _now(),
            memory_id,
        ),
    )
    append_memory_history(
        conn,
        memory_id=memory_id,
        event="UPDATE",
        version=version,
        old_text=current["text"],
        new_text=new_text,
        metadata=new_metadata,
        actor_key_hash=actor_key_hash,
    )
    if current.get("project_id") is not None:
        from memoratum.webhooks import record_event

        record_event(
            conn,
            project_id=str(current["project_id"]),
            event_type="memory_update",
            memory_id=memory_id,
            data={"memory": new_text, "metadata": new_metadata},
        )
    return get_memory(conn, memory_id)


def categorize_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    category: str,
    actor_key_hash: str | None = None,
) -> dict[str, Any]:
    """Set a memory category and emit only the categorization event."""
    if not isinstance(category, str) or not category.strip() or len(category.strip()) > 200:
        raise ValueError("category must be a non-empty string of at most 200 characters")
    current = get_memory(conn, memory_id)
    if current["state"] == "deleted":
        raise KeyError(memory_id)
    category = category.strip()
    metadata = clean_memory_metadata({**current["metadata"], "category": category})
    version = int(current["version"]) + 1
    conn.execute(
        "UPDATE memories SET metadata = ?, version = ?, updated_at = ? WHERE id = ?",
        (json.dumps(metadata, separators=(",", ":")), version, _now(), memory_id),
    )
    append_memory_history(
        conn,
        memory_id=memory_id,
        event="UPDATE",
        version=version,
        old_text=current["text"],
        new_text=current["text"],
        metadata=metadata,
        actor_key_hash=actor_key_hash,
    )
    if current.get("project_id") is not None:
        from memoratum.webhooks import record_event

        record_event(
            conn,
            project_id=str(current["project_id"]),
            event_type="memory_categorize",
            memory_id=memory_id,
            data={"category": category},
        )
    return get_memory(conn, memory_id)


def linked_memory_ids(conn: sqlite3.Connection, memory_id: str) -> list[str]:
    """Return older canonical memories superseded by the given fact, transitively."""
    memory = get_memory(conn, memory_id)
    fact_id = memory.get("fact_id")
    if not fact_id:
        return []
    seen: set[str] = set()
    frontier = [fact_id]
    while frontier:
        current = frontier.pop()
        rows = conn.execute(
            "SELECT id FROM facts WHERE superseded_by = ? ORDER BY created_at DESC",
            (current,),
        ).fetchall()
        for row in rows:
            older_fact = str(row["id"])
            linked = conn.execute(
                "SELECT id FROM memories WHERE fact_id = ? AND state != 'deleted'",
                (older_fact,),
            ).fetchone()
            if linked is not None and str(linked["id"]) not in seen:
                seen.add(str(linked["id"]))
                frontier.append(older_fact)
    return sorted(seen)


def soft_delete_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    actor_key_hash: str | None = None,
    delete_linked: bool = False,
) -> dict[str, Any]:
    """Mark a memory deleted and append a redacted tombstone without committing."""
    memory = get_memory(conn, memory_id)
    if memory["state"] == "deleted":
        return {"memory_id": memory_id, "deleted": False, "cascade_count": 0}
    targets = [memory_id]
    if delete_linked:
        targets.extend(linked_memory_ids(conn, memory_id))
    now = _now()
    for target in targets:
        row = get_memory(conn, target)
        version = int(row["version"]) + 1
        conn.execute(
            "UPDATE memories SET state = 'deleted', version = ?, updated_at = ? WHERE id = ?",
            (version, now, target),
        )
        append_memory_history(
            conn,
            memory_id=target,
            event="DELETE",
            version=version,
            old_text=None,
            new_text=None,
            metadata={},
            actor_key_hash=actor_key_hash,
            content_hash=hashlib.sha256(row["text"].encode()).hexdigest(),
        )
        if row.get("project_id") is not None:
            from memoratum.webhooks import record_event

            record_event(
                conn,
                project_id=str(row["project_id"]),
                event_type="memory_delete",
                memory_id=target,
                data={"deleted": True},
            )
    return {
        "memory_id": memory_id,
        "deleted": True,
        "cascade_count": len(targets) - 1,
        "deleted_ids": targets,
    }


def list_all_memories(
    conn: sqlite3.Connection,
    *,
    org_id: str | None = None,
    project_id: str | None = None,
    include_deleted: bool = False,
    show_expired: bool = False,
) -> list[dict[str, Any]]:
    """List canonical memories across tags for an authorized administrative filter."""
    where: list[str] = []
    params: list[Any] = []
    if org_id is not None:
        where.append("org_id = ?")
        params.append(org_id)
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    if not include_deleted:
        where.append("state != 'deleted'")
    if not show_expired:
        where.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(_now())
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT * FROM memories{clause} ORDER BY created_at, id", params
    ).fetchall()
    return [_decode_memory(row) for row in rows]


def list_memories(
    db: sqlite3.Connection,
    container_tag: str,
    *,
    org_id: str | None = None,
    project_id: str | None = None,
    include_deleted: bool = False,
    show_expired: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where = ["container_tag = ?"]
    params: list[Any] = [container_tag]
    if org_id is not None:
        where.append("org_id = ?")
        params.append(org_id)
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    if not include_deleted:
        where.append("state != 'deleted'")
    if not show_expired:
        where.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(_now())
    query = f"SELECT * FROM memories WHERE {' AND '.join(where)} ORDER BY created_at, id"
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend((max(0, limit), max(0, offset)))
    return [_decode_memory(row) for row in db.execute(query, params).fetchall()]


def append_memory_history(
    db: sqlite3.Connection,
    *,
    memory_id: str,
    event: str,
    version: int,
    old_text: str | None = None,
    new_text: str | None = None,
    metadata: dict[str, Any] | None = None,
    actor_key_hash: str | None = None,
    content_hash: str | None = None,
) -> str:
    if event not in {"ADD", "UPDATE", "DELETE"}:
        raise ValueError(f"unknown memory history event: {event}")
    if content_hash is None:
        source_text = new_text if new_text is not None else old_text
        content_hash = (
            hashlib.sha256(source_text.encode()).hexdigest() if source_text is not None else None
        )
    history_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO memory_history("
        "id, memory_id, event, old_text, new_text, metadata, version, actor_key_hash, created_at,"
        " content_hash, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            history_id,
            memory_id,
            event,
            old_text,
            new_text,
            json.dumps(metadata or {}, separators=(",", ":")),
            version,
            actor_key_hash,
            _now(),
            content_hash,
            _now(),
        ),
    )
    return history_id


def list_memory_history(
    db: sqlite3.Connection,
    memory_id: str,
    *,
    container_tag: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where = ["memory_id = ?"]
    params: list[Any] = [memory_id]
    if container_tag is not None:
        where.append("memory_id IN (SELECT id FROM memories WHERE container_tag = ?)")
        params.append(container_tag)
    query = (
        "SELECT id, memory_id, event, old_text, new_text, metadata, version,"
        " actor_key_hash, created_at, content_hash, updated_at FROM memory_history"
        f" WHERE {' AND '.join(where)} ORDER BY created_at, id"
    )
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend((max(0, limit), max(0, offset)))
    rows = db.execute(query, params).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item.get("metadata") or "{}")
        out.append(item)
    return out


def canonical_request_hash(value: Any) -> str:
    """Hash a JSON-compatible request deterministically for idempotency claims."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class IdempotencyClaim:
    status: str
    request_hash: str
    response: Any = None
    job_id: str | None = None
    scope: str = ""
    key: str = ""


def claim_idempotency(
    conn: sqlite3.Connection,
    *,
    scope: str,
    key: str,
    request_hash: str,
    expires_in: int = 86_400,
) -> IdempotencyClaim:
    """Atomically claim a scoped key, replaying a completed response if present."""
    now = _now()
    conn.execute("DELETE FROM idempotency_keys WHERE expires_at <= ?", (now,))
    try:
        conn.execute(
            "INSERT INTO idempotency_keys(scope, key, request_hash, status, created_at, expires_at)"
            " VALUES (?, ?, ?, 'in_flight', ?, ?)",
            (scope, key, request_hash, now, now + max(60, expires_in)),
        )
        conn.commit()
        return IdempotencyClaim(status="claimed", request_hash=request_hash, scope=scope, key=key)
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT request_hash, status, response, job_id FROM idempotency_keys"
            " WHERE scope = ? AND key = ?",
            (scope, key),
        ).fetchone()
        if row is None:
            raise
        if row["request_hash"] != request_hash:
            conn.commit()
            return IdempotencyClaim(status="conflict", request_hash=row["request_hash"])
        response = json.loads(row["response"]) if row["response"] else None
        conn.commit()
        return IdempotencyClaim(
            status="replay" if row["status"] == "done" else row["status"],
            request_hash=row["request_hash"],
            response=response,
            job_id=row["job_id"],
            scope=scope,
            key=key,
        )


def complete_idempotency(
    conn: sqlite3.Connection,
    *,
    scope: str,
    key: str,
    request_hash: str,
    response: Any,
    job_id: str | None = None,
) -> None:
    changed = conn.execute(
        "UPDATE idempotency_keys SET status = 'done', response = ?, job_id = ?"
        " WHERE scope = ? AND key = ? AND request_hash = ?",
        (json.dumps(response, separators=(",", ":")), job_id, scope, key, request_hash),
    ).rowcount
    if changed != 1:
        raise RuntimeError("idempotency claim is missing or does not match")
    conn.commit()


def release_idempotency(conn: sqlite3.Connection, *, scope: str, key: str) -> None:
    conn.execute("DELETE FROM idempotency_keys WHERE scope = ? AND key = ?", (scope, key))
    conn.commit()


def get_idempotency(conn: sqlite3.Connection, *, scope: str, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT scope, key, request_hash, status, response, job_id, created_at, expires_at"
        " FROM idempotency_keys WHERE scope = ? AND key = ?",
        (scope, key),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["response"] = json.loads(result["response"]) if result["response"] else None
    return result


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
    project_id: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    match = fts_query(query)
    if match is None:
        return []
    joins = " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
    conditions = ["chunks_fts MATCH ?"]
    params: list[Any] = [match]
    if container_tag is not None or org_id is not None or project_id is not None:
        joins += " JOIN documents d ON d.id = c.document_id"
    if container_tag is not None:
        conditions.append("d.container_tag = ?")
        params.append(container_tag)
    if org_id is not None:
        conditions.append("d.org_id = ?")
        params.append(org_id)
    if project_id is not None:
        conditions.append("d.project_id = ?")
        params.append(project_id)
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
    db: sqlite3.Connection,
    *,
    container_tag: str | None = None,
    org_id: str | None = None,
    project_id: str | None = None,
    role: str = "READER",
) -> str:
    if role not in {"OWNER", "READER"}:
        raise ValueError("role must be OWNER or READER")
    raw = "mm_" + secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO api_keys(key_hash, container_tag, created_at, org_id, project_id, role)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (_hash_key(raw), container_tag, _now(), org_id, project_id, role),
    )
    db.commit()
    return raw


def lookup_key(db: sqlite3.Connection, raw: str) -> dict[str, Any] | None:
    """Return the key row (container_tag/org_id None = wildcard/default), None if unknown/revoked."""
    h = _hash_key(raw)
    if db.execute("SELECT 1 FROM revoked_keys WHERE key_hash = ?", (h,)).fetchone() is not None:
        return None
    row = db.execute(
        "SELECT key_hash, container_tag, org_id, project_id, role FROM api_keys WHERE key_hash = ?",
        (h,),
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
    """Expire canonical memories and hard-delete expired source rows/vectors."""
    now = time.time()
    expired_memories = conn.execute(
        "SELECT id FROM memories WHERE state != 'deleted'"
        " AND expires_at IS NOT NULL AND expires_at <= ?",
        (now,),
    ).fetchall()
    memory_ids: list[str] = []
    for row in expired_memories:
        result = soft_delete_memory(conn, str(row["id"]))
        if result["deleted"]:
            memory_ids.append(str(row["id"]))
    if memory_ids:
        placeholders = ",".join("?" for _ in memory_ids)
        conn.execute(f"DELETE FROM vector_points WHERE id IN ({placeholders})", memory_ids)
    conn.execute(
        "DELETE FROM vector_points WHERE id IN ("
        "SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id"
        " WHERE d.expires_at IS NOT NULL AND d.expires_at <= ?"
        ")",
        (now,),
    )
    facts = conn.execute(
        "DELETE FROM facts WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
    ).rowcount
    docs = conn.execute(
        "DELETE FROM documents WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
    ).rowcount
    conn.commit()
    return {"memories": len(memory_ids), "facts": facts, "documents": docs}


def purge_project(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    org_id: str | None = None,
    preserve_job_id: str | None = None,
) -> dict[str, int]:
    """Purge all project-owned data and the project row in one transaction."""
    project = conn.execute(
        "SELECT org_id, deleting FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if project is None:
        return {"memories": 0, "facts": 0, "documents": 0, "keys": 0, "webhooks": 0}
    if org_id is not None and project["org_id"] != org_id:
        raise ValueError("project does not belong to org_id")
    key_rows = conn.execute(
        "SELECT key_hash FROM api_keys WHERE project_id = ?", (project_id,)
    ).fetchall()
    for key_row in key_rows:
        conn.execute(
            "DELETE FROM idempotency_keys WHERE scope = ?", (f"key:{key_row['key_hash']}",)
        )
        conn.execute("DELETE FROM revoked_keys WHERE key_hash = ?", (key_row["key_hash"],))
    memories = conn.execute("DELETE FROM memories WHERE project_id = ?", (project_id,)).rowcount
    facts = conn.execute("DELETE FROM facts WHERE project_id = ?", (project_id,)).rowcount
    conn.execute("DELETE FROM vector_points WHERE project_id = ?", (project_id,))
    documents = conn.execute("DELETE FROM documents WHERE project_id = ?", (project_id,)).rowcount
    keys = len(key_rows)
    webhooks = conn.execute("DELETE FROM webhooks WHERE project_id = ?", (project_id,)).rowcount
    conn.execute("DELETE FROM usage_counters WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM audit_events WHERE project_id = ?", (project_id,))
    job_rows = conn.execute("SELECT id, project_id, payload FROM jobs").fetchall()
    for job_row in job_rows:
        if job_row["id"] == preserve_job_id:
            continue
        payload_project = None
        try:
            payload_project = (json.loads(job_row["payload"] or "{}") or {}).get("project_id")
        except (TypeError, ValueError):
            payload_project = None
        if job_row["project_id"] == project_id or payload_project == project_id:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_row["id"],))
    conn.execute("DELETE FROM project_members WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    return {
        "memories": memories,
        "facts": facts,
        "documents": documents,
        "keys": keys,
        "webhooks": webhooks,
    }


def purge_tag(
    db: sqlite3.Connection,
    container_tag: str,
    *,
    org_id: str | None = None,
    project_id: str | None = None,
    legacy_only: bool = False,
) -> dict[str, int]:
    """Delete tag data within an explicit scope.

    ``legacy_only`` is used for historical container-tag keys. It restricts the
    operation to rows that have no organization or project so a legacy key can
    never erase a tenant-scoped record.
    """
    where = "container_tag = ?"
    params: list[Any] = [container_tag]
    if org_id is not None:
        where += " AND org_id = ?"
        params.append(org_id)
    if project_id is not None:
        where += " AND project_id = ?"
        params.append(project_id)
    if legacy_only:
        where += " AND org_id IS NULL AND project_id IS NULL"
    memory_ids = [
        str(row["id"])
        for row in db.execute(f"SELECT id FROM memories WHERE {where}", params).fetchall()
    ]
    if memory_ids:
        placeholders = ",".join("?" for _ in memory_ids)
        db.execute(
            f"DELETE FROM webhook_deliveries WHERE memory_id IN ({placeholders})", memory_ids
        )
        db.execute(f"DELETE FROM domain_events WHERE memory_id IN ({placeholders})", memory_ids)
    memories = db.execute(f"DELETE FROM memories WHERE {where}", params).rowcount
    facts = db.execute(f"DELETE FROM facts WHERE {where}", params).rowcount
    db.execute(f"DELETE FROM vector_points WHERE {where}", params)
    docs = db.execute(f"DELETE FROM documents WHERE {where}", params).rowcount
    db.commit()
    return {"memories": memories, "facts": facts, "documents": docs, "keys": 0}


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
    project_id: str | None = None,
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
    project = _scope_value(project_id)
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
          key_hash, container_tag, org_id, project_id, requests, searches,
          document_writes, fact_writes, input_chars, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key_hash, container_tag, org_id, project_id) DO UPDATE SET
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
            project,
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
    project_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, params = _scope_filter(container_tag=container_tag, org_id=org_id)
    if project_id is not None:
        where = f"{where} AND project_id = ?" if where else "project_id = ?"
        params.append(project_id)
    clause = f" WHERE {where}" if where else ""
    rows = db.execute(
        "SELECT key_hash, container_tag, org_id, project_id, requests, searches, document_writes,"
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
        item["project_id"] = item["project_id"] or None
        out.append(item)
    return out


def count_usage(
    db: sqlite3.Connection,
    *,
    container_tag: str | None = None,
    org_id: str | None = None,
    project_id: str | None = None,
) -> int:
    where, params = _scope_filter(container_tag=container_tag, org_id=org_id)
    if project_id is not None:
        where = f"{where} AND project_id = ?" if where else "project_id = ?"
        params.append(project_id)
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
    project_id: str | None = None,
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
          id, created_at, actor_kind, actor_key_hash, container_tag, org_id, project_id,
          action, resource_type, resource_id, outcome, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            _now(),
            actor_kind,
            # Hash again at the persistence boundary in case a caller passes a raw value.
            hashlib.sha256(actor_key_hash.encode()).hexdigest()[:16] if actor_key_hash else None,
            container_tag,
            org_id,
            project_id,
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
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, params = _scope_filter(container_tag=container_tag, org_id=org_id)
    if project_id is not None:
        where = f"{where} AND project_id = ?" if where else "project_id = ?"
        params.append(project_id)
    clause = f" WHERE {where}" if where else ""
    rows = db.execute(
        "SELECT id, created_at, actor_kind, actor_key_hash, container_tag, org_id, project_id, action,"
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
    db: sqlite3.Connection,
    *,
    container_tag: str | None = None,
    org_id: str | None = None,
    project_id: str | None = None,
) -> int:
    where, params = _scope_filter(container_tag=container_tag, org_id=org_id)
    if project_id is not None:
        where = f"{where} AND project_id = ?" if where else "project_id = ?"
        params.append(project_id)
    clause = f" WHERE {where}" if where else ""
    return int(
        db.execute(f"SELECT COUNT(*) AS n FROM audit_events{clause}", params).fetchone()["n"]
    )
