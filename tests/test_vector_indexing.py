"""Vector-index indexing and search pushdown contract (RED)."""

import os
import tempfile


def _db():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "vectors-index.db"))


def test_ingest_populates_scoped_vector_index() -> None:
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_document
    from memoratum.vectorstore import InMemoryVectorStore

    conn = _db()
    doc = db.create_document(
        conn,
        container_tag="u1",
        content="The project guide explains vector indexing.",
        metadata={"project": "atlas"},
        org_id="org-a",
    )
    store = InMemoryVectorStore()
    process_document(conn, HashEmbedder(dims=8), doc["id"], vector_store=store)

    hits = store.query([1.0] * 8, container_tag="u1", org_id="org-a")
    assert len(hits) == 1
    assert hits[0].kind == "chunk"
    assert hits[0].metadata["project"] == "atlas"
    assert hits[0].metadata["document_id"] == doc["id"]
    assert store.query([1.0] * 8, container_tag="u1", org_id="org-b") == []
    conn.close()


def test_search_uses_index_candidates_instead_of_reembedding_chunks() -> None:
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_document
    from memoratum.search import search
    from memoratum.vectorstore import InMemoryVectorStore

    class CountingEmbedder(HashEmbedder):
        def __init__(self):
            super().__init__(dims=8)
            self.calls: list[list[str]] = []

        def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(list(texts))
            return super().embed(texts)

    conn = _db()
    store = InMemoryVectorStore()
    base = HashEmbedder(dims=8)
    first = db.create_document(conn, container_tag="u1", content="The cat sat on the mat.")
    second = db.create_document(conn, container_tag="u1", content="Quantum physics notes.")
    process_document(conn, base, first["id"], vector_store=store)
    process_document(conn, base, second["id"], vector_store=store)

    query_embedder = CountingEmbedder()
    hits = search(
        conn,
        query_embedder,
        "cat mat",
        container_tag="u1",
        limit=5,
        search_mode="documents",
        vector_store=store,
    )
    assert hits and "cat" in hits[0]["chunk"]
    assert query_embedder.calls == [["cat mat"]]
    conn.close()


def test_hybrid_search_keeps_fact_vector_leg_when_chunk_index_is_used() -> None:
    from memoratum import db, facts
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_document
    from memoratum.search import pack_vector, search
    from memoratum.vectorstore import InMemoryVectorStore

    conn = _db()
    store = InMemoryVectorStore()
    embedder = HashEmbedder(dims=8)
    doc = db.create_document(conn, container_tag="u1", content="Document chunk text.")
    process_document(conn, embedder, doc["id"], vector_store=store)
    fact = facts.add_fact(
        conn,
        container_tag="u1",
        subject="user",
        predicate="likes",
        object="tea",
        document_id=None,
    )
    fact_text = f"{fact['subject']} {fact['predicate']} {fact['object']}"
    conn.execute(
        "UPDATE facts SET embedding = ? WHERE id = ?",
        (pack_vector(embedder.embed([fact_text])[0]), fact["id"]),
    )
    conn.commit()

    hits = search(
        conn,
        embedder,
        "tea",
        container_tag="u1",
        limit=10,
        search_mode="hybrid",
        vector_store=store,
    )
    assert any(hit.get("memory") == fact_text for hit in hits)
    conn.close()


def test_vector_index_settings_load_from_environment() -> None:
    from memoratum.config import Settings

    names = (
        "MEMORATUM_VECTOR_STORE",
        "MEMORATUM_VECTOR_STORE_ENDPOINT",
        "MEMORATUM_VECTOR_STORE_PATH",
        "MEMORATUM_VECTOR_STORE_COLLECTION",
    )
    values = ("qdrant", "http://vectors:6333", "/tmp/vectors", "memories")
    for name, value in zip(names, values):
        os.environ[name] = value
    try:
        settings = Settings.load()
        assert settings.vector_store == "qdrant"
        assert settings.vector_store_endpoint == "http://vectors:6333"
        assert settings.vector_store_path == "/tmp/vectors"
        assert settings.vector_store_collection == "memories"
    finally:
        for name in names:
            os.environ.pop(name, None)
