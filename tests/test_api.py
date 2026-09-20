"""API contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client(auth: bool = False):
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    if auth:
        os.environ["MEMORATUM_API_KEY"] = "test-key"
    else:
        os.environ.pop("MEMORATUM_API_KEY", None)
    from memoratum.app import create_app

    return TestClient(create_app())


def test_ingest_then_search_roundtrip() -> None:
    c = _client()
    r = c.post("/v3/documents", json={"content": "The cat sat on the mat.", "containerTag": "u1"})
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    s = c.get(f"/v3/documents/{doc_id}")
    assert s.json()["status"] == "done"
    q = c.post("/v4/search", json={"q": "cat mat", "containerTag": "u1", "limit": 5})
    assert q.status_code == 200, q.text
    body = q.json()
    assert body["total"] >= 1
    assert "cat" in body["results"][0]["chunk"]


def test_validation_and_errors() -> None:
    c = _client()
    assert c.post("/v3/documents", json={}).status_code == 422
    assert c.post("/v3/documents", json={"content": 123}).status_code == 422
    assert c.get("/v3/documents/nope").status_code == 404
    assert c.get("/v3/documents/nope").json()["error"]["code"] == "NOT_FOUND"


def test_bearer_auth_and_scoping() -> None:
    c = _client(auth=True)
    assert c.post("/v3/documents", json={"content": "x", "containerTag": "u1"}).status_code == 401
    authed = {"Authorization": "Bearer test-key"}
    r = c.post("/v3/documents", json={"content": "secret", "containerTag": "u1"}, headers=authed)
    assert r.status_code == 201, r.text
    q = c.post("/v4/search", json={"q": "secret", "containerTag": "u1"}, headers=authed)
    assert q.status_code == 200
