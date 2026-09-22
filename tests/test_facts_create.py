"""Fact creation contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client():
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.pop("MEMORATUM_API_KEY", None)
    from memoratum.app import create_app

    return TestClient(create_app())


def test_create_fact_returns_full_row() -> None:
    c = _client()
    r = c.post(
        "/v4/facts",
        json={"subject": "a", "predicate": "calls", "object": "b", "containerTag": "g1"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["subject"], body["predicate"], body["object"]) == ("a", "calls", "b")
    again = c.post(
        "/v4/facts",
        json={"subject": "a", "predicate": "calls", "object": "b", "containerTag": "g1"},
    )
    assert again.json()["id"] == body["id"]
    bad = c.post("/v4/facts", json={"subject": "a", "predicate": "calls"})
    assert bad.status_code == 422
    skipped = c.post(
        "/v4/facts",
        json={
            "subject": "x",
            "predicate": "y",
            "object": "z",
            "containerTag": "g1",
            "skipEmbedding": True,
        },
    )
    assert skipped.status_code == 201, skipped.text
