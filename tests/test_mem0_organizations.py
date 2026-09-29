"""Local organization/project compatibility contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client() -> TestClient:
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MEMORATUM_API_KEY"] = "admin-key"
    from memoratum.app import create_app

    return TestClient(create_app())


def _admin() -> dict[str, str]:
    return {"Authorization": "Token admin-key"}


def test_ping_seeds_a_default_local_tenant() -> None:
    client = _client()
    response = client.get("/v1/ping/", headers=_admin())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["org_id"] == "local-org"
    assert body["project_id"] == "local-project"


def test_project_compatibility_routes_manage_projects_and_members() -> None:
    client = _client()
    created = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": "Research", "description": "Local research project"},
    )
    assert created.status_code == 201, created.text
    project = created.json()
    project_id = project["id"]
    assert project["org_id"] == "local-org"

    fetched = client.get(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/", headers=_admin()
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["name"] == "Research"

    updated = client.patch(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/",
        headers=_admin(),
        json={"custom_instructions": "Prefer concise memories."},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["custom_instructions"] == "Prefer concise memories."

    members = client.get(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/members/", headers=_admin()
    )
    assert members.status_code == 200, members.text
    assert members.json()["members"] == []

    added = client.post(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/members/",
        headers=_admin(),
        json={"email": "reader@example.test", "role": "READER"},
    )
    assert added.status_code == 201, added.text
    assert added.json()["role"] == "READER"
    assert "email" in added.json()

    updated_member = client.put(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/members/",
        headers=_admin(),
        json={"email": "reader@example.test", "role": "OWNER"},
    )
    assert updated_member.status_code == 200, updated_member.text
    assert updated_member.json()["role"] == "OWNER"

    removed = client.delete(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/members/",
        headers=_admin(),
        params={"email": "reader@example.test"},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed"] is True


def test_project_scoped_keys_isolate_memory_reads() -> None:
    client = _client()
    created = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": "Private"},
    ).json()
    project_id = created["id"]
    key = client.post(
        "/v4/keys",
        headers=_admin(),
        json={"containerTag": "mem0:user_id:alice", "project_id": project_id},
    ).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    added = client.post(
        "/v3/memories/add/",
        headers=headers,
        json={"messages": [{"role": "user", "content": "Project secret"}], "user_id": "alice"},
    )
    assert added.status_code == 200, added.text
    from helpers import drain

    drain()
    found = client.post(
        "/v3/memories/search/",
        headers=headers,
        json={"query": "secret", "filters": {"user_id": "alice"}},
    )
    assert found.status_code == 200, found.text
    assert found.json()["results"]

    other = client.post(
        "/v4/keys",
        headers=_admin(),
        json={"containerTag": "mem0:user_id:alice", "project_id": "another-project"},
    ).json()["key"]
    other_headers = {"Authorization": f"Bearer {other}"}
    assert (
        client.post(
            "/v3/memories/search/",
            headers=other_headers,
            json={"query": "secret", "filters": {"user_id": "alice"}},
        ).json()["results"]
        == []
    )
    assert (
        client.get(
            "/v1/memories/",
            headers=other_headers,
            params={"user_id": "alice"},
        ).json()["results"]
        == []
    )


def test_project_scoped_key_cannot_write_to_another_tag() -> None:
    client = _client()
    project_id = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": "Bound"},
    ).json()["id"]
    key = client.post(
        "/v4/keys",
        headers=_admin(),
        json={"containerTag": "mem0:user_id:alice", "project_id": project_id},
    ).json()["key"]
    response = client.post(
        "/v3/memories/add/",
        headers={"Authorization": f"Bearer {key}"},
        json={"messages": [{"role": "user", "content": "nope"}], "user_id": "bob"},
    )
    assert response.status_code == 403
