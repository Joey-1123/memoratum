"""Ingest worker contract (RED)."""

import os
import struct
import tempfile


def _db():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_process_one_indexes_chunks_with_embeddings() -> None:
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_one

    conn = _db()
    doc = db.create_document(conn, container_tag="u1", content="# T\n\nHello world here.\n")
    out = process_one(conn, HashEmbedder(dims=8))
    assert out is not None and out["id"] == doc["id"]
    assert db.get_document(conn, doc["id"])["status"] == "done"
    rows = conn.execute(
        "SELECT text, embedding FROM chunks WHERE document_id = ?", (doc["id"],)
    ).fetchall()
    assert len(rows) >= 1
    assert len(struct.unpack(f"{8}f", rows[0]["embedding"])) == 8
    assert process_one(conn, HashEmbedder(dims=8)) is None
    conn.close()


def test_failed_embedding_marks_failed() -> None:
    from memoratum import db
    from memoratum.ingest import process_one

    class Boom:
        dims = 8

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("nope")

    conn = _db()
    doc = db.create_document(conn, container_tag="u1", content="x")
    process_one(conn, Boom())
    assert db.get_document(conn, doc["id"])["status"] == "failed"
    conn.close()
