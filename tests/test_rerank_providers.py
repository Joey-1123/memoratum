"""Reranker provider contract (RED)."""

import json


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.payload


def test_cohere_reranker_sends_v2_shape_and_restores_input_order() -> None:
    from memoratum.rerank import CohereReranker

    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return Response(
            {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]
            }
        )

    reranker = CohereReranker(api_key="key", opener=opener)
    assert reranker.score("q", ["first", "second"]) == [0.2, 0.9]
    request, timeout = calls[0]
    assert request.full_url == "https://api.cohere.com/v2/rerank"
    assert timeout == 30.0
    body = json.loads(request.data)
    assert body == {"model": "rerank-v3.5", "query": "q", "documents": ["first", "second"]}
    assert request.get_header("Authorization") == "Bearer key"


def test_voyage_reranker_uses_its_endpoint_and_model() -> None:
    from memoratum.rerank import VoyageReranker

    calls = []

    def opener(request, timeout):
        calls.append(request)
        return Response({"results": [{"index": 0, "relevance_score": 0.75}]})

    reranker = VoyageReranker(model="rerank-2.5", api_key="v", opener=opener)
    assert reranker.score("q", ["doc"]) == [0.75]
    assert calls[0].full_url == "https://api.voyageai.com/v1/rerank"
    assert json.loads(calls[0].data)["model"] == "rerank-2.5"


def test_reranker_factory_selects_remote_providers() -> None:
    from memoratum.rerank import CohereReranker, VoyageReranker, build_reranker

    assert isinstance(build_reranker("cohere", model="rerank-v3.5"), CohereReranker)
    assert isinstance(build_reranker("voyage", model="rerank-2.5"), VoyageReranker)
