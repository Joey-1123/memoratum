"""Provider-neutral embedding adapter contract (RED)."""

import sys
import types


def test_litellm_embedder_parses_indexed_vectors() -> None:
    from memoratum.embeddings import LiteLLMEmbedder

    calls = []

    def embedding(**kwargs):
        calls.append(kwargs)
        return {
            "data": [
                {"index": 1, "embedding": [0.3, 0.4]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        }

    fake = types.SimpleNamespace(embedding=embedding, telemetry=True)
    embedder = LiteLLMEmbedder(model="provider/embed", client=fake)
    assert embedder.embed(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert embedder.dims == 2
    assert calls[0]["model"] == "provider/embed"
    assert calls[0]["input"] == ["a", "b"]
    assert fake.telemetry is False


def test_fastembed_adapter_converts_generator_and_sets_dims() -> None:
    from memoratum.embeddings import FastEmbedEmbedder

    class FakeModel:
        def embed(self, texts):
            assert texts == ["a", "b"]
            return ([1.0, 0.0], [0.0, 1.0])

    embedder = FastEmbedEmbedder(model_name="local/model", model=FakeModel())
    assert embedder.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert embedder.dims == 2


def test_embedding_factory_falls_back_to_api_for_missing_optional_gateway(monkeypatch) -> None:
    from memoratum.embeddings import ApiEmbedder, build_embedder

    monkeypatch.setitem(sys.modules, "litellm", None)
    embedder = build_embedder(
        "litellm",
        endpoint="http://local/v1",
        model="embed-model",
        api_key="",
        dims=0,
    )
    assert isinstance(embedder, ApiEmbedder)


def test_embedding_factory_rejects_unknown_provider() -> None:
    import pytest

    from memoratum.embeddings import build_embedder

    with pytest.raises(ValueError, match="unknown embedding provider"):
        build_embedder("mystery", endpoint="", model="", api_key="", dims=0)
