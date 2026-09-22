"""Relevance rerank in search contract (RED)."""

import os
import tempfile


def _setup():
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_all

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
    e = HashEmbedder(dims=16)
    db.create_document(
        conn, container_tag="u1", content="quantum physics black holes event horizons"
    )
    db.create_document(conn, container_tag="u1", content="the cat sat on the mat today")
    process_all(conn, e)
    return conn, e


def test_rerank_orders_by_relevance() -> None:
    from memoratum.rerank import HeuristicReranker
    from memoratum.search import search

    conn, e = _setup()
    hits = search(
        conn, e, "cat mat", container_tag="u1", limit=2, rerank=True, reranker=HeuristicReranker()
    )
    assert len(hits) == 2
    assert "cat" in hits[0]["chunk"]
    conn.close()


def test_rerank_defaults_to_heuristic() -> None:
    from memoratum.search import search

    conn, e = _setup()
    hits = search(conn, e, "cat mat", container_tag="u1", limit=2, rerank=True)
    assert "cat" in hits[0]["chunk"]
    conn.close()
