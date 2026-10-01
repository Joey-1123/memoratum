"""Follow-up hardening regressions for scope, deletion, and lifecycle."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch, *, admin: bool = True) -> TestClient:
    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    if admin:
        monkeypatch.setenv("MEMORATUM_API_KEY", "admin-key")
    else:
        monkeypatch.delenv("MEMORATUM_API_KEY", raising=False)
    from memoratum.app import create_app

    return TestClient(create_app())


def _admin() -> dict[str, str]:
    return {"Authorization": "Token admin-key"}


def test_native_fact_delete_removes_canonical_memory_and_vector(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    from memoratum import db
    from memoratum.config import Settings
    from memoratum.facts import add_fact
    from memoratum.vectorstore import SQLiteVectorStore, VectorRecord

    conn = db.connect(Settings.load().db_path)
    fact = add_fact(
        conn,
        container_tag="mem0:user_id:alice",
        subject="user",
        predicate="likes",
        object="Paris",
        document_id=None,
    )
    memory = conn.execute("SELECT * FROM memories WHERE fact_id = ?", (fact["id"],)).fetchone()
    assert memory is not None
    SQLiteVectorStore(conn).upsert(
        [
            VectorRecord(
                id=memory["id"],
                vector=[1.0, 0.0],
                text=memory["text"],
                kind="memory",
                container_tag=memory["container_tag"],
                org_id=memory["org_id"],
                project_id=memory["project_id"],
            )
        ]
    )
    conn.close()

    response = client.delete(f"/v4/memories/{fact['id']}", headers=_admin())
    assert response.status_code == 200, response.text
    conn = db.connect(Settings.load().db_path)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM memories WHERE id = ?", (memory["id"],)).fetchone()[
                0
            ]
            == 1
        )
        assert (
            conn.execute("SELECT state FROM memories WHERE id = ?", (memory["id"],)).fetchone()[0]
            == "deleted"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM vector_points WHERE id = ?", (memory["id"],)
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_open_mode_project_document_derives_project_org(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch, admin=False)
    project_response = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers={"Authorization": "Bearer mm_open_mode"},
        json={"name": "Open"},
    )
    assert project_response.status_code == 201, project_response.text
    project_id = str(project_response.json()["id"])
    response = client.post(
        "/v3/documents",
        json={
            "containerTag": "mem0:user_id:alice",
            "content": "open scoped",
            "project_id": project_id,
        },
    )
    assert response.status_code == 201, response.text

    from memoratum import db
    from memoratum.config import Settings

    conn = db.connect(Settings.load().db_path)
    try:
        row = conn.execute("SELECT org_id, project_id FROM documents").fetchone()
        assert row["org_id"] == "local-org"
        assert row["project_id"] == project_id
    finally:
        conn.close()


def test_official_project_update_accepts_sdk_scope_fields_and_delete(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": "SDK"},
    ).json()
    path = f"/api/v1/orgs/organizations/local-org/projects/{project['id']}/"
    updated = client.patch(
        path,
        headers=_admin(),
        json={
            "name": "Updated",
            "org_id": "local-org",
            "project_id": project["id"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Updated"
    deleted = client.delete(path, headers=_admin())
    assert deleted.status_code == 202, deleted.text
    assert deleted.json()["job_id"]
    assert client.get(path, headers=_admin()).status_code == 200


def test_project_owner_can_cancel_queued_ingest(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": "Cancel"},
    ).json()
    project_id = project["id"]
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
    assert cancelled.json()["status"] == "cancelled"
    status = client.get(f"/v4/jobs/{job_id}", headers=headers)
    assert status.status_code == 200 and status.json()["status"] == "cancelled"
