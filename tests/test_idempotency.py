"""Idempotent state-changing compatibility routes (RED)."""

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


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": "Token admin-key", "Idempotency-Key": key}


def test_add_replays_same_key_and_rejects_a_different_hash() -> None:
    client = _client()
    payload = {
        "messages": [{"role": "user", "content": "I live in Paris."}],
        "user_id": "alice",
    }

    first = client.post("/v3/memories/add/", headers=_headers("add-1"), json=payload)
    second = client.post("/v3/memories/add/", headers=_headers("add-1"), json=payload)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json() == first.json()

    conflict = client.post(
        "/v3/memories/add/",
        headers=_headers("add-1"),
        json={**payload, "messages": [{"role": "user", "content": "I live in London."}]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    from memoratum import db
    from memoratum.config import Settings

    conn = db.connect(Settings.load().db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()[0] == 1
    finally:
        conn.close()


def test_update_replays_without_creating_duplicate_history() -> None:
    client = _client()
    added = client.post(
        "/v3/memories/add/",
        headers=_headers("add-update"),
        json={
            "messages": [{"role": "user", "content": "I live in Paris."}],
            "user_id": "alice",
        },
    )
    assert added.status_code == 200, added.text
    from helpers import drain

    drain()
    listed = client.get(
        "/v1/memories/", headers={"Authorization": "Token admin-key"}, params={"user_id": "alice"}
    )
    memory_id = listed.json()["results"][0]["id"]

    body = {"text": "I live in London."}
    first = client.put(f"/v1/memories/{memory_id}/", headers=_headers("update-1"), json=body)
    second = client.put(f"/v1/memories/{memory_id}/", headers=_headers("update-1"), json=body)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json() == first.json()

    conflict = client.put(
        f"/v1/memories/{memory_id}/",
        headers=_headers("update-1"),
        json={"text": "I live in Rome."},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    history = client.get(
        f"/v1/memories/{memory_id}/history/", headers={"Authorization": "Token admin-key"}
    )
    assert [row["event"] for row in history.json()] == ["ADD", "UPDATE"]


def test_claim_is_scoped_and_only_one_concurrent_claim_wins() -> None:
    import threading

    from memoratum import db

    path = os.path.join(tempfile.mkdtemp(), "claims.db")
    db.connect(path).close()
    results: list[str] = []
    barrier = threading.Barrier(2)

    def claim() -> None:
        conn = db.connect(path)
        try:
            barrier.wait(timeout=5)
            result = db.claim_idempotency(
                conn,
                scope="admin-key",
                key="same",
                request_hash="hash-1",
                expires_in=3600,
            )
            results.append(result.status)
        finally:
            conn.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(results) == ["claimed", "in_flight"]
