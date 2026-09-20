"""Metadata + filters contract (RED)."""

import os
import tempfile


def _db():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_document_metadata_roundtrip_and_filter() -> None:
    from memoratum import db

    conn = _db()
    d1 = db.create_document(conn, container_tag="u1", content="alpha", metadata={"type": "meeting"})
    db.create_document(conn, container_tag="u1", content="beta", metadata={"type": "note"})
    assert db.get_document(conn, d1["id"])["metadata"] == {"type": "meeting"}
    rows = conn.execute("SELECT id FROM documents WHERE container_tag = 'u1'").fetchall()
    assert len(rows) == 2
    conn.close()


def test_search_filters_by_metadata() -> None:
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_all
    from memoratum.search import search

    conn = _db()
    e = HashEmbedder(dims=16)
    db.create_document(
        conn,
        container_tag="u1",
        content="alpha project meeting notes",
        metadata={"type": "meeting"},
    )
    db.create_document(
        conn, container_tag="u1", content="alpha project note text", metadata={"type": "note"}
    )
    process_all(conn, e)
    hits = search(
        conn, e, "alpha project", container_tag="u1", limit=10, filters={"type": "meeting"}
    )
    assert hits
    assert all("meeting" in h["chunk"] for h in hits)
    conn.close()


def test_search_filters_on_facts() -> None:
    from memoratum.embeddings import HashEmbedder
    from memoratum.facts import add_fact
    from memoratum.search import search

    conn = _db()
    e = HashEmbedder(dims=16)
    add_fact(
        conn,
        container_tag="u1",
        subject="user",
        predicate="loves",
        object="Paris",
        document_id=None,
        metadata={"source": "chat"},
    )
    add_fact(
        conn,
        container_tag="u1",
        subject="user",
        predicate="hates",
        object="Mondays",
        document_id=None,
        metadata={"source": "email"},
    )
    hits = search(
        conn,
        e,
        "user loves hates Paris Mondays",
        container_tag="u1",
        search_mode="memories",
        filters={"source": "chat"},
    )
    assert [h["memory"] for h in hits] == ["user loves Paris"]
    conn.close()
