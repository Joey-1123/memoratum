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


def test_recall_at_k_counts_retrieval_positions_not_distinct_sessions() -> None:
    """R@k must mean "a gold session within the first k hits".

    The v0 implementation applied the slice *after* deduplication, so R@k really
    asked "are there k distinct sessions anywhere in the retrieved set". On a
    corpus averaging ~7.6 chunks per session a 20-hit budget yields only ~2.6
    distinct sessions, so R@5 and R@10 were identical -- visible in
    eval/RESULTS-n10.md. See specs/002-evaluation-harness/research.md (D7).
    """
    from memoratum.eval_metrics import full_recall, partial_recall

    # s2 sits at hit-rank 5, so it is out of reach at k=2 and k=4, and in reach at k=5.
    ranked = ["s1", "s1", "s1", "s1", "s2"]
    gold = {"s2"}
    assert partial_recall(ranked, gold, k=2) == 0.0
    assert partial_recall(ranked, gold, k=4) == 0.0
    assert partial_recall(ranked, gold, k=5) == 1.0
    assert full_recall(ranked, gold, k=5) == 1.0


def test_recall_at_k_is_monotone_and_never_exceeds_distinct_session_count() -> None:
    """R@k is non-decreasing in k, and cannot credit a session past the k-th hit."""
    from memoratum.eval_metrics import partial_recall

    # 8 chunks of s1, then s2 at rank 9, then s3 at rank 10.
    ranked = ["s1"] * 8 + ["s2", "s3"]
    gold = {"s2", "s3"}
    assert [partial_recall(ranked, gold, k=k) for k in range(1, 12)] == [
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        1.0,
        1.0,
    ]


def test_recall_at_k_can_only_decrease_versus_the_old_definition() -> None:
    """The fix is a correction downward, never upward.

    The set of sessions visible to R@k under the corrected definition is always a
    subset of the set visible to v0, so a lower score after the fix is expected and
    must not be read as a retrieval regression.
    """
    from memoratum.eval_metrics import partial_recall, session_ids_of

    def v0(ranked: list[str], gold: set[str], *, k: int) -> float:
        top = set(session_ids_of(ranked)[:k])
        return 1.0 if top & gold else 0.0

    cases = [
        (["a"] * 7 + ["b"], {"b"}),
        (["a", "b", "a", "c"], {"c"}),
        (["x", "y", "z"], {"z"}),
        (["a"] * 20, {"a"}),
        (["a", "b", "c", "a", "b", "c"], {"a", "c"}),
    ]
    for ranked, gold in cases:
        for k in (1, 2, 3, 5, 10, 20):
            assert partial_recall(ranked, gold, k=k) <= v0(ranked, gold, k=k), (
                f"corrected R@{k} exceeded v0 for {ranked} gold={gold}"
            )


def test_mrr_still_ranks_over_distinct_sessions() -> None:
    """MRR is a session-space metric and is deliberately unchanged by the fix."""
    from memoratum.eval_metrics import mrr

    # s2 is the first *distinct* gold session, reached at hit-rank 3.
    ranked = ["s1", "s1", "s2"]
    assert mrr(ranked, {"s2"}) == 0.5
