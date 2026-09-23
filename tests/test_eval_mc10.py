"""LoCoMo-MC10 evaluation contract (RED)."""

import json


def _item() -> dict:
    return {
        "question_id": "mc-1",
        "question_type": "single_hop",
        "question": "What color is the key?",
        "choices": [
            "blue",
            "red",
            "green",
            "yellow",
            "black",
            "white",
            "purple",
            "orange",
            "pink",
            "brown",
        ],
        "answer": "blue",
        "correct_choice_index": 0,
        "haystack_session_ids": ["s1", "s2"],
        "haystack_sessions": [
            [{"speaker": "A", "text": "The key is blue."}],
            [{"speaker": "B", "text": "We saw a red house."}],
        ],
    }


def test_normalize_mc10_validates_choices_and_sessions() -> None:
    from memoratum.eval_mc10 import normalize_mc10

    records = normalize_mc10([_item()])
    assert records[0]["question_id"] == "mc-1"
    assert records[0]["sessions"][0]["session_id"] == "s1"
    assert records[0]["correct_choice_index"] == 0


def test_heuristic_answerer_uses_retrieved_context() -> None:
    from memoratum.eval_mc10 import HeuristicChoiceAnswerer

    answerer = HeuristicChoiceAnswerer()
    assert answerer.choose("What color is the key?", ["blue", "red"], "The key is blue.") == 0


def test_evaluate_mc10_reports_accuracy_and_balanced_accuracy() -> None:
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_mc10 import HeuristicChoiceAnswerer, evaluate_mc10

    result = evaluate_mc10(
        [_item()],
        n=1,
        seed=4,
        k=1,
        embedder=HashEmbedder(dims=8),
        answerer=HeuristicChoiceAnswerer(),
        dataset_hash="fixture",
    )
    assert result["accuracy"] == 1.0
    assert result["balanced_accuracy"] == 1.0
    assert result["by_type"]["single_hop"]["accuracy"] == 1.0


def test_mc10_records_load_from_jsonl(tmp_path) -> None:
    from memoratum.eval_mc10 import load_mc10_records

    path = tmp_path / "mc10.jsonl"
    path.write_text(json.dumps(_item()) + "\n")
    assert load_mc10_records(path)[0]["question_id"] == "mc-1"
