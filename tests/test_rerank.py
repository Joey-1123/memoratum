"""Rerank flag contract (RED)."""

import os
import tempfile
import time


def test_rerank_breaks_ties_toward_fresh_hits() -> None:
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_all
    from memoratum.search import search

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
    e = HashEmbedder(dims=16)
    db.create_document(conn, container_tag="u1", content="deploy checklist alpha")
    process_all(conn, e)
    time.sleep(0.05)
    db.create_document(conn, container_tag="u1", content="deploy checklist alpha")
    process_all(conn, e)
    # age the first chunk by two days so recency is decisive, not noise
    conn.execute("UPDATE chunks SET created_at = created_at - 172800 WHERE id = 1")
    conn.commit()
    plain = search(conn, e, "deploy checklist", container_tag="u1", limit=2)
    ranked = search(conn, e, "deploy checklist", container_tag="u1", limit=2, rerank=True)
    assert len(plain) == len(ranked) == 2
    assert ranked[0]["id"] == "chunk_2"
    conn.close()
