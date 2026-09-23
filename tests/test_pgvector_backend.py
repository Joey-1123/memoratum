"""Postgres vector backend contract (RED)."""

from memoratum.vectorstore import VectorRecord


def test_pgvector_adapter_uses_cosine_distance_and_scope() -> None:
    from memoratum.vectorstore import PgVectorStore

    class Cursor:
        def __init__(self):
            self.calls = []

        def execute(self, query, params=None):
            self.calls.append((query, params))
            if "SELECT" in query:
                self.rows = [
                    ("chunk-a", "chunk", "alpha", "t", "org-a", '{"source": "test"}', 1.0, 0.9)
                ]
            return self

        def fetchall(self):
            return getattr(self, "rows", [])

        def rowcount(self):
            return 1

    class Connection:
        def __init__(self):
            self.cursor_obj = Cursor()
            self.closed = False

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            pass

        def close(self):
            self.closed = True

    conn = Connection()
    store = PgVectorStore(conn, dims=2, table="vectors")
    store.upsert(
        [
            VectorRecord(
                id="chunk-a",
                vector=[1.0, 0.0],
                text="alpha",
                kind="chunk",
                container_tag="t",
                org_id="org-a",
            )
        ]
    )
    hits = store.query([1.0, 0.0], container_tag="t", org_id="org-a")
    assert hits[0].id == "chunk-a"
    assert hits[0].score == 0.9
    assert any("ORDER BY embedding <=>" in query for query, _ in conn.cursor_obj.calls)
    store.delete(ids=["chunk-a"])
    store.close()
    assert conn.closed
