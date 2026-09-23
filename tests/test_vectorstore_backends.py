"""Optional vector backend adapter contract (RED)."""

from memoratum.vectorstore import VectorRecord


def _record(record_id: str = "chunk-a"):
    return VectorRecord(
        id=record_id,
        vector=[1.0, 0.0],
        text="alpha",
        kind="chunk",
        container_tag="t",
        org_id="org-a",
        metadata={"source": "test"},
    )


def test_qdrant_adapter_maps_points_and_payload_filters() -> None:
    from memoratum.vectorstore import QdrantVectorStore

    calls = []

    class FakeModels:
        class Distance:
            COSINE = "cosine"

        class VectorParams:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class FieldCondition:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class MatchValue:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class Filter:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class PointStruct:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

    class FakeClient:
        def get_or_create_collection(self, **kwargs):
            calls.append(("collection", kwargs))
            return object()

        def upsert(self, **kwargs):
            calls.append(("upsert", kwargs))

        def query_points(self, **kwargs):
            calls.append(("query", kwargs))
            return type(
                "Result",
                (),
                {
                    "points": [
                        type(
                            "Point",
                            (),
                            {
                                "id": "uuid",
                                "score": 0.9,
                                "payload": {
                                    "record_id": "chunk-a",
                                    "text": "alpha",
                                    "kind": "chunk",
                                    "container_tag": "t",
                                    "org_id": "org-a",
                                    "metadata_json": '{"source": "test"}',
                                },
                            },
                        )()
                    ]
                },
            )()

        def delete(self, **kwargs):
            calls.append(("delete", kwargs))

        def close(self):
            calls.append(("close", {}))

    client = FakeClient()
    store = QdrantVectorStore(
        client=client,
        models=FakeModels,
        collection_name="mem",
        dims=2,
    )
    store.upsert([_record()])
    hits = store.query([1.0, 0.0], container_tag="t", org_id="org-a")
    assert hits[0].id == "chunk-a"
    store.delete(ids=["chunk-a"])
    store.close()
    assert any(name == "upsert" for name, _ in calls)
    assert any(name == "query" for name, _ in calls)
    assert any(name == "delete" for name, _ in calls)
    assert calls[-1][0] == "close"


def test_chroma_adapter_upserts_and_parses_columnar_results() -> None:
    from memoratum.vectorstore import ChromaVectorStore

    class FakeCollection:
        def __init__(self):
            self.calls = []

        def upsert(self, **kwargs):
            self.calls.append(("upsert", kwargs))

        def query(self, **kwargs):
            self.calls.append(("query", kwargs))
            return {
                "ids": [["chunk-a"]],
                "distances": [[0.1]],
                "documents": [["alpha"]],
                "metadatas": [
                    [
                        {
                            "record_id": "chunk-a",
                            "kind": "chunk",
                            "container_tag": "t",
                            "org_id": "org-a",
                            "metadata_json": '{"source": "test"}',
                        }
                    ]
                ],
            }

        def delete(self, **kwargs):
            self.calls.append(("delete", kwargs))

    class FakeClient:
        def __init__(self):
            self.collection = FakeCollection()
            self.calls = []

        def get_or_create_collection(self, **kwargs):
            self.calls.append(("collection", kwargs))
            return self.collection

        def close(self):
            self.calls.append(("close", {}))

    client = FakeClient()
    store = ChromaVectorStore(client=client, collection_name="mem")
    store.upsert([_record()])
    hits = store.query([1.0, 0.0], container_tag="t", org_id="org-a")
    assert hits[0].id == "chunk-a"
    assert hits[0].score == 0.9
    store.delete(ids=["chunk-a"])
    store.close()
    assert any(name == "upsert" for name, _ in client.collection.calls)
    assert any(name == "query" for name, _ in client.collection.calls)
    assert any(name == "delete" for name, _ in client.collection.calls)
    assert client.calls[-1][0] == "close"


def test_backend_factory_selects_injected_clients() -> None:
    from memoratum.vectorstore import ChromaVectorStore, QdrantVectorStore, build_vector_store

    class Client:
        pass

    client = Client()
    assert isinstance(
        build_vector_store("qdrant", conn=None, client=client, collection_name="q", dims=2),
        QdrantVectorStore,
    )
    assert isinstance(
        build_vector_store("chroma", conn=None, client=client, collection_name="c"),
        ChromaVectorStore,
    )
