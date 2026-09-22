"""Reranker provider contract (RED)."""


def test_heuristic_precision_ranking() -> None:
    from memoratum.rerank import HeuristicReranker

    r = HeuristicReranker()
    docs = [
        "the cat sat on the mat and more filler words here",
        "quantum physics black holes event horizons",
    ]
    scores = r.score("cat mat", docs)
    assert scores[0] > scores[1]
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_cross_encoder_lazy_missing() -> None:
    from memoratum.rerank import CrossEncoderReranker

    r = CrossEncoderReranker(model="definitely-not-a-real-model-xyz")
    try:
        r.score("q", ["d"])
    except RuntimeError as e:
        assert "sentence-transformers" in str(e) or "model" in str(e).lower()
        return
    raise AssertionError("expected RuntimeError")
