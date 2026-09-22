"""Async ingest wiring contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client():
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.pop("MEMORATUM_API_KEY", None)
    from memoratum.app import create_app

    return TestClient(create_app())


def test_create_returns_queued_then_worker_completes() -> None:
    from memoratum import db
    from memoratum.config import Settings
    from memoratum.worker import run_once

    c = _client()
    try:
        r = c.post("/v3/documents", json={"content": "hello world here", "containerTag": "u1"})
        assert r.status_code == 201, r.text
        doc_id = r.json()["id"]
        assert r.json()["status"] == "queued"
        assert r.json()["job_id"]

        from memoratum.app import build_embedder

        conn = db.connect(Settings.load().db_path)
        try:
            assert run_once(conn, build_embedder(Settings.load()), None, worker_id="t") is not None
            while run_once(conn, build_embedder(Settings.load()), None, worker_id="t") is not None:
                pass
            assert db.get_document(conn, doc_id)["status"] == "done"
            job = c.get(f"/v4/jobs/{r.json()['job_id']}")
            assert job.json()["status"] == "done"
        finally:
            conn.close()
    finally:
        os.environ.pop("MEMORATUM_DATA_DIR", None)


def test_jobs_endpoint_auth() -> None:
    c = _client()
    assert c.get("/v4/jobs/abc").status_code == 404
