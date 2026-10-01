"""Project role and tenant-consistency regressions."""

from __future__ import annotations

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


def _key(client: TestClient, project_id: str, *, role: str = "OWNER") -> dict[str, str]:
    response = client.post(
        "/v4/keys",
        headers=_admin(),
        json={
            "containerTag": "mem0:user_id:alice",
            "project_id": project_id,
            "role": role,
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['key']}"}


def test_reader_key_can_read_but_cannot_manage_project_or_webhooks(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Reader Project")
    reader = _key(client, project_id, role="READER")

    assert (
        client.get(
            f"/api/v1/orgs/organizations/local-org/projects/{project_id}/", headers=reader
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/orgs/organizations/local-org/projects/{project_id}/",
            headers=reader,
            json={"name": "forbidden"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/orgs/organizations/local-org/projects/{project_id}/members/", headers=reader
        ).status_code
        == 403
    )
    assert client.get(f"/api/v1/webhooks/projects/{project_id}/", headers=reader).status_code == 403
    assert (
        client.post(
            "/v3/documents",
            headers=reader,
            json={"containerTag": "mem0:user_id:alice", "content": "forbidden"},
        ).status_code
        == 403
    )


def test_project_key_cannot_be_issued_for_unknown_or_mismatched_project(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Consistent Project")
    unknown = client.post(
        "/v4/keys",
        headers=_admin(),
        json={"containerTag": "mem0:user_id:alice", "project_id": "missing-project"},
    )
    assert unknown.status_code == 404
    mismatched = client.post(
        "/v4/keys",
        headers=_admin(),
        json={
            "containerTag": "mem0:user_id:alice",
            "project_id": project_id,
            "org_id": "another-org",
        },
    )
    assert mismatched.status_code == 422


def test_project_scope_derives_and_enforces_organization(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Org Project")
    headers = _key(client, project_id)
    response = client.post(
        "/v3/documents",
        headers=headers,
        json={"containerTag": "mem0:user_id:alice", "content": "scoped"},
    )
    assert response.status_code == 201, response.text
    from memoratum import db

    conn = db.connect(str(tmp_path / "memoratum.db"))
    try:
        row = conn.execute("SELECT org_id, project_id FROM documents").fetchone()
        assert row["org_id"] == "local-org"
        assert row["project_id"] == project_id
    finally:
        conn.close()


def test_oidc_project_owner_can_manage_project(tmp_path, monkeypatch) -> None:
    from memoratum.app import create_app
    from memoratum.auth import Identity, StaticIdentityProvider

    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_API_KEY", "admin-key")
    provider = StaticIdentityProvider(
        {
            "owner-token": Identity(
                subject="owner@example.test",
                email="owner@example.test",
                project_id=None,
            )
        }
    )
    client = TestClient(create_app(identity_provider=provider))
    project_id = _project(client, "OIDC Project")
    # Bind the verified subject as an owner after provisioning the project.
    admin = _admin()
    members = client.post(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/members/",
        headers=admin,
        json={"email": "owner@example.test", "role": "OWNER"},
    )
    assert members.status_code == 201, members.text
    response = client.patch(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/",
        headers={"Authorization": "Bearer owner-token"},
        json={"name": "managed by owner"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "managed by owner"
