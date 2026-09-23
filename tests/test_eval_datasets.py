"""Evaluation dataset adapter contract (RED)."""

import json


def test_load_records_supports_jsonl_and_object_wrapper(tmp_path) -> None:
    from memoratum.eval_datasets import load_records

    jsonl = tmp_path / "records.jsonl"
    jsonl.write_text('{"question":"a"}\n\n{"question":"b"}\n')
    assert [record["question"] for record in load_records(jsonl)] == ["a", "b"]

    wrapped = tmp_path / "records.json"
    wrapped.write_text(json.dumps({"questions": [{"question": "c"}]}))
    assert load_records(wrapped) == [{"question": "c"}]


def test_normalize_longmemeval_accepts_alternate_session_shape() -> None:
    from memoratum.eval_datasets import normalize_longmemeval

    records = normalize_longmemeval(
        [
            {
                "id": "q1",
                "query": "What happened?",
                "sessions": {
                    "s1": {"messages": [{"speaker": "A", "text": "hello"}]},
                    "s2": [{"role": "user", "content": "world"}],
                },
                "answer_session_id": "s2",
            }
        ]
    )
    assert records[0]["question_id"] == "q1"
    assert [session["session_id"] for session in records[0]["sessions"]] == ["s1", "s2"]
    assert records[0]["sessions"][0]["turns"][0]["content"] == "hello"
    assert records[0]["answer_session_ids"] == ["s2"]


def test_sampling_is_reproducible_and_full_dataset_can_be_requested() -> None:
    from memoratum.eval_datasets import sample_records

    records = [{"question": str(index)} for index in range(5)]
    assert sample_records(records, n=2, seed=7) == sample_records(records, n=2, seed=7)
    assert sample_records(records, n=0, seed=7) == records
