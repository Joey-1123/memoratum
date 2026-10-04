"""MCP stdio server.

`mcp.py` was the least-tested module in the project at 0% coverage -- it is the
one server surface with no HTTP boundary, so nothing else in the suite reached it.
These tests drive the real `main()` loop over real stdin/stdout rather than
importing helpers, because the JSON-RPC framing *is* the contract.

The server has no auth by design (local-process trust only, per docs/SECURITY.md),
so these tests assert framing and error handling, not authorization.
"""

import io
import json
import os

import pytest


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_EMBEDDINGS_PROVIDER", "hash")
    return tmp_path


def _rpc(monkeypatch, *requests) -> list[dict]:
    """Feed newline-delimited JSON-RPC into the real server loop and parse replies."""
    from memoratum import mcp

    payload = "".join(json.dumps(r) + "\n" for r in requests)
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    mcp.main()
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _call(mid: int, name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


# --- handshake and discovery ------------------------------------------------


def test_initialize_reports_protocol_and_server(monkeypatch, data_dir):
    (reply,) = _rpc(monkeypatch, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert reply["id"] == 1
    assert reply["jsonrpc"] == "2.0"
    assert reply["result"]["protocolVersion"]
    assert reply["result"]["serverInfo"]["name"] == "memoratum"


def test_tools_list_advertises_both_tools(monkeypatch, data_dir):
    (reply,) = _rpc(monkeypatch, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    names = {tool["name"] for tool in reply["result"]["tools"]}
    assert names == {"remember", "recall"}
    for tool in reply["result"]["tools"]:
        assert tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["required"], f"{tool['name']} declares no required arguments"
        for required in schema["required"]:
            assert required in schema["properties"], (
                f"{tool['name']} requires {required!r} but does not declare it"
            )


def test_tools_list_is_not_mutable_by_a_caller(monkeypatch, data_dir):
    """A caller receiving the list must not be able to corrupt the server's copy."""
    from memoratum import mcp

    before = json.dumps(mcp.TOOLS, sort_keys=True)
    _rpc(monkeypatch, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert json.dumps(mcp.TOOLS, sort_keys=True) == before


# --- the tools actually work ----------------------------------------------


def test_remember_then_recall_round_trip(monkeypatch, data_dir):
    replies = _rpc(
        monkeypatch,
        _call(1, "remember", {"content": "the sky is green", "containerTag": "mcp:test"}),
        _call(2, "recall", {"q": "sky green", "containerTag": "mcp:test"}),
    )
    assert len(replies) == 2, replies
    assert "error" not in replies[0], replies[0]
    assert replies[0]["result"]["content"][0]["text"] == "stored"

    assert "error" not in replies[1], replies[1]
    texts = [part["text"] for part in replies[1]["result"]["content"]]
    assert any("sky is green" in t for t in texts), texts


def test_remember_defaults_the_container_tag(monkeypatch, data_dir):
    (reply,) = _rpc(monkeypatch, _call(1, "remember", {"content": "no tag given"}))
    assert "error" not in reply, reply
    from memoratum import db
    from memoratum.config import Settings

    conn = db.connect(Settings.load().db_path)
    try:
        rows = conn.execute("SELECT container_tag FROM documents").fetchall()
        assert [r["container_tag"] for r in rows] == ["default"]
    finally:
        conn.close()


def test_recall_respects_the_container_tag(monkeypatch, data_dir):
    """Tags are the isolation boundary; a recall must not cross them."""
    replies = _rpc(
        monkeypatch,
        _call(1, "remember", {"content": "alpha private", "containerTag": "mcp:a"}),
        _call(2, "remember", {"content": "beta private", "containerTag": "mcp:b"}),
        _call(3, "recall", {"q": "private", "containerTag": "mcp:a"}),
    )
    texts = [part["text"] for part in replies[2]["result"]["content"]]
    assert any("alpha" in t for t in texts), texts
    assert not any("beta" in t for t in texts), f"recall leaked across tags: {texts}"


def test_recall_honours_limit(monkeypatch, data_dir):
    requests = [
        _call(index, "remember", {"content": f"item {index} zebra", "containerTag": "mcp:lim"})
        for index in range(6)
    ]
    requests.append(_call(99, "recall", {"q": "zebra", "containerTag": "mcp:lim", "limit": 2}))
    replies = _rpc(monkeypatch, *requests)
    assert len(replies[-1]["result"]["content"]) <= 2, replies[-1]


# --- error handling --------------------------------------------------------


def test_unknown_tool_returns_method_not_found(monkeypatch, data_dir):
    (reply,) = _rpc(monkeypatch, _call(1, "teleport", {}))
    assert reply["error"]["code"] == -32601
    assert "teleport" in reply["error"]["message"]


def test_unknown_method_returns_method_not_found(monkeypatch, data_dir):
    (reply,) = _rpc(monkeypatch, {"jsonrpc": "2.0", "id": 1, "method": "tools/frobnicate"})
    assert reply["error"]["code"] == -32601


def test_missing_required_argument_is_an_error_not_a_crash(monkeypatch, data_dir):
    """A KeyError inside the tool must still produce valid JSON-RPC."""
    (reply,) = _rpc(monkeypatch, _call(1, "remember", {}))
    assert "error" in reply, reply
    assert reply["error"]["code"] == -32603
    assert isinstance(reply["error"]["message"], str)


def test_malformed_json_is_skipped_and_does_not_desync(monkeypatch, data_dir):
    from memoratum import mcp

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('not json\n{"jsonrpc":"2.0","id":7,"method":"initialize","params":{}}\n'),
    )
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    mcp.main()
    replies = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert len(replies) == 1, "a bad line must not produce a reply or stop the loop"
    assert replies[0]["id"] == 7


def test_blank_lines_are_ignored(monkeypatch, data_dir):
    from memoratum import mcp

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('\n\n   \n{"jsonrpc":"2.0","id":3,"method":"initialize","params":{}}\n\n'),
    )
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    mcp.main()
    assert len([ln for ln in out.getvalue().splitlines() if ln.strip()]) == 1


def test_every_request_gets_exactly_one_reply(monkeypatch, data_dir):
    """JSON-RPC requires a response per request; silence is a protocol violation."""
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        _call(3, "teleport", {}),
        _call(4, "remember", {"content": "ok", "containerTag": "mcp:x"}),
    ]
    replies = _rpc(monkeypatch, *requests)
    assert [r["id"] for r in replies] == [1, 2, 3, 4], "replies must be ordered and complete"


def test_notification_without_id_still_answers(monkeypatch, data_dir):
    """No id means a notification; JSON-RPC says answer nothing, so this is asserted
    explicitly rather than left to chance."""
    from memoratum import mcp

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('{"jsonrpc":"2.0","method":"initialize","params":{}}\n'),
    )
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    mcp.main()
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] is None


