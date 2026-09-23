"""Expiration/TTL contract (RED)."""

import os
import tempfile
import time


def _db():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_expired_documents_hidden_from_search() -> None:
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_all
    from memoratum.search import search

    conn = _db()
    e = HashEmbedder(dims=16)
    past = time.time() - 10
    future = time.time() + 3600
    db.create_document(conn, container_tag="u1", content="old news alpha", expires_at=past)
    db.create_document(conn, container_tag="u1", content="fresh news alpha", expires_at=future)
    db.create_document(conn, container_tag="u1", content="timeless alpha lore")
    process_all(conn, e)
    hits = search(conn, e, "alpha news", container_tag="u1", limit=10)
    texts = [h["chunk"] for h in hits]
    assert any("fresh" in t for t in texts)
    assert any("timeless" in t for t in texts)
    assert not any("old news" in t for t in texts)
    conn.close()


def test_expired_facts_hidden() -> None:
    import time as _t

    from memoratum.facts import add_fact, list_facts

    conn = _db()
    add_fact(
        conn,
        container_tag="u1",
        subject="s",
        predicate="p",
        object="old",
        document_id=None,
        expires_at=_t.time() - 5,
    )
    add_fact(conn, container_tag="u1", subject="s", predicate="p", object="new", document_id=None)
    current = [f["object"] for f in list_facts(conn, "u1")]
    assert current == ["new"]
    conn.close()


def test_prune_removes_expired_rows() -> None:
    import time as _t

    from memoratum import db

    conn = _db()
    db.create_document(conn, container_tag="u1", content="gone", expires_at=_t.time() - 5)
    db.create_document(conn, container_tag="u1", content="stays")
    counts = db.prune_expired(conn)
    assert counts["documents"] == 1
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    conn.close()
