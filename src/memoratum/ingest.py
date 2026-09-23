# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ingest worker: queued → chunking → embedding → indexing → done|failed."""

from __future__ import annotations

import sqlite3
import struct
import time
from typing import Any

from memoratum import db
from memoratum.chunking import split_markdown
from memoratum.embeddings import Embedder
from memoratum.vectorstore import VectorRecord, VectorStore


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def process_document(
    conn: sqlite3.Connection,
    embedder: Embedder,
    doc_id: str,
    *,
    vector_store: VectorStore | None = None,
) -> dict[str, Any]:
    try:
        doc = db.get_document(conn, doc_id)
        chunks = split_markdown(doc["content"])
        texts = [c["text"] for c in chunks] or [doc["content"]]
        vecs = embedder.embed(texts)
        for table in ("chunks", "facts"):
            prior = conn.execute(f"SELECT embedding FROM {table} LIMIT 1").fetchone()
            if (
                prior is not None
                and prior["embedding"] is not None
                and len(prior["embedding"]) != len(vecs[0]) * 4
            ):
                raise RuntimeError(
                    "embedding dimension change detected; re-embed from a fresh database"
                )
        chunk_ids = db.add_chunks(conn, doc_id, texts, [_pack(v) for v in vecs])
        if vector_store is not None:
            metadata = dict(doc.get("metadata") or {})
            metadata["document_id"] = doc_id
            vector_store.upsert(
                [
                    VectorRecord(
                        id=str(chunk_id),
                        vector=vector,
                        text=text,
                        kind="chunk",
                        container_tag=doc["container_tag"],
                        org_id=doc.get("org_id"),
                        metadata=metadata,
                        created_at=time.time(),
                    )
                    for chunk_id, text, vector in zip(chunk_ids, texts, vecs, strict=True)
                ]
            )
        db.set_status(conn, doc_id, "done")
    except Exception:
        db.set_status(conn, doc_id, "failed")
        raise
    return db.get_document(conn, doc_id)


def process_one(
    conn: sqlite3.Connection, embedder: Embedder, *, vector_store: VectorStore | None = None
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id FROM documents WHERE status = 'queued' ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        return process_document(conn, embedder, row["id"], vector_store=vector_store)
    except Exception:  # noqa: BLE001 — callers of process_one get status, not exceptions
        return db.get_document(conn, row["id"])


def process_all(
    conn: sqlite3.Connection, embedder: Embedder, *, vector_store: VectorStore | None = None
) -> int:
    n = 0
    while process_one(conn, embedder, vector_store=vector_store) is not None:
        n += 1
    return n
