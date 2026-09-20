"""Runner helpers contract (RED)."""


def test_format_session_joins_turns() -> None:
    from memoratum.eval_longmemeval import format_session

    turns = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert format_session(turns) == "user: hi\nassistant: hello"
    assert format_session([]) == ""
