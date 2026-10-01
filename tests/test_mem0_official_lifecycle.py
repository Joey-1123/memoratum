"""Official Mem0 client lifecycle replay against a local contract server (RED)."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest

pytest.importorskip("httpx")
pytest.importorskip("mem0")
os.environ.setdefault("MEM0_TELEMETRY", "false")


class _Handler(BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, str, dict[str, Any], str]]] = []
    memory_id: ClassVar[str] = "mem_contract"
    text: ClassVar[str] = "before"
    history: ClassVar[list[dict[str, Any]]] = [
        {
            "id": "h1",
            "memory_id": memory_id,
            "event": "ADD",
            "old_memory": None,
            "new_memory": "before",
        }
    ]

    def _reply(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body: dict[str, Any] | None = None) -> None:
        auth = self.headers.get("Authorization", "")
        type(self).calls.append((self.command, urlsplit(self.path).path, body or {}, auth))

    def do_GET(self) -> None:
        self._record()
        if self.path == "/v1/ping/":
            self._reply({"status": "ok", "org_id": "org-1", "project_id": "project-1"})
        elif self.path == f"/v1/memories/{self.memory_id}/":
            self._reply({"id": self.memory_id, "memory": self.text, "metadata": {}})
        elif self.path == f"/v1/memories/{self.memory_id}/history/":
            self._reply(self.history)
        elif self.path == "/v3/memories/":
            self._reply(
                {
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{"id": self.memory_id, "memory": self.text}],
                }
            )
        else:
            self._reply({"error": {"code": "NOT_FOUND", "message": "not found"}}, 404)

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self._record(body)
        if self.path == f"/v1/memories/{self.memory_id}/":
            type(self).text = body.get("text", type(self).text)
            type(self).history.append(
                {
                    "id": "h2",
                    "memory_id": self.memory_id,
                    "event": "UPDATE",
                    "old_memory": "before",
                    "new_memory": type(self).text,
                }
            )
            self._reply({"id": self.memory_id, "memory": type(self).text})
        elif self.path == "/v1/batch/":
            self._reply(
                {"message": f"Successfully updated {len(body.get('memories', []))} memories"}
            )
        else:
            self._reply({"error": {"code": "NOT_FOUND", "message": "not found"}}, 404)

    def do_DELETE(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        path = urlsplit(self.path).path
        self._record(body)
        if path == "/v1/batch/":
            self._reply(
                {"message": f"Successfully deleted {len(body.get('memories', []))} memories"}
            )
        elif path == "/v1/memories/":
            self._reply(
                {"message": "Delete in progress. This may take some time.", "event_id": "e-delete"}
            )
        elif path == f"/v1/memories/{self.memory_id}/":
            self._reply({"message": "Memory deleted successfully!"})
        else:
            self._reply({"error": {"code": "NOT_FOUND", "message": "not found"}}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self._record(body)
        if self.path == "/v3/memories/":
            self._reply(
                {
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [{"id": self.memory_id, "memory": self.text}],
                }
            )
        else:
            self._reply({"error": {"code": "NOT_FOUND", "message": "not found"}}, 404)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def lifecycle_server():
    _Handler.calls.clear()
    _Handler.text = "before"
    _Handler.history = [
        {
            "id": "h1",
            "memory_id": _Handler.memory_id,
            "event": "ADD",
            "old_memory": None,
            "new_memory": "before",
        }
    ]
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_official_client_replays_lifecycle_routes(lifecycle_server) -> None:
    from mem0 import MemoryClient

    client = MemoryClient(
        api_key="contract-key",
        host=f"http://127.0.0.1:{lifecycle_server.server_port}",
    )
    try:
        assert client.get(_Handler.memory_id)["memory"] == "before"
        assert client.update(_Handler.memory_id, text="after")["memory"] == "after"
        assert client.history(_Handler.memory_id)[-1]["event"] == "UPDATE"
        assert client.get_all(filters={"user_id": "alice"})["count"] == 1
        assert client.delete(_Handler.memory_id)["message"].startswith("Memory deleted")
        assert client.delete_all(user_id="alice")["event_id"] == "e-delete"
        assert client.batch_update([{"memory_id": _Handler.memory_id, "text": "batch"}])[
            "message"
        ].startswith("Successfully updated")
        assert client.batch_delete([{"memory_id": _Handler.memory_id}])["message"].startswith(
            "Successfully deleted"
        )
    finally:
        client.client.close()

    paths = [(method, path) for method, path, _, _ in _Handler.calls]
    assert ("GET", f"/v1/memories/{_Handler.memory_id}/") in paths
    assert ("PUT", f"/v1/memories/{_Handler.memory_id}/") in paths
    assert ("GET", f"/v1/memories/{_Handler.memory_id}/history/") in paths
    assert ("POST", "/v3/memories/") in paths
    assert ("DELETE", f"/v1/memories/{_Handler.memory_id}/") in paths
    assert ("DELETE", "/v1/memories/") in paths
    assert ("PUT", "/v1/batch/") in paths
    assert ("DELETE", "/v1/batch/") in paths
    assert all(auth.startswith("Token ") for _, _, _, auth in _Handler.calls)
