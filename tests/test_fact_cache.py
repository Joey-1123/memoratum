"""Fact embedding cache contract (RED)."""


def test_fact_embeddings_cached_across_searches() -> None:
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
    add_fact(
        conn,
        container_tag="u1",
        subject="user",
        predicate="hates",
        object="Mondays",
        document_id=None,
    )
    search(conn, e, "paris", container_tag="u1", search_mode="memories")
    first_calls = list(calls)
    search(conn, e, "paris", container_tag="u1", search_mode="memories")
    # second search must not re-embed the corpus: only the fresh query vector
    assert sum(calls) < sum(first_calls) + 2, calls
    # new fact invalidates the cache
    add_fact(conn, container_tag="u1", subject="x", predicate="y", object="z", document_id=None)
    search(conn, e, "paris", container_tag="u1", search_mode="memories")
    assert sum(calls) > sum(first_calls) + 1
    conn.close()
