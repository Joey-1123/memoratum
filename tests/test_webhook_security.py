"""Webhook authorization and secret-handling security tests."""

from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient


def _client() -> tuple[TestClient, dict[str, str]]:
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MEMORATUM_API_KEY"] = "admin-key"
    from memoratum.app import create_app

    return TestClient(create_app()), {"Authorization": "Token admin-key"}


def _project(client: TestClient, admin: dict[str, str], name: str = "Scoped") -> str:
    response = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=admin,
        json={"name": name},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _hook(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        f"/api/v1/webhooks/projects/{project_id}/",
        headers=headers,
        json={
            "url": "https://example.com/hook",
            "name": "hook",
            "event_types": ["memory_add"],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_project_key_cannot_manage_another_projects_webhooks() -> None:
    client, admin = _client()
    project_id = _project(client, admin, "Owned")
    other_id = _project(client, admin, "Other")
    other_hook = _hook(client, admin, other_id)
    key = client.post(
        "/v4/keys",
        headers=admin,
        json={"containerTag": "mem0:user_id:alice", "project_id": project_id},
    ).json()["key"]
    scoped = {"Authorization": f"Bearer {key}"}
    assert client.get(f"/api/v1/webhooks/projects/{other_id}/", headers=scoped).status_code == 403
    assert client.get(f"/api/v1/webhooks/{other_hook}/", headers=scoped).status_code == 403
    assert client.delete(f"/api/v1/webhooks/{other_hook}/", headers=scoped).status_code == 403


def test_webhook_secret_is_only_returned_on_creation_and_rotation() -> None:
    client, admin = _client()
    project_id = _project(client, admin, "Secrets")
    created = client.post(
        f"/api/v1/webhooks/projects/{project_id}/",
        headers=admin,
        json={
            "url": "https://example.com/hook",
            "name": "hook",
            "event_types": ["memory_add"],
        },
    )
    assert created.status_code == 201
    secret = created.json()["secret"]
    webhook_id = created.json()["id"]
    assert secret.startswith("whsec_")

    fetched = client.get(f"/api/v1/webhooks/{webhook_id}/", headers=admin)
    assert "secret" not in fetched.json()
    listed = client.get(f"/api/v1/webhooks/projects/{project_id}/", headers=admin)
    assert all("secret" not in item for item in listed.json())

    rotated = client.post(f"/v4/webhooks/{webhook_id}/rotate-secret", headers=admin)
    assert rotated.status_code == 200, rotated.text
    new_secret = rotated.json()["secret"]
    assert new_secret.startswith("whsec_")
    assert new_secret != secret
    assert "secret" not in client.get(f"/api/v1/webhooks/{webhook_id}/", headers=admin).json()
