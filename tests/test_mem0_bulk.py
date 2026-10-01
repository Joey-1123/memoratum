"""Mem0 batch and scoped delete-all contract (RED)."""

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


def _add(client: TestClient, text: str, user_id: str) -> str:
    from helpers import drain

    response = client.post(
        "/v3/memories/add/",
        headers={"Authorization": "Token admin-key"},
        json={"messages": [{"role": "user", "content": text}], "user_id": user_id},
    )
    assert response.status_code == 200, response.text
    drain()
    listed = client.get(
        "/v1/memories/",
        headers={"Authorization": "Token admin-key"},
        params={"user_id": user_id},
    )
    assert listed.status_code == 200, listed.text
    matches = [item for item in listed.json()["results"] if item["memory"] == text]
    assert len(matches) == 1, listed.text
    return str(matches[0]["id"])


def test_batch_update_is_atomic_and_records_each_history() -> None:
    client = _client()
    headers = {"Authorization": "Token admin-key"}
    first = _add(client, "I like tea", "alice")
    second = _add(client, "I like coffee", "alice")

    response = client.put(
        "/v1/batch/",
        headers=headers,
        json={
            "memories": [
                {"memory_id": first, "text": "I like green tea"},
                {"memory_id": second, "metadata": {"drink": "coffee"}},
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"message": "Successfully updated 2 memories"}

    first_history = client.get(f"/v1/memories/{first}/history/", headers=headers)
    second_history = client.get(f"/v1/memories/{second}/history/", headers=headers)
    assert first_history.status_code == 200, first_history.text
    assert second_history.status_code == 200, second_history.text
    assert [row["event"] for row in first_history.json()] == ["ADD", "UPDATE"]
    assert first_history.json()[-1]["new_memory"] == "I like green tea"
    assert second_history.json()[-1]["metadata"] == {"drink": "coffee"}


def test_batch_update_validates_every_item_before_writing() -> None:
    client = _client()
    headers = {"Authorization": "Token admin-key"}
    memory_id = _add(client, "Original", "alice")

    response = client.put(
        "/v1/batch/",
        headers=headers,
        json={
            "memories": [
                {"memory_id": memory_id, "text": "Changed"},
                {"memory_id": "mem_missing", "text": "Missing"},
            ]
        },
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert client.get(f"/v1/memories/{memory_id}/", headers=headers).json()["memory"] == "Original"


def test_batch_delete_removes_all_requested_memories() -> None:
    client = _client()
    headers = {"Authorization": "Token admin-key"}
    first = _add(client, "Delete me one", "alice")
    second = _add(client, "Delete me two", "alice")

    response = client.request(
        "DELETE",
        "/v1/batch/",
        headers=headers,
        json={"memories": [{"memory_id": first}, {"memory_id": second}]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"message": "Successfully deleted 2 memories"}
    assert client.get(f"/v1/memories/{first}/", headers=headers).status_code == 404
    assert client.get(f"/v1/memories/{second}/", headers=headers).status_code == 404


def test_delete_all_requires_a_filter_and_is_asynchronous() -> None:
    client = _client()
    headers = {"Authorization": "Token admin-key"}
    alice = _add(client, "Alice memory", "alice")
    bob = _add(client, "Bob memory", "bob")

    missing = client.delete("/v1/memories/", headers=headers)
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "VALIDATION_ERROR"

    response = client.delete("/v1/memories/", headers=headers, params={"user_id": "alice"})
    assert response.status_code == 200, response.text
    assert response.json()["event_id"]
    assert "progress" in response.json()["message"].lower()

    from helpers import drain

    drain()
    event = client.get(f"/v1/event/{response.json()['event_id']}/", headers=headers)
    assert event.status_code == 200, event.text
    assert event.json()["status"] == "SUCCEEDED"
    assert client.get(f"/v1/memories/{alice}/", headers=headers).status_code == 404
    assert client.get(f"/v1/memories/{bob}/", headers=headers).status_code == 200


def test_batch_rejects_oversized_and_duplicate_requests() -> None:
    client = _client()
    headers = {"Authorization": "Token admin-key"}
    memory_id = _add(client, "Only one", "alice")

    oversized = client.put(
        "/v1/batch/",
        headers=headers,
        json={"memories": [{"memory_id": memory_id, "text": "x"} for _ in range(1001)]},
    )
    assert oversized.status_code == 422, oversized.text
    assert oversized.json()["error"]["code"] == "VALIDATION_ERROR"

    duplicate = client.put(
        "/v1/batch/",
        headers=headers,
        json={
            "memories": [
                {"memory_id": memory_id, "text": "one"},
                {"memory_id": memory_id, "text": "two"},
            ]
        },
    )
    assert duplicate.status_code == 422, duplicate.text
    assert duplicate.json()["error"]["code"] == "VALIDATION_ERROR"
