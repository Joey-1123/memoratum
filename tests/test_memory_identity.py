"""Canonical memory identity contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client() -> TestClient:
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MEMORATUM_API_KEY"] = "admin-key"
    from memoratum.app import create_app

    return TestClient(create_app())


def test_mem0_search_and_list_return_the_same_canonical_memory_id() -> None:
    from helpers import drain

    client = _client()
    headers = {"Authorization": "Token admin-key"}
    added = client.post(
        "/v3/memories/add/",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "I live in Paris."}],
            "user_id": "alice",
        },
    )
    assert added.status_code == 200, added.text
    drain()

    searched = client.post(
        "/v3/memories/search/",
        headers=headers,
        json={"query": "Paris", "filters": {"user_id": "alice"}, "top_k": 5},
    )
    listed = client.get("/v1/memories/", headers=headers, params={"user_id": "alice"})
    assert searched.status_code == 200, searched.text
    assert listed.status_code == 200, listed.text

    memory_hits = [item for item in searched.json()["results"] if "memory" in item]
    assert memory_hits
    assert memory_hits[0]["id"].startswith("mem_")
    listed_by_id = {item["id"]: item for item in listed.json()["results"]}
    assert memory_hits[0]["id"] in listed_by_id
    assert listed_by_id[memory_hits[0]["id"]]["memory"] == memory_hits[0]["memory"]


def test_canonical_memory_round_trips_and_records_add_history() -> None:
    from memoratum import db
    from memoratum.config import Settings
    from memoratum.mem0_contract import validate_response

    client = _client()
    headers = {"Authorization": "Token admin-key"}
    added = client.post(
        "/v3/memories/add/",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "I moved to Paris."}],
            "user_id": "alice",
            "metadata": {"source": "test"},
        },
    )
    assert added.status_code == 200, added.text
    validate_response(added.json(), kind="add")

    from helpers import drain

    drain()
    listed = client.get("/v1/memories/", headers=headers, params={"user_id": "alice"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["results"]

    conn = db.connect(Settings.load().db_path)
    try:
        memory_id = listed.json()["results"][0]["id"]
        assert memory_id.startswith("mem_")
        history = db.list_memory_history(conn, memory_id, container_tag="mem0:user_id:alice")
        assert [row["event"] for row in history] == ["ADD"]
        assert history[0]["new_text"] == listed.json()["results"][0]["memory"]
    finally:
        conn.close()
