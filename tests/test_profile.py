"""Profile endpoint contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def test_profile_returns_facts_and_stats() -> None:
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.pop("MEMORATUM_API_KEY", None)
    from memoratum.app import create_app

    c = TestClient(create_app())
    c.post("/v3/documents", json={"content": "The user loves Paris.", "containerTag": "u1"})
    r = c.get("/v4/profile", params={"containerTag": "u1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["containerTag"] == "u1"
    assert body["stats"]["documents"] == 1
    assert body["stats"]["chunks"] >= 1
    empty = c.get("/v4/profile", params={"containerTag": "nobody"})
    assert empty.json()["stats"]["documents"] == 0
