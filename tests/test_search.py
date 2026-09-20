"""Hybrid search contract (RED)."""

import os
import tempfile


def _setup():
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_all

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
    e = HashEmbedder(dims=16)
    db.create_document(conn, container_tag="u1", content="The cat sat on the mat.")
    db.create_document(conn, container_tag="u1", content="Quantum physics and black holes.")
    db.create_document(conn, container_tag="u2", content="The cat sat on the mat.")
    process_all(conn, e)
    return conn, e


def test_hybrid_finds_relevant_chunk() -> None:
    from memoratum.search import search

    conn, e = _setup()
    hits = search(conn, e, "cat mat", container_tag="u1", limit=5)
    assert len(hits) >= 1
    assert "cat" in hits[0]["chunk"]
    assert set(hits[0]) >= {"id", "chunk", "similarity"}
    conn.close()


def test_container_tag_isolation_is_mandatory() -> None:
    from memoratum.search import search

    conn, e = _setup()
    hits = search(conn, e, "quantum", container_tag="u2", limit=5)
    assert all("quantum" not in h["chunk"] for h in hits)
    conn.close()


def test_threshold_filters_weak_hits() -> None:
    from memoratum.search import search

    conn, e = _setup()
    loose = search(conn, e, "cat mat", container_tag="u1", limit=10, threshold=0.0)
    strict = search(conn, e, "cat mat", container_tag="u1", limit=10, threshold=0.99)
    assert len(strict) <= len(loose)
    conn.close()
