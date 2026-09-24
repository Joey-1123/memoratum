"""Mem0-compatible API shim contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client() -> TestClient:
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MEMORATUM_API_KEY"] = "admin-key"
    from memoratum.app import create_app

    return TestClient(create_app())


def test_mem0_add_event_and_search_compat() -> None:
    c = _client()
    headers = {"Authorization": "Token admin-key"}
    added = c.post(
        "/v3/memories/add/",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "I moved to Paris."}],
            "user_id": "alice",
            "metadata": {"source": "test"},
        },
    )
    assert added.status_code == 200, added.text
    event = added.json()["event_id"]
    assert added.json()["status"] == "PENDING"

    from helpers import drain

    drain()
    status = c.get(f"/v1/event/{event}/", headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "SUCCEEDED"

    searched = c.post(
        "/v3/memories/search/",
        headers=headers,
        json={"query": "Paris", "filters": {"user_id": "alice"}, "top_k": 5},
    )
    assert searched.status_code == 200, searched.text
    assert searched.json()["results"]


def test_mem0_alias_requires_entity_filter() -> None:
    c = _client()
    response = c.post(
        "/v1/memories/search/",
        headers={"Authorization": "Token admin-key"},
        json={"query": "anything", "filters": {}},
    )
    assert response.status_code == 422
