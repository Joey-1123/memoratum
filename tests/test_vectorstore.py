"""Vector store contract (RED)."""

import os
import tempfile


def _db():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "vectors.db"))


def _record(record_id: str, *, text: str, org_id: str | None = None):
    from memoratum.vectorstore import VectorRecord

    return VectorRecord(
        id=record_id,
        vector=[1.0, 0.0] if record_id.endswith("a") else [0.0, 1.0],
        text=text,
        kind="chunk",
        container_tag="t",
        org_id=org_id,
        metadata={"source": record_id},
    )


def test_in_memory_store_queries_scope_and_metadata() -> None:
    from memoratum.vectorstore import InMemoryVectorStore

    store = InMemoryVectorStore()
    store.upsert(
        [
            _record("a", text="alpha", org_id="org-a"),
            _record("b", text="beta", org_id="org-b"),
        ]
    )
    hits = store.query([1.0, 0.0], container_tag="t", org_id="org-a", limit=5)
    assert [hit.id for hit in hits] == ["a"]
    assert hits[0].text == "alpha"
    assert hits[0].metadata == {"source": "a"}
    assert store.query([1.0, 0.0], container_tag="t", filters={"source": "b"})[0].id == "b"


def test_sqlite_store_upserts_updates_and_deletes() -> None:
    from memoratum.vectorstore import SQLiteVectorStore

    conn = _db()
    store = SQLiteVectorStore(conn)
    store.upsert([_record("a", text="first", org_id="org-a")])
    store.upsert([_record("a", text="updated", org_id="org-a")])
    hits = store.query([1.0, 0.0], container_tag="t", org_id="org-a")
    assert len(hits) == 1 and hits[0].text == "updated"
    assert store.delete(ids=["a"]) == 1
    assert store.query([1.0, 0.0], container_tag="t", org_id="org-a") == []
    conn.close()


def test_sqlite_store_filters_organization() -> None:
    from memoratum.vectorstore import SQLiteVectorStore

    conn = _db()
    store = SQLiteVectorStore(conn)
    store.upsert(
        [
            _record("a", text="alpha", org_id="org-a"),
            _record("b", text="beta", org_id="org-b"),
        ]
    )
    assert [hit.id for hit in store.query([0.0, 1.0], container_tag="t", org_id="org-b")] == ["b"]
    assert len(store.query([0.0, 1.0], container_tag="t")) == 2
    conn.close()


def test_unknown_vector_store_is_rejected() -> None:
    import pytest

    from memoratum.vectorstore import build_vector_store

    with pytest.raises(ValueError, match="unknown vector store"):
        build_vector_store("mystery", conn=None)
