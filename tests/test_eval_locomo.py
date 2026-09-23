"""LoCoMo evaluation adapter contract (RED)."""

import json


def _sample() -> dict:
    return {
        "sample_id": "conversation-1",
        "conversation": {
            "session_1": [
                {"speaker": "A", "dia_id": "d1", "text": "I found the blue key."},
            ],
            "session_2": [
                {"speaker": "B", "dia_id": "d2", "text": "We ate pizza."},
            ],
        },
        "observation": {
            "session_1_observation": "A found a blue key.",
            "session_2_observation": "A and B ate pizza.",
        },
        "session_summary": {
            "session_1_summary": "A found a key.",
            "session_2_summary": "They ate pizza.",
        },
        "qa": [
            {"question": "What did A find?", "answer": "blue key", "evidence": ["d1"]},
        ],
    }


def test_normalize_locomo_maps_evidence_dialogs_to_sessions() -> None:
    from memoratum.eval_locomo import normalize_locomo

    records = normalize_locomo([_sample()])
    assert len(records) == 1
    assert records[0]["question_id"] == "conversation-1:0"
    assert records[0]["answer_session_ids"] == ["session_1"]
    assert records[0]["sessions"][0]["turns"][0]["content"] == "I found the blue key."


def test_normalize_locomo_supports_observation_source() -> None:
    from memoratum.eval_locomo import normalize_locomo

    records = normalize_locomo([_sample()], source="observations")
    contents = [turn["content"] for session in records[0]["sessions"] for turn in session["turns"]]
    assert "A found a blue key." in contents


def test_evaluate_locomo_runs_retrieval_against_normalized_sessions() -> None:
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_locomo import evaluate_locomo

    result = evaluate_locomo(
        [_sample()],
        n=1,
        seed=3,
        ks=[1],
        modes=["documents"],
        embedder=HashEmbedder(dims=8),
        dataset_hash="fixture",
    )
    assert result["dataset"] == "locomo-dialogs"
    assert result["questions"][0]["modes"]["documents"]["ranked_sessions"]


def test_locomo_records_can_be_loaded_from_wrapped_json(tmp_path) -> None:
    from memoratum.eval_locomo import load_locomo_records

    path = tmp_path / "locomo.json"
    path.write_text(json.dumps({"data": [_sample()]}))
    assert load_locomo_records(path)[0]["sample_id"] == "conversation-1"
