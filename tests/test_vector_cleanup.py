"""Vector index lifecycle cleanup contract (RED)."""

import os
import tempfile


def _conn():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "cleanup.db"))


def _index_chunk(conn, doc_id: str, chunk_id: int) -> None:
    from memoratum.vectorstore import SQLiteVectorStore, VectorRecord

    doc = conn.execute(
        "SELECT container_tag, org_id FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    SQLiteVectorStore(conn).upsert(
        [
            VectorRecord(
                id=str(chunk_id),
                vector=[1.0, 0.0],
                text="content",
                kind="chunk",
                container_tag=doc["container_tag"],
                org_id=doc["org_id"],
            )
        ]
    )


def test_purge_tag_removes_local_vector_points() -> None:
    from memoratum import db

    conn = _conn()
    doc = db.create_document(conn, container_tag="u1", content="hello")
    chunk_id = db.add_chunks(conn, doc["id"], ["hello"])[0]
    _index_chunk(conn, doc["id"], chunk_id)
    db.purge_tag(conn, "u1")
    assert conn.execute("SELECT COUNT(*) AS n FROM vector_points").fetchone()["n"] == 0
    conn.close()


def test_prune_expired_removes_local_vector_points() -> None:
    from memoratum import db

    conn = _conn()
    doc = db.create_document(conn, container_tag="u1", content="hello", expires_at=1)
    chunk_id = db.add_chunks(conn, doc["id"], ["hello"])[0]
    _index_chunk(conn, doc["id"], chunk_id)
    db.prune_expired(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM vector_points").fetchone()["n"] == 0
    conn.close()
