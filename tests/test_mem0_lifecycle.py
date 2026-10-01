"""Mem0 memory lifecycle contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client() -> TestClient:
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MEMORATUM_API_KEY"] = "admin-key"
    os.environ.pop("MEMORATUM_LLM_ENDPOINT", None)
    os.environ.pop("MEMORATUM_LLM_MODEL", None)
    from memoratum.app import create_app

    return TestClient(create_app())


def _add(client: TestClient, *, text: str = "I live in Paris.", user_id: str = "alice") -> str:
    from helpers import drain

    response = client.post(
        "/v3/memories/add/",
        headers={"Authorization": "Token admin-key"},
        json={"messages": [{"role": "user", "content": text}], "user_id": user_id},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PENDING"
    drain()
    listed = client.get(
        "/v1/memories/",
        headers={"Authorization": "Token admin-key"},
        params={"user_id": user_id},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["results"]
    return str(listed.json()["results"][0]["id"])


def test_get_update_history_delete_round_trip() -> None:
    client = _client()
    headers = {"Authorization": "Token admin-key"}
    memory_id = _add(client)

    fetched = client.get(f"/v1/memories/{memory_id}/", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == memory_id
    assert fetched.json()["memory"] == "I live in Paris."

    updated = client.put(
        f"/v1/memories/{memory_id}/",
        headers=headers,
        json={
            "text": "I live in London.",
            "metadata": {"reason": "moved", "user_id": "attacker"},
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == memory_id
    assert updated.json()["memory"] == "I live in London."
    assert updated.json()["metadata"] == {"reason": "moved"}
    assert updated.json()["version"] == 2

    history = client.get(f"/v1/memories/{memory_id}/history/", headers=headers)
    assert history.status_code == 200, history.text
    assert [item["event"] for item in history.json()] == ["ADD", "UPDATE"]
    assert history.json()[1]["old_memory"] == "I live in Paris."
    assert history.json()[1]["new_memory"] == "I live in London."

    searched = client.post(
        "/v3/memories/search/",
        headers=headers,
        json={"query": "London", "filters": {"user_id": "alice"}, "top_k": 5},
    )
    assert searched.status_code == 200, searched.text
    assert searched.json()["results"][0]["id"] == memory_id
    assert searched.json()["results"][0]["memory"] == "I live in London."

    deleted = client.delete(f"/v1/memories/{memory_id}/", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["message"] == "Memory deleted successfully!"

    assert client.get(f"/v1/memories/{memory_id}/", headers=headers).status_code == 404
    assert (
        client.get("/v1/memories/", headers=headers, params={"user_id": "alice"}).json()["results"]
        == []
    )
    history = client.get(f"/v1/memories/{memory_id}/history/", headers=headers)
    assert history.status_code == 200, history.text
    assert history.json()[-1]["event"] == "DELETE"
    assert history.json()[-1]["old_memory"] is None
    assert history.json()[-1]["new_memory"] is None
    assert history.json()[-1]["content_hash"]


def test_get_all_v3_uses_filters_and_returns_canonical_ids() -> None:
    client = _client()
    headers = {"Authorization": "Token admin-key"}
    memory_id = _add(client, text="I love tennis.", user_id="bob")

    response = client.post(
        "/v3/memories/",
        headers=headers,
        json={"filters": {"user_id": "bob"}, "page": 1, "page_size": 10},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    assert body["results"][0]["id"] == memory_id
    assert body["results"][0]["memory"] == "I love tennis."


def test_memory_routes_hide_other_scopes() -> None:
    client = _client()
    admin = {"Authorization": "Bearer admin-key"}
    memory_id = _add(client, text="Private note", user_id="alice")

    scoped = client.post(
        "/v4/keys",
        headers=admin,
        json={"containerTag": "mem0:user_id:bob"},
    )
    assert scoped.status_code == 201, scoped.text
    key = scoped.json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get(f"/v1/memories/{memory_id}/", headers=headers).status_code == 404
    assert (
        client.put(f"/v1/memories/{memory_id}/", headers=headers, json={"text": "leak"}).status_code
        == 404
    )
    assert client.delete(f"/v1/memories/{memory_id}/", headers=headers).status_code == 404
