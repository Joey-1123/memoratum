"""Feature-completion regressions for lifecycle and webhook controls."""

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


def _project(client: TestClient, name: str) -> str:
    response = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": name},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_project_delete_is_idempotent_and_cancel_resets_deleting(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Idempotent")
    path = f"/api/v1/orgs/organizations/local-org/projects/{project_id}/"
    first = client.delete(path, headers=_admin())
    second = client.delete(path, headers=_admin())
    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["job_id"] == second.json()["job_id"]

    cancelled = client.post(f"/v4/jobs/{first.json()['job_id']}/cancel", headers=_admin())
    assert cancelled.status_code == 200, cancelled.text
    project = client.get(path, headers=_admin())
    assert project.status_code == 200, project.text
    assert (
        client.post(
            "/v3/documents",
            headers=_admin(),
            json={
                "containerTag": "mem0:user_id:alice",
                "content": "after cancel",
                "project_id": project_id,
            },
        ).status_code
        == 201
    )


def test_categorize_memory_records_category_and_event(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Categorize")
    key = client.post(
        "/v4/keys",
        headers=_admin(),
        json={"containerTag": "mem0:user_id:alice", "project_id": project_id, "role": "OWNER"},
    ).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    added = client.post(
        "/v3/memories/add/",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "categorize me"}],
            "user_id": "alice",
            "infer": False,
        },
    )
    assert added.status_code == 200, added.text
    from memoratum import db

    conn = db.connect(str(tmp_path / "memoratum.db"))
    memory = conn.execute(
        "SELECT id FROM memories WHERE container_tag = ?", ("mem0:user_id:alice",)
    ).fetchone()
    assert memory is not None
    memory_id = str(memory["id"])
    response = client.post(
        f"/v4/memories/{memory_id}/categorize",
        headers=headers,
        json={"category": "preference"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["metadata"]["category"] == "preference"
    event = conn.execute(
        "SELECT event_type FROM domain_events WHERE memory_id = ? ORDER BY created_at DESC",
        (memory_id,),
    ).fetchone()
    assert event is not None and event["event_type"] == "memory_categorize"
    conn.close()


def test_native_memory_lifecycle_aliases_work(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    created = client.post(
        "/v4/memories",
        headers=_admin(),
        json={
            "text": "native memory",
            "containerTag": "mem0:user_id:alice",
            "metadata": {"kind": "note"},
        },
    )
    assert created.status_code == 201, created.text
    memory_id = created.json()["id"]
    assert client.get(f"/v4/memories/{memory_id}", headers=_admin()).status_code == 200
    updated = client.patch(
        f"/v4/memories/{memory_id}",
        headers=_admin(),
        json={"text": "native updated"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["memory"] == "native updated"
    assert client.get(f"/v4/memories/{memory_id}/history", headers=_admin()).status_code == 200
    deleted = client.delete(f"/v4/memories/{memory_id}", headers=_admin())
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] == memory_id


def test_native_bulk_job_reports_per_item_progress(tmp_path, monkeypatch) -> None:
    from memoratum import db, jobs
    from memoratum.config import Settings
    from memoratum.embeddings import HashEmbedder
    from memoratum.worker import run_once

    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Bulk")
    key = client.post(
        "/v4/keys",
        headers=_admin(),
        json={"containerTag": "mem0:user_id:alice", "project_id": project_id, "role": "OWNER"},
    ).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    for content in ("first", "second"):
        assert (
            client.post(
                "/v3/memories/add/",
                headers=headers,
                json={
                    "messages": [{"role": "user", "content": content}],
                    "user_id": "alice",
                    "infer": False,
                },
            ).status_code
            == 200
        )
    conn = db.connect(Settings.load().db_path)
    memories = conn.execute(
        "SELECT id FROM memories WHERE project_id = ? ORDER BY created_at", (project_id,)
    ).fetchall()
    ids = [str(row["id"]) for row in memories]
    assert len(ids) == 2
    response = client.post(
        "/v4/memories/bulk",
        headers=headers,
        json={
            "containerTag": "mem0:user_id:alice",
            "operations": [
                {"memory_id": ids[0], "action": "update", "text": "updated first"},
                {"memory_id": ids[1], "action": "delete"},
            ],
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    for _ in range(10):
        run_once(conn, HashEmbedder(dims=16), None, worker_id="bulk-test")
        if jobs.get(conn, job_id)["status"] == "done":
            break
    job = jobs.get(conn, job_id)
    assert job is not None and job["status"] == "done"
    assert job["result"]["completed"] == 2
    assert [item["status"] for item in job["result"]["items"]] == ["completed", "completed"]
    assert db.get_memory(conn, ids[0])["text"] == "updated first"
    assert db.get_memory(conn, ids[1])["state"] == "deleted"
    conn.close()


def test_ingest_reports_partial_completion_after_index_write_failure(tmp_path, monkeypatch) -> None:
    from memoratum import db
    from memoratum.config import Settings
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_document

    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Partial")
    conn = db.connect(Settings.load().db_path)
    document = db.create_document(
        conn,
        container_tag="mem0:user_id:alice",
        content="partial content",
        project_id=project_id,
        org_id="local-org",
    )

    class FailingStore:
        name = "failing"

        def upsert(self, records):
            raise RuntimeError("vector outage")

    import pytest

    with pytest.raises(RuntimeError, match="vector outage"):
        process_document(conn, HashEmbedder(dims=8), document["id"], vector_store=FailingStore())
    event = conn.execute(
        "SELECT event_type FROM domain_events WHERE project_id = ? ORDER BY id DESC",
        (project_id,),
    ).fetchone()
    assert event is not None
    assert event["event_type"] == "ingest_job_partially_completed"
    conn.close()


def test_webhook_secret_is_encrypted_at_rest(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Encrypted secret")
    created = client.post(
        f"/api/v1/webhooks/projects/{project_id}/",
        headers=_admin(),
        json={"url": "https://example.com/hook", "name": "hook", "event_types": ["memory_add"]},
    )
    assert created.status_code == 201, created.text
    plaintext = created.json()["secret"]
    from memoratum import db

    conn = db.connect(str(tmp_path / "memoratum.db"))
    stored = conn.execute("SELECT secret FROM webhooks").fetchone()["secret"]
    assert stored.startswith("enc:v1:")
    assert plaintext not in stored
    conn.close()


def test_webhook_is_active_can_be_disabled_and_reenabled(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Active")
    created = client.post(
        f"/api/v1/webhooks/projects/{project_id}/",
        headers=_admin(),
        json={"url": "https://example.com/hook", "name": "hook", "event_types": ["memory_add"]},
    )
    assert created.status_code == 201, created.text
    webhook_id = created.json()["id"]
    disabled = client.put(
        f"/api/v1/webhooks/{webhook_id}/",
        headers=_admin(),
        json={"is_active": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["webhook"]["is_active"] is False
    enabled = client.put(
        f"/api/v1/webhooks/{webhook_id}/",
        headers=_admin(),
        json={"is_active": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["webhook"]["is_active"] is True


def test_open_mode_can_manage_projects(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch, admin=False)
    created = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        json={"name": "Open management"},
    )
    assert created.status_code == 201, created.text
    project_id = str(created.json()["id"])
    assert (
        client.get(f"/api/v1/orgs/organizations/local-org/projects/{project_id}/").status_code
        == 200
    )


def test_pgvector_migration_and_score_index_are_real() -> None:
    from memoratum.vectorstore import PgVectorStore

    class Cursor:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def execute(self, query, params=None):
            self.queries.append(query)
            if "SELECT" in query:
                self.rows = [("m1", "memory", "text", "tag", "org", "project", "{}", 12.0, 0.75)]
            return self

        def fetchall(self):
            return getattr(self, "rows", [])

        def rowcount(self):
            return 0

    class Connection:
        def __init__(self) -> None:
            self.cursor_obj = Cursor()

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            pass

        def close(self):
            pass

    connection = Connection()
    store = PgVectorStore(connection, dims=2, table="vectors")
    hits = store.query([1.0, 0.0], container_tag="tag", project_id="project")
    assert hits[0].score == 0.75
    assert hits[0].created_at == 12.0
    assert any(
        "ADD COLUMN IF NOT EXISTS project_id" in query for query in connection.cursor_obj.queries
    )
