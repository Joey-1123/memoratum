# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ingest worker: queued → chunking → embedding → indexing → done|failed."""

from __future__ import annotations

import sqlite3
import struct
from typing import Any

from memoratum import db
from memoratum.chunking import split_markdown
from memoratum.embeddings import Embedder


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def process_one(conn: sqlite3.Connection, embedder: Embedder) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id FROM documents WHERE status = 'queued' ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    doc_id = row["id"]
    try:
        doc = db.get_document(conn, doc_id)
        chunks = split_markdown(doc["content"])
        texts = [c["text"] for c in chunks] or [doc["content"]]
        vecs = embedder.embed(texts)
        db.add_chunks(conn, doc_id, texts, [_pack(v) for v in vecs])
        db.set_status(conn, doc_id, "done")
    except Exception:  # noqa: BLE001 — worker must record failure, never crash the loop
        db.set_status(conn, doc_id, "failed")
    return db.get_document(conn, doc_id)


def process_all(conn: sqlite3.Connection, embedder: Embedder) -> int:
    n = 0
    while process_one(conn, embedder) is not None:
        n += 1
    return n
