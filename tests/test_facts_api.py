"""Fact listing contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client():
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.pop("MEMORATUM_API_KEY", None)
    from memoratum.app import create_app

    return TestClient(create_app())


def test_list_facts_with_filters_and_history() -> None:
    from memoratum import db
    from memoratum.config import Settings
    from memoratum.facts import add_fact

    c = _client()
    c.post("/v3/documents", json={"content": "The user loves Paris.", "containerTag": "u1"})

    conn = db.connect(Settings.load().db_path)
    try:
        add_fact(
            conn,
            container_tag="u1",
            subject="user",
            predicate="works_at",
            object="Tencent",
            document_id=None,
        )
        add_fact(
            conn,
            container_tag="u1",
            subject="user",
            predicate="works_at",
            object="Moonshot",
            document_id=None,
        )
        add_fact(conn, container_tag="u2", subject="a", predicate="b", object="c", document_id=None)
    finally:
        conn.close()
    r = c.get("/v4/facts", params={"containerTag": "u1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["facts"][0]["object"] == "Moonshot"
    full = c.get("/v4/facts", params={"containerTag": "u1", "include_superseded": "true"})
    assert full.json()["total"] == 2
    other = c.get("/v4/facts", params={"containerTag": "u2"})
    assert other.json()["total"] == 1
    missing = c.get("/v4/facts")
    assert missing.status_code == 422
