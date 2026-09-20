"""Forget API contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client():
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MEMORATUM_API_KEY"] = "admin-key"
    from memoratum.app import create_app

    try:
        return TestClient(create_app())
    finally:
        os.environ.pop("MEMORATUM_API_KEY", None)


def _admin():
    return {"Authorization": "Bearer admin-key"}


def test_forget_fact_removes_it_everywhere() -> None:
    from memoratum import db
    from memoratum.config import Settings
    from memoratum.facts import add_fact, list_facts

    c = _client()
    conn = db.connect(Settings.load().db_path)
    f = add_fact(
        conn,
        container_tag="u1",
        subject="user",
        predicate="loves",
        object="Paris",
        document_id=None,
    )
    conn.close()
    r = c.delete(f"/v4/memories/{f['id']}", headers=_admin())
    assert r.status_code == 200, r.text
    conn = db.connect(Settings.load().db_path)
    try:
        assert list_facts(conn, "u1", include_superseded=True) == []
    finally:
        conn.close()
    assert c.delete(f"/v4/memories/{f['id']}", headers=_admin()).status_code == 404


def test_purge_tag_wipes_only_that_tag() -> None:
    c = _client()
    c.post("/v3/documents", json={"content": "bye", "containerTag": "gone"}, headers=_admin())
    c.post("/v3/documents", json={"content": "stay", "containerTag": "kept"}, headers=_admin())
    r = c.delete("/v4/tags/gone", headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["documents"] == 1
    assert (
        c.get("/v4/profile", params={"containerTag": "gone"}, headers=_admin()).json()["stats"][
            "documents"
        ]
        == 0
    )
    assert (
        c.get("/v4/profile", params={"containerTag": "kept"}, headers=_admin()).json()["stats"][
            "documents"
        ]
        == 1
    )


def test_scoped_key_cannot_purge_other_tag() -> None:
    c = _client()
    scoped = c.post("/v4/keys", json={"containerTag": "mine"}, headers=_admin()).json()["key"]
    h = {"Authorization": f"Bearer {scoped}"}
    assert c.delete("/v4/tags/other", headers=h).status_code == 404
    assert c.delete("/v4/tags/mine", headers=h).status_code == 200


def test_revoked_key_stops_working() -> None:
    c = _client()
    scoped = c.post("/v4/keys", json={"containerTag": "u1"}, headers=_admin()).json()["key"]
    h = {"Authorization": f"Bearer {scoped}"}
    assert (
        c.post("/v3/documents", json={"content": "x", "containerTag": "u1"}, headers=h).status_code
        == 201
    )
    r = c.post("/v4/keys/revoke", json={"key": scoped}, headers=_admin())
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert (
        c.post("/v3/documents", json={"content": "y", "containerTag": "u1"}, headers=h).status_code
        == 401
    )
