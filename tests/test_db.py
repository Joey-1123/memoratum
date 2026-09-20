"""DB layer contract (RED)."""

import os
import tempfile


def _open():
    from memoratum.db import connect

    d = tempfile.mkdtemp()
    return connect(os.path.join(d, "t.db"))


def test_wal_foreign_keys_and_migrations() -> None:
    db = _open()
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 1
    db.close()


def test_custom_id_upsert_is_idempotent() -> None:
    from memoratum.db import create_document, get_document

    db = _open()
    a = create_document(db, container_tag="u1", content="hello", custom_id="s1")
    assert a["status"] == "queued"
    b = create_document(db, container_tag="u1", content="hello again", custom_id="s1")
    assert b["id"] == a["id"]
    assert get_document(db, a["id"])["content"] == "hello again"
    n = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert n == 1
    db.close()


def test_chunks_and_fts_roundtrip() -> None:
    from memoratum.db import add_chunks, create_document, keyword_search

    db = _open()
    doc = create_document(db, container_tag="u1", content="x")
    add_chunks(db, doc["id"], ["the quick brown fox", "lazy dog sleeps"])
    hits = keyword_search(db, "fox", limit=5)
    assert len(hits) == 1
    assert hits[0]["text"] == "the quick brown fox"
    db.close()


def test_fts_special_chars_do_not_error() -> None:
    from memoratum.db import add_chunks, create_document, keyword_search

    db = _open()
    doc = create_document(db, container_tag="u1", content="x")
    add_chunks(db, doc["id"], ["where does the user want to travel"])
    assert keyword_search(db, "where does the user want to travel?", limit=5)
    db.close()


def test_concurrent_writers_with_own_connections() -> None:
    import threading

    from memoratum import db

    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db.connect(path).close()
    errors: list = []
    barrier = threading.Barrier(5)

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=30)
            conn = db.connect(path)
            try:
                db.create_document(conn, container_tag="u1", content=f"w{i}", custom_id=f"w{i}")
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    conn = db.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 5
    finally:
        conn.close()


def test_scoped_keys() -> None:
    from memoratum.db import create_api_key, lookup_key

    db = _open()
    raw = create_api_key(db, container_tag="proj-a")
    assert raw.startswith("mm_")
    row = lookup_key(db, raw)
    assert row is not None and row["container_tag"] == "proj-a"
    assert lookup_key(db, "mm_bogus") is None
    wild = create_api_key(db, container_tag=None)
    wrow = lookup_key(db, wild)
    assert wrow is not None and wrow["container_tag"] is None
    db.close()
