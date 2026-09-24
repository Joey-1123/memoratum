"""Official Mem0 Python SDK contract against a local Memoratum server."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

pytest.importorskip("httpx", reason="the official Mem0 Python client uses httpx")
pytest.importorskip("mem0", reason="official Mem0 Python SDK is not installed")
os.environ.setdefault("MEM0_TELEMETRY", "false")


def _fixture(name: str) -> dict:
    path = Path(__file__).parent / "fixtures" / "mem0" / "v0.1" / name
    return json.loads(path.read_text())


class _Handler(BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, str, dict]]] = []

    def _reply(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        type(self).calls.append((self.command, self.path, {}))
        if self.path == "/v1/ping/":
            self._reply(
                {
                    "status": "ok",
                    "user_email": None,
                    "org_id": "org-1",
                    "project_id": "project-1",
                }
            )
        else:
            self._reply({"error": {"code": "NOT_FOUND", "message": "not found"}}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append((self.command, self.path, payload))
        if self.path == "/v3/memories/add/":
            self._reply(_fixture("add_response.json"))
        elif self.path == "/v3/memories/search/":
            self._reply(_fixture("search_response.json"))
        else:
            self._reply({"error": {"code": "NOT_FOUND", "message": "not found"}}, 404)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def official_client_server():
    _Handler.calls.clear()
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_official_python_client_replays_supported_routes(official_client_server) -> None:
    from mem0 import MemoryClient

    client = MemoryClient(
        api_key="contract-key",
        host=f"http://127.0.0.1:{official_client_server.server_port}",
    )
    try:
        added = client.add(_fixture("add_request.json")["messages"], user_id="contract-user")
        assert added["status"] == "PENDING"
        searched = client.search("local memories", filters={"user_id": "contract-user"}, top_k=5)
        assert searched["results"]

        from memoratum.mem0_contract import validate_add_request, validate_response

        validate_add_request(_fixture("add_request.json"))
        validate_response(added, kind="add")
        validate_response(searched, kind="search")
    finally:
        client.client.close()

    assert _Handler.calls[0][:2] == ("GET", "/v1/ping/")
    assert _Handler.calls[1][:2] == ("POST", "/v3/memories/add/")
    assert _Handler.calls[2][:2] == ("POST", "/v3/memories/search/")
    assert _Handler.calls[1][2]["user_id"] == "contract-user"
    assert _Handler.calls[2][2]["filters"] == {"user_id": "contract-user"}
    assert _Handler.calls[1][2]["messages"] == _fixture("add_request.json")["messages"]
