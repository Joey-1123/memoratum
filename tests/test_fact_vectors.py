"""Persisted fact vectors contract (RED)."""


def test_search_backfills_missing_vectors_once() -> None:
    import os
    import tempfile

    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.facts import add_fact
    from memoratum.search import search

    calls = []

    class Counting(HashEmbedder):
        def embed(self, texts):
            calls.append(len(texts))
            return super().embed(texts)

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
    e = Counting(dims=16)
    add_fact(
        conn,
        container_tag="u1",
        subject="user",
        predicate="loves",
        object="Paris",
        document_id=None,
    )
    row = conn.execute("SELECT embedding FROM facts").fetchone()
    assert row["embedding"] is None
    search(conn, e, "paris", container_tag="u1", search_mode="memories")
    row = conn.execute("SELECT embedding FROM facts").fetchone()
    assert row["embedding"] is not None
    n_calls = len(calls)
    search(conn, e, "paris", container_tag="u1", search_mode="memories")
    assert len(calls) == n_calls + 1, calls  # query vector only
    conn.close()
