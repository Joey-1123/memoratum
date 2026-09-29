"""Regression tests for production hardening fixes."""

from __future__ import annotations

import json
import os
import tempfile
import time

from fastapi.testclient import TestClient


def _client() -> TestClient:
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MEMORATUM_API_KEY"] = "admin-key"
    from memoratum.app import create_app

    return TestClient(create_app())


def _admin() -> dict[str, str]:
    return {"Authorization": "Token admin-key"}


def test_replay_requeues_a_durable_delivery_job(tmp_path) -> None:
    from memoratum import db, jobs, webhooks

    conn = db.connect(str(tmp_path / "memoratum.db"))
    now = time.time()
    conn.execute(
        "INSERT INTO projects(id, org_id, name, created_at, updated_at) VALUES (?, 'local-org', ?, ?, ?)",
        ("replay-project", "Replay", now, now),
    )
    conn.commit()
    hook = webhooks.create_webhook(
        conn,
        project_id="replay-project",
        name="Replay",
        url="https://example.com/hook",
        event_types=["memory_add"],
        secret="whsec_replay",
    )
    memory = db.create_memory(
        conn,
        text="replay me",
        container_tag="mem0:user_id:alice",
        project_id="replay-project",
        org_id="local-org",
    )
    delivery_id = conn.execute(
        "SELECT id FROM webhook_deliveries WHERE webhook_id = ?", (hook["id"],)
    ).fetchone()["id"]
    conn.execute(
        "UPDATE webhook_deliveries SET status = 'dead', attempts = 5 WHERE id = ?", (delivery_id,)
    )
    conn.commit()
    result = webhooks.replay_delivery(conn, delivery_id)
    assert result is not None
    row = conn.execute(
        "SELECT status, attempts, next_attempt_at FROM webhook_deliveries WHERE id = ?",
        (delivery_id,),
    ).fetchone()
    assert row["status"] == "queued" and row["attempts"] == 0 and row["next_attempt_at"] is None
    queued = [
        job
        for job in jobs.pending(conn)
        if job["kind"] == "webhook_delivery" and job["payload"]["delivery_id"] == delivery_id
    ]
    assert len(queued) == 1
    assert queued[0]["run_after"] <= time.time()
    assert memory["id"]
    conn.close()


def test_legacy_fact_id_resolves_to_canonical_memory_and_can_be_updated() -> None:
    client = _client()
    from memoratum import db
    from memoratum.config import Settings

    conn = db.connect(Settings.load().db_path)
    from memoratum.facts import add_fact

    fact = add_fact(
        conn,
        container_tag="mem0:user_id:alice",
        subject="user",
        predicate="likes",
        object="Paris",
        document_id=None,
    )
    canonical_id = conn.execute(
        "SELECT id FROM memories WHERE fact_id = ?", (fact["id"],)
    ).fetchone()["id"]
    assert canonical_id.startswith("mem_")
    conn.close()

    fetched = client.get(f"/v1/memories/{fact['id']}/", headers=_admin())
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == canonical_id
    updated = client.put(
        f"/v1/memories/{fact['id']}/", headers=_admin(), json={"text": "user likes Lyon"}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == canonical_id


def test_failed_ingest_records_project_failure_event() -> None:
    from memoratum import db, jobs
    from memoratum.worker import run_once

    conn = db.connect(str(tempfile.mkdtemp() + "/memoratum.db"))
    now = time.time()
    conn.execute(
        "INSERT INTO projects(id, org_id, name, created_at, updated_at) VALUES (?, 'local-org', ?, ?, ?)",
        ("failed-project", "Failed", now, now),
    )
    conn.commit()
    doc = db.create_document(
        conn,
        container_tag="mem0:user_id:alice",
        content="will fail",
        project_id="failed-project",
        org_id="local-org",
    )
    job_id = jobs.enqueue(
        conn,
        kind="ingest",
        payload={"document_id": doc["id"], "project_id": "failed-project"},
    )

    class Failing:
        dims = 8

        def embed(self, texts):
            raise RuntimeError("embedding outage")

    run_once(conn, Failing(), None, worker_id="test")
    events = conn.execute(
        "SELECT event_type FROM domain_events WHERE project_id = ? ORDER BY id", ("failed-project",)
    ).fetchall()
    assert [row["event_type"] for row in events] == ["ingest_job_failed"]
    assert jobs.get(conn, job_id)["status"] in {"queued", "failed"}
    conn.close()


def test_expiry_removes_canonical_memory_and_vector() -> None:
    from memoratum import db
    from memoratum.vectorstore import SQLiteVectorStore, VectorRecord

    conn = db.connect(str(tempfile.mkdtemp() + "/memoratum.db"))
    memory = db.create_memory(
        conn,
        text="expires",
        container_tag="mem0:user_id:alice",
        expires_at=time.time() - 1,
    )
    SQLiteVectorStore(conn).upsert(
        [
            VectorRecord(
                id=memory["id"],
                vector=[1.0, 0.0],
                text="expires",
                kind="memory",
                container_tag=memory["container_tag"],
            )
        ]
    )
    result = db.prune_expired(conn)
    assert result["memories"] == 1
    assert db.get_memory(conn, memory["id"])["state"] == "deleted"
    assert (
        conn.execute("SELECT COUNT(*) FROM vector_points WHERE id = ?", (memory["id"],)).fetchone()[
            0
        ]
        == 0
    )
    assert (
        json.loads(
            conn.execute(
                "SELECT metadata FROM memory_history WHERE memory_id = ? AND event = 'DELETE'",
                (memory["id"],),
            ).fetchone()["metadata"]
        )
        == {}
    )
    conn.close()


def test_project_usage_and_audit_are_filterable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_API_KEY", "admin-key")
    from memoratum.app import create_app

    client = TestClient(create_app())
    project = client.post(
        "/api/v1/orgs/organizations/local-org/projects/", headers=_admin(), json={"name": "Usage"}
    ).json()["id"]
    key = client.post(
        "/v4/keys",
        headers=_admin(),
        json={"containerTag": "mem0:user_id:alice", "project_id": project},
    ).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    assert (
        client.post(
            "/v3/memories/add/",
            headers=headers,
            json={
                "messages": [{"role": "user", "content": "usage"}],
                "user_id": "alice",
                "infer": False,
            },
        ).status_code
        == 200
    )
    usage = client.get("/v4/usage", headers=_admin(), params={"project_id": project})
    assert usage.status_code == 200
    assert usage.json()["usage"] and usage.json()["usage"][0]["projectId"] == project
    audit = client.get("/v4/audit", headers=_admin(), params={"project_id": project})
    assert audit.status_code == 200
    assert audit.json()["events"]
