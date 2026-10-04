"""Adversarial regressions for tenant isolation and durable job semantics."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_API_KEY", "admin-key")
    from memoratum.app import create_app

    return TestClient(create_app())


def _admin() -> dict[str, str]:
    return {"Authorization": "Token admin-key"}


def _project(client: TestClient, name: str) -> str:
    response = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": name},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _tag_key(client: TestClient, tag: str = "mem0:user_id:alice") -> dict[str, str]:
    response = client.post(
        "/v4/keys",
        headers=_admin(),
        json={"containerTag": tag},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['key']}"}


def test_tag_scoped_key_cannot_manage_projects(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Victim")
    headers = _tag_key(client)
    base = f"/api/v1/orgs/organizations/local-org/projects/{project_id}"

    assert client.get(f"{base}/", headers=headers).status_code == 403
    assert client.patch(f"{base}/", headers=headers, json={"name": "stolen"}).status_code == 403
    assert client.get(f"{base}/members/", headers=headers).status_code == 403
    assert (
        client.post(
            f"{base}/members/", headers=headers, json={"email": "attacker@example.test"}
        ).status_code
        == 403
    )
    assert client.delete(f"{base}/", headers=headers).status_code == 403


def test_oidc_role_claim_without_membership_cannot_manage_project(tmp_path, monkeypatch) -> None:
    from memoratum.app import create_app
    from memoratum.auth import Identity, StaticIdentityProvider

    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_API_KEY", "admin-key")
    provider = StaticIdentityProvider(
        {
            "not-a-member": Identity(
                subject="not-a-member@example.test",
                email="not-a-member@example.test",
                role="OWNER",
            )
        }
    )
    client = TestClient(create_app(identity_provider=provider))
    project_id = _project(client, "Protected")
    headers = {"Authorization": "Bearer not-a-member"}
    base = f"/api/v1/orgs/organizations/local-org/projects/{project_id}"

    assert client.get(f"{base}/", headers=headers).status_code == 403
    assert client.patch(f"{base}/", headers=headers, json={"name": "stolen"}).status_code == 403
    assert client.delete(f"{base}/", headers=headers).status_code == 403


def test_unscoped_tag_purge_does_not_cross_project_boundaries(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_a = _project(client, "Alpha")
    project_b = _project(client, "Beta")
    from memoratum import db

    conn = db.connect(str(tmp_path / "memoratum.db"))
    now = time.time()
    for project_id, text in ((project_a, "alpha"), (project_b, "beta")):
        conn.execute(
            "INSERT INTO documents(id, container_tag, content, status, created_at, updated_at,"
            " org_id, project_id) VALUES (?, 'mem0:user_id:alice', ?, 'queued', ?, ?, ?, ?)",
            (f"doc-{project_id}", text, now, now, "local-org", project_id),
        )
    conn.commit()
    conn.close()

    headers = _tag_key(client)
    response = client.delete("/v4/tags/mem0:user_id:alice", headers=headers)
    assert response.status_code == 200, response.text

    conn = db.connect(str(tmp_path / "memoratum.db"))
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 2
    finally:
        conn.close()


def test_purge_project_job_is_visible_and_marks_project_deleting(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Disposable")
    response = client.delete(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/", headers=_admin()
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    status = client.get(f"/v4/jobs/{job_id}", headers=_admin())
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "queued"

    write = client.post(
        "/v3/documents",
        headers=_admin(),
        json={
            "containerTag": "mem0:user_id:alice",
            "content": "too late",
            "project_id": project_id,
        },
    )
    assert write.status_code == 409, write.text


def test_replay_does_not_reset_a_running_delivery_job(tmp_path, monkeypatch) -> None:
    from memoratum import db, webhooks

    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_API_KEY", "admin-key")
    conn = db.connect(str(tmp_path / "memoratum.db"))
    now = time.time()
    conn.execute(
        "INSERT INTO projects(id, org_id, name, created_at, updated_at) VALUES"
        " ('replay-running', 'local-org', 'Replay', ?, ?)",
        (now, now),
    )
    conn.commit()
    hook = webhooks.create_webhook(
        conn,
        project_id="replay-running",
        name="Replay",
        url="https://example.com/hook",
        event_types=["memory_add"],
        secret="whsec_running",
    )
    db.create_memory(
        conn,
        text="running",
        container_tag="mem0:user_id:alice",
        project_id="replay-running",
        org_id="local-org",
    )
    delivery_id = conn.execute(
        "SELECT id FROM webhook_deliveries WHERE webhook_id = ?", (hook["id"],)
    ).fetchone()["id"]
    payload = json.dumps({"delivery_id": delivery_id}, separators=(",", ":"))
    # Model a LIVE claim: a live lease is what makes this job untouchable. A raw
    # `status='running'` with no lease means a DEAD worker, which the reaper is
    # required to recover. See tests/test_jobs_lease.py for that other half.
    conn.execute(
        "UPDATE jobs SET status = 'running', worker = 'worker-1',"
        " lease_expires_at = ?, heartbeat_at = ?"
        " WHERE kind = 'webhook_delivery' AND payload = ?",
        (time.time() + 300, time.time(), payload),
    )
    conn.commit()

    assert webhooks.replay_delivery(conn, delivery_id) is None
    row = conn.execute(
        "SELECT status, worker FROM jobs WHERE kind = 'webhook_delivery' AND payload = ?",
        (payload,),
    ).fetchone()
    assert row["status"] == "running"
    assert row["worker"] == "worker-1"
    conn.close()


def test_cancelled_ingest_reaches_terminal_event_state_and_document_state(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Cancel")
    key = client.post(
        "/v4/keys",
        headers=_admin(),
        json={"containerTag": "mem0:user_id:alice", "project_id": project_id, "role": "OWNER"},
    ).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    document = client.post(
        "/v3/documents",
        headers=headers,
        json={"containerTag": "mem0:user_id:alice", "content": "cancel me"},
    )
    assert document.status_code == 201, document.text
    job_id = document.json()["job_id"]
    cancelled = client.post(f"/v4/jobs/{job_id}/cancel", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    event = client.get(f"/v1/event/{job_id}/", headers=headers)
    assert event.status_code == 200, event.text
    assert event.json()["status"] == "FAILED"

    from memoratum import db

    conn = db.connect(str(tmp_path / "memoratum.db"))
    try:
        row = conn.execute(
            "SELECT status FROM documents WHERE id = ?", (document.json()["id"],)
        ).fetchone()
        assert row["status"] == "cancelled"
    finally:
        conn.close()
