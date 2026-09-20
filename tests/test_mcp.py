"""MCP server contract (RED)."""

import json
import os
import subprocess
import sys
import tempfile


def _session():
    env = dict(os.environ, MEMORATUM_DATA_DIR=tempfile.mkdtemp())
    proc = subprocess.Popen(
        [sys.executable, "-m", "memoratum.mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=env,
    )
    assert proc.stdin and proc.stdout

    def send(obj: dict) -> None:
        assert proc.stdin
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv() -> dict:
        assert proc.stdout
        line = proc.stdout.readline()
        assert line, "server closed stdout"
        return json.loads(line)

    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )
    init = recv()
    assert init["result"]["serverInfo"]["name"] == "memoratum"
    return proc, send, recv


def test_remember_and_recall() -> None:
    proc, send, recv = _session()
    try:
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [t["name"] for t in recv()["result"]["tools"]]
        assert "remember" in names and "recall" in names
        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "remember",
                    "arguments": {"content": "The user loves Paris.", "containerTag": "u1"},
                },
            }
        )
        assert recv()["result"]["content"][0]["text"] == "stored"
        send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "recall", "arguments": {"q": "paris", "containerTag": "u1"}},
            }
        )
        texts = [c["text"] for c in recv()["result"]["content"]]
        assert any("Paris" in t for t in texts)
    finally:
        proc.kill()


def test_unknown_tool_errors() -> None:
    proc, send, recv = _session()
    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            }
        )
        assert "error" in recv()
    finally:
        proc.kill()
