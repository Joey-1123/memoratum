"""SDK client contract (RED)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar


class _Stub(BaseHTTPRequestHandler):
    seen: ClassVar[list] = []

    def _reply(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", "0"))
        _Stub.seen.append(
            (self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(n) or b"{}"))
        )
        if self.path == "/v3/documents":
            self._reply({"id": "d1", "status": "done"})
        else:
            self._reply(
                {
                    "results": [{"memory": "user loves Paris", "similarity": 0.9}],
                    "timing": 1,
                    "total": 1,
                }
            )

    def log_message(self, *args: object) -> None:
        pass


def _client():
    from memoratum.client import Client

    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Stub.seen.clear()
    return server, Client(base_url=f"http://127.0.0.1:{server.server_port}", api_key="k")


def test_add_sends_auth_and_returns_id() -> None:
    server, c = _client()
    try:
        doc = c.add("hello", container_tag="u1")
        assert doc["id"] == "d1"
        path, auth, body = _Stub.seen[0]
        assert path == "/v3/documents"
        assert auth == "Bearer k"
        assert body["containerTag"] == "u1"
    finally:
        server.shutdown()


def test_search_returns_results() -> None:
    server, c = _client()
    try:
        res = c.search("paris", container_tag="u1", search_mode="memories", limit=3)
        assert res["total"] == 1
        assert res["results"][0]["memory"] == "user loves Paris"
    finally:
        server.shutdown()


def test_http_error_raises() -> None:
    from memoratum.client import Client, MemoratumError

    c = Client(base_url="http://127.0.0.1:1", api_key="k", timeout=2.0)
    try:
        c.search("x")
    except MemoratumError:
        return
    raise AssertionError("expected MemoratumError")