def test_empty_stdin_is_clean(monkeypatch, data_dir):
    from memoratum import mcp

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    mcp.main()
    assert out.getvalue() == ""


def test_stored_content_is_retrievable_out_of_band(monkeypatch, data_dir):
    """The MCP path must write through the same store the HTTP API reads."""
    from memoratum import db
    from memoratum.config import Settings

    _rpc(monkeypatch, _call(1, "remember", {"content": "persisted words", "containerTag": "mcp:p"}))
    conn = db.connect(Settings.load().db_path)
    try:
        docs = conn.execute("SELECT content FROM documents").fetchall()
        assert any("persisted words" in r["content"] for r in docs)
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        assert chunks > 0, "the MCP remember path must actually ingest"
    finally:
        conn.close()


def test_no_network_auth_is_expected(monkeypatch, data_dir):
    """Local-process trust only: the server must not require or read a key.

    docs/SECURITY.md states the MCP server has no auth by design and must not be
    exposed over a network transport. This pins that it also does not silently
    start requiring one.
    """
    source = open(  # noqa: SIM115 - reading the module under test deliberately
        os.path.join(os.path.dirname(__file__), "..", "src", "memoratum", "mcp.py")
    ).read()
    assert "auth" not in source.lower(), "the MCP server must stay auth-free by design"
    assert "uvicorn" not in source, "the MCP server must not open a network transport"
