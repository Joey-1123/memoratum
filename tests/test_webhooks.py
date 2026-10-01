"""Opt-in webhook delivery contract (RED)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

from fastapi.testclient import TestClient


def _db():
    from memoratum import db

    path = os.path.join(tempfile.mkdtemp(), "memoratum.db")
    return db.connect(path)


def _project(conn, name: str = "Private") -> str:
    import time

    project_id = "project-test-" + name.lower()
    conn.execute(
        "INSERT INTO projects(id, org_id, name, created_at, updated_at) VALUES (?, 'local-org', ?, ?, ?)",
        (project_id, name, time.time(), time.time()),
    )
    conn.commit()
    return project_id


def test_webhook_url_rejects_unsafe_targets_by_default() -> None:
    from memoratum.webhooks import UnsafeWebhookTarget, validate_webhook_url

    for url in (
        "http://example.com/hook",
        "https://127.0.0.1/hook",
        "https://localhost/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.4/hook",
    ):
        try:
            validate_webhook_url(
                url, allow_private=False, resolver=lambda *_args: [("10.0.0.1", 443)]
            )
        except UnsafeWebhookTarget:
            pass
        else:  # pragma: no cover - assertion documents the rejection contract
            raise AssertionError(f"unsafe webhook target was accepted: {url}")


def test_webhook_url_allows_private_targets_only_in_explicit_development_mode() -> None:
    from memoratum.webhooks import validate_webhook_url

    url = validate_webhook_url(
        "http://127.0.0.1:8123/hook",
        allow_private=True,
        resolver=lambda *_args: [("127.0.0.1", 8123)],
    )
    assert url == "http://127.0.0.1:8123/hook"


def test_event_subscription_creates_one_local_delivery_per_matching_webhook() -> None:
    from memoratum import db
    from memoratum.webhooks import create_webhook, list_deliveries

    conn = _db()
    project_id = _project(conn)
    webhook = create_webhook(
        conn,
        project_id=project_id,
        name="Events",
        url="https://example.com/hook",
        event_types=["memory_add"],
        secret="whsec_test",
    )
    memory = db.create_memory(
        conn,
        text="remember this",
        container_tag="mem0:user_id:alice",
        project_id=project_id,
        org_id="local-org",
    )
    deliveries = list_deliveries(conn, project_id=project_id)
    assert len(deliveries) == 1
    assert deliveries[0]["event_type"] == "memory_add"
    assert deliveries[0]["status"] == "queued"
    assert deliveries[0]["webhook_id"] == webhook["id"]
    assert deliveries[0]["memory_id"] == memory["id"]
    conn.close()


class _HookHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, Any]]] = []
    status: ClassVar[int] = 200

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).requests.append(
            {
                "body": json.loads(body),
                "signature": self.headers.get("X-Memoratum-Signature"),
                "timestamp": self.headers.get("X-Memoratum-Timestamp"),
                "delivery": self.headers.get("X-Memoratum-Delivery"),
            }
        )
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args: object) -> None:
        pass


def test_delivery_signs_official_event_shape_and_records_success() -> None:
    from memoratum import db
    from memoratum.webhooks import create_webhook, deliver_delivery, get_delivery_secret

    _HookHandler.requests.clear()
    _HookHandler.status = 200
    server = HTTPServer(("127.0.0.1", 0), _HookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = _db()
    try:
        project_id = _project(conn)
        webhook = create_webhook(
            conn,
            project_id=project_id,
            name="Local",
            url=f"http://127.0.0.1:{server.server_port}/hook",
            event_types=["memory_add"],
            secret="whsec_local",
            allow_private=True,
        )
        memory = db.create_memory(
            conn,
            text="signed memory",
            container_tag="mem0:user_id:alice",
            project_id=project_id,
            org_id="local-org",
        )
        delivery_id = conn.execute(
            "SELECT id FROM webhook_deliveries WHERE webhook_id = ?", (webhook["id"],)
        ).fetchone()["id"]
        result = deliver_delivery(
            conn,
            delivery_id,
            allow_private=True,
            timeout_seconds=2,
            max_response_bytes=1024,
        )
        assert result["status"] == "succeeded"
        assert len(_HookHandler.requests) == 1
        request = _HookHandler.requests[0]
        assert request["body"]["event_details"]["event"] == "ADD"
        assert request["body"]["event_details"]["id"] == memory["id"]
        assert request["body"]["event_details"]["data"]["memory"] == "signed memory"
        assert request["signature"].startswith("sha256=")
        assert request["delivery"] == delivery_id
        assert get_delivery_secret(conn, delivery_id) == "whsec_local"
        row = conn.execute(
            "SELECT status, attempts FROM webhook_deliveries WHERE id = ?", (delivery_id,)
        ).fetchone()
        assert row["status"] == "succeeded"
        assert row["attempts"] == 1
    finally:
        conn.close()
        server.shutdown()
        thread.join(timeout=5)


def test_management_routes_are_opt_in_and_redact_secrets() -> None:
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MEMORATUM_API_KEY"] = "admin-key"
    os.environ["MEMORATUM_WEBHOOK_ALLOW_PRIVATE_TARGETS"] = "true"
    os.environ["MEMORATUM_ENV"] = "test"
    from memoratum.app import create_app

    client = TestClient(create_app())
    headers = {"Authorization": "Token admin-key"}
    project = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=headers,
        json={"name": "Hooked"},
    ).json()
    created = client.post(
        f"/api/v1/webhooks/projects/{project['id']}/",
        headers=headers,
        json={
            "url": "http://127.0.0.1:9999/hook",
            "name": "Local hook",
            "event_types": ["memory_add", "memory_update"],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["secret"].startswith("whsec_")
    webhook_id = body["id"]
    listed = client.get(f"/api/v1/webhooks/projects/{project['id']}/", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["id"] == webhook_id
    assert "secret" not in listed.json()[0]
    fetched = client.get(f"/api/v1/webhooks/{webhook_id}/", headers=headers)
    assert fetched.status_code == 200
    assert "secret" not in fetched.json()
    os.environ.pop("MEMORATUM_WEBHOOK_ALLOW_PRIVATE_TARGETS", None)
    os.environ.pop("MEMORATUM_ENV", None)
