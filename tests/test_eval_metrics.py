"""Eval metric math contract (RED)."""


def test_session_recall_metrics() -> None:
    from memoratum.eval_metrics import mrr, partial_recall, session_ids_of

    ranked = ["s3", "s1", "s2"]
    assert session_ids_of(ranked) == ["s3", "s1", "s2"]
    assert partial_recall(ranked, {"s1"}, k=2) == 1.0
    assert partial_recall(ranked, {"s9"}, k=2) == 0.0
    assert mrr(ranked, {"s1"}) == 0.5
    assert mrr(ranked, {"s9"}) == 0.0


def test_full_recall_needs_all_gold() -> None:
    from memoratum.eval_metrics import full_recall

    assert full_recall(["s1", "s2", "s3"], {"s1", "s2"}, k=2) == 1.0
    assert full_recall(["s1", "s3"], {"s1", "s2"}, k=2) == 0.0
