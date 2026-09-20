"""Dream scoping contract (RED)."""

import os
import tempfile

from test_dreaming import _llm


def _db():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_dream_pending_scopes_to_tag() -> None:
    from memoratum import db
    from memoratum.dreaming import dream_pending

    conn = _db()
    db.create_document(conn, container_tag="u1", content="Doc one.")
    db.create_document(conn, container_tag="u2", content="Doc two.")
    server, llm = _llm()
    try:
        dream_pending(conn, llm, mode="dynamic", container_tag="u1")
        dreamed_u1 = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE container_tag='u1' AND dreamed_at IS NOT NULL"
        ).fetchone()[0]
        dreamed_u2 = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE container_tag='u2' AND dreamed_at IS NOT NULL"
        ).fetchone()[0]
        assert dreamed_u1 >= 1
        assert dreamed_u2 == 0
    finally:
        server.shutdown()
        conn.close()


def test_upsert_clears_dreamed_at() -> None:
    from memoratum import db

    conn = _db()
    d = db.create_document(conn, container_tag="u1", content="v1", custom_id="s1")
    conn.execute("UPDATE documents SET dreamed_at = 1.0 WHERE id = ?", (d["id"],))
    conn.commit()
    d2 = db.create_document(conn, container_tag="u1", content="v2", custom_id="s1")
    assert d2["id"] == d["id"]
    assert db.get_document(conn, d["id"])["dreamed_at"] is None
    conn.close()


def test_dynamic_windows_cover_oversized_docs() -> None:
    from memoratum import db
    from memoratum.dreaming import dream_pending
    from memoratum.facts import list_facts

    conn = _db()
    for i in range(3):
        db.create_document(conn, container_tag="u1", content=("word " * 2000) + str(i))
    server, llm = _llm()
    try:
        calls = dream_pending(conn, llm, mode="dynamic")
        assert calls > 1
        remaining = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE dreamed_at IS NULL"
        ).fetchone()[0]
        assert remaining == 0
        assert len(list_facts(conn, "u1")) >= 1
    finally:
        server.shutdown()
        conn.close()
