"""Webhook retry, size-limit, and event-lifecycle regression tests."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

from memoratum import db, jobs


def _db():
    path = os.path.join(tempfile.mkdtemp(), "memoratum.db")
    return db.connect(path)


def _project(conn, name: str = "Retry") -> str:
    project_id = "project-" + name.lower()
    now = time.time()
    conn.execute(
        "INSERT INTO projects(id, org_id, name, created_at, updated_at) VALUES (?, 'local-org', ?, ?, ?)",
        (project_id, name, now, now),
    )
    conn.commit()
    return project_id


class _FailingHook(BaseHTTPRequestHandler):
    requests: ClassVar[list[bytes]] = []
    response_size: ClassVar[int] = 0
    status: ClassVar[int] = 500

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        type(self).requests.append(self.rfile.read(length))
        body = b"x" * type(self).response_size
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


def _server() -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(("127.0.0.1", 0), _FailingHook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _create_local_webhook(conn, url: str, project_id: str):
    from memoratum.webhooks import create_webhook

    return create_webhook(
        conn,
        project_id=project_id,
        name="Failing",
        url=url,
        event_types=["memory_add", "memory_update", "memory_delete"],
        secret="whsec_retry_test",
        allow_private=True,
    )


def test_failed_delivery_requeues_job_with_backoff() -> None:
    from memoratum.app import build_embedder
    from memoratum.config import Settings
    from memoratum.worker import run_once

    _FailingHook.requests.clear()
    _FailingHook.status = 500
    _FailingHook.response_size = 0
    server, thread = _server()
    conn = _db()
    try:
        project_id = _project(conn)
        _create_local_webhook(conn, f"http://127.0.0.1:{server.server_port}/hook", project_id)
        db.create_memory(
            conn,
            text="retry me",
            container_tag="mem0:user_id:alice",
            project_id=project_id,
            org_id="local-org",
        )
        queued = jobs.pending(conn)
        assert len(queued) == 1
        job_id = queued[0]["id"]
        result = run_once(
            conn,
            build_embedder(Settings.load()),
            None,
            worker_id="test",
            webhook_allow_private_targets=True,
        )
        assert result == job_id
        assert jobs.get(conn, job_id)["status"] == "done"
        delivery_id = conn.execute("SELECT id FROM webhook_deliveries").fetchone()["id"]
        assert (
            conn.execute(
                "SELECT status FROM webhook_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()["status"]
            == "queued"
        )
        retry_jobs = [
            job
            for job in jobs.pending(conn)
            if job["kind"] == "webhook_delivery" and job["payload"]["delivery_id"] == delivery_id
        ]
        assert len(retry_jobs) == 1
        assert retry_jobs[0]["run_after"] > time.time()
    finally:
        conn.close()
        server.shutdown()
        thread.join(timeout=5)


def test_oversized_webhook_response_is_not_accepted() -> None:
    from memoratum.webhooks import deliver_delivery

    _FailingHook.requests.clear()
    _FailingHook.status = 200
    _FailingHook.response_size = 128
    server, thread = _server()
    conn = _db()
    try:
        project_id = _project(conn)
        _create_local_webhook(conn, f"http://127.0.0.1:{server.server_port}/hook", project_id)
        db.create_memory(
            conn,
            text="bounded",
            container_tag="mem0:user_id:alice",
            project_id=project_id,
            org_id="local-org",
        )
        delivery_id = conn.execute("SELECT id FROM webhook_deliveries").fetchone()["id"]
        result = deliver_delivery(
            conn,
            delivery_id,
            allow_private=True,
            timeout_seconds=2,
            max_response_bytes=16,
            max_attempts=1,
        )
        assert result["status"] == "dead"
        row = conn.execute(
            "SELECT status, response_status FROM webhook_deliveries WHERE id = ?", (delivery_id,)
        ).fetchone()
        assert row["status"] == "dead"
        assert row["response_status"] == 200
    finally:
        conn.close()
        server.shutdown()
        thread.join(timeout=5)


def test_memory_update_and_delete_each_create_distinct_deliveries() -> None:
    from memoratum.webhooks import list_deliveries

    _FailingHook.requests.clear()
    _FailingHook.status = 200
    _FailingHook.response_size = 0
    server, thread = _server()
    conn = _db()
    try:
        project_id = _project(conn)
        _create_local_webhook(conn, f"http://127.0.0.1:{server.server_port}/hook", project_id)
        memory = db.create_memory(
            conn,
            text="before",
            container_tag="mem0:user_id:alice",
            project_id=project_id,
            org_id="local-org",
        )
        db.update_memory(conn, memory["id"], text="after")
        conn.commit()
        db.soft_delete_memory(conn, memory["id"])
        conn.commit()
        deliveries = list_deliveries(conn, project_id=project_id)
        assert [item["event_type"] for item in deliveries] == [
            "memory_add",
            "memory_update",
            "memory_delete",
        ]
        assert len({item["memory_id"] for item in deliveries}) == 1
        payloads = [
            json.loads(row["payload"])
            for row in conn.execute(
                "SELECT payload FROM webhook_deliveries ORDER BY created_at, id"
            )
        ]
        assert payloads[1]["event_details"]["data"]["memory"] == "after"
        assert "memory" not in payloads[2]["event_details"]["data"]
    finally:
        conn.close()
        server.shutdown()
        thread.join(timeout=5)
