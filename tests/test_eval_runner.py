"""Runner helpers contract (RED)."""


def test_format_session_joins_turns() -> None:
    from memoratum.eval_longmemeval import format_session

    turns = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert format_session(turns) == "user: hi\nassistant: hello"
    assert format_session([]) == ""


def test_evaluate_returns_reproducible_manifest_and_aggregate() -> None:
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_longmemeval import evaluate

    result = evaluate(
        [
            {
                "question_id": "q1",
                "question": "Where is the cat?",
                "question_type": "simple",
                "haystack_session_ids": ["s1", "s2"],
                "haystack_sessions": [
                    [{"role": "user", "content": "The cat is in the garden."}],
                    [{"role": "user", "content": "The dog is in the house."}],
                ],
                "answer_session_ids": ["s1"],
            }
        ],
        n=1,
        seed=9,
        ks=[1],
        modes=["documents"],
        embedder=HashEmbedder(dims=8),
        dataset_hash="fixture",
    )
    assert result["n"] == 1
    assert result["manifest"]["data_sha256"] == "fixture"
    assert result["manifest"]["embedder"] == "HashEmbedder:8"
    assert "documents" in result["aggregate"]
    assert result["questions"][0]["modes"]["documents"]["ranked_sessions"]
