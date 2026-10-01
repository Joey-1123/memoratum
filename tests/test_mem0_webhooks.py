"""Official Mem0 webhook client route replay against a local contract server."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import pytest

pytest.importorskip("httpx")
pytest.importorskip("mem0")
os.environ.setdefault("MEM0_TELEMETRY", "false")


class _WebhookHandler(BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, str, dict[str, Any]]]] = []
    webhook_id: ClassVar[str] = "wh_contract"

    def _reply(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        type(self).calls.append(("GET", self.path, {}))
        if self.path == "/v1/ping/":
            self._reply(
                {"status": "ok", "user_email": None, "org_id": "org-1", "project_id": "project-1"}
            )
        elif self.path == "/api/v1/webhooks/projects/project-1/":
            self._reply(
                [
                    {
                        "id": self.webhook_id,
                        "name": "contract",
                        "url": "https://example.com/hook",
                        "event_types": ["memory_add"],
                        "is_active": True,
                    }
                ]
            )
        else:
            self._reply({"detail": "not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append(("POST", self.path, payload))
        if self.path == "/api/v1/webhooks/projects/project-1/":
            self._reply(
                {
                    "id": self.webhook_id,
                    "name": payload["name"],
                    "url": payload["url"],
                    "event_types": payload["event_types"],
                    "is_active": True,
                    "secret": "whsec_once",
                },
                201,
            )
        else:
            self._reply({"detail": "not found"}, 404)

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append(("PUT", self.path, payload))
        if self.path == f"/api/v1/webhooks/{self.webhook_id}/":
            self._reply({"message": "Webhook updated successfully"})
        else:
            self._reply({"detail": "not found"}, 404)

    def do_DELETE(self) -> None:
        type(self).calls.append(("DELETE", self.path, {}))
        if self.path == f"/api/v1/webhooks/{self.webhook_id}/":
            self._reply({"message": "Webhook deleted successfully"})
        else:
            self._reply({"detail": "not found"}, 404)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def webhook_server():
    _WebhookHandler.calls.clear()
    server = HTTPServer(("127.0.0.1", 0), _WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_official_client_replays_webhook_routes(webhook_server) -> None:
    from mem0 import MemoryClient

    client = MemoryClient(
        api_key="contract-key", host=f"http://127.0.0.1:{webhook_server.server_port}"
    )
    try:
        created = client.create_webhook(
            url="https://example.com/hook",
            name="contract",
            project_id="project-1",
            event_types=["memory_add"],
        )
        assert created["id"] == _WebhookHandler.webhook_id
        assert client.get_webhooks("project-1")[0]["id"] == _WebhookHandler.webhook_id
        assert client.update_webhook(_WebhookHandler.webhook_id, name="updated")[
            "message"
        ].startswith("Webhook updated")
        assert client.delete_webhook(_WebhookHandler.webhook_id)["message"].startswith(
            "Webhook deleted"
        )
    finally:
        client.client.close()

    paths = {(method, path) for method, path, _ in _WebhookHandler.calls}
    assert ("POST", "/api/v1/webhooks/projects/project-1/") in paths
    assert ("GET", "/api/v1/webhooks/projects/project-1/") in paths
    assert ("PUT", f"/api/v1/webhooks/{_WebhookHandler.webhook_id}/") in paths
    assert ("DELETE", f"/api/v1/webhooks/{_WebhookHandler.webhook_id}/") in paths
