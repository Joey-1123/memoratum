"""Hardening slice B contract (RED)."""

import os
import tempfile


def _db():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_add_fact_returns_full_row() -> None:
    from memoratum.facts import add_fact

    conn = _db()
    f = add_fact(conn, container_tag="u1", subject="a", predicate="b", object="c", document_id=None)
    assert (f["subject"], f["predicate"], f["container_tag"]) == ("a", "b", "u1")
    conn.close()


def test_cross_tag_document_is_404_not_403() -> None:
    from fastapi.testclient import TestClient

    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MEMORATUM_API_KEY"] = "admin-key"
    try:
        from memoratum.app import create_app

        c = TestClient(create_app())
        admin = {"Authorization": "Bearer admin-key"}
        other = c.post(
            "/v3/documents", json={"content": "x", "containerTag": "proj-b"}, headers=admin
        ).json()["id"]
        scoped = c.post("/v4/keys", json={"containerTag": "proj-a"}, headers=admin).json()["key"]
        h = {"Authorization": f"Bearer {scoped}"}
        assert c.get(f"/v3/documents/{other}", headers=h).status_code == 404
        assert c.get("/v3/documents/nope", headers=h).status_code == 404
        assert c.get(f"/v3/documents/{other}").status_code == 401
    finally:
        os.environ.pop("MEMORATUM_API_KEY", None)


def test_malformed_dims_env_fails_fast_with_message() -> None:
    os.environ["MEMORATUM_EMBEDDINGS_DIMS"] = "abc"
    try:
        from memoratum.config import Settings

        try:
            Settings.load()
        except ValueError as e:
            assert "MEMORATUM_EMBEDDINGS_DIMS" in str(e)
            return
        raise AssertionError("expected ValueError")
    finally:
        os.environ.pop("MEMORATUM_EMBEDDINGS_DIMS", None)


def test_dimension_change_marks_doc_failed() -> None:
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_one

    conn = _db()
    db.create_document(conn, container_tag="u1", content="first")
    process_one(conn, HashEmbedder(dims=8))
    db.create_document(conn, container_tag="u1", content="second")
    process_one(conn, HashEmbedder(dims=16))
    statuses = [
        r["status"]
        for r in conn.execute("SELECT status FROM documents ORDER BY created_at").fetchall()
    ]
    assert statuses == ["done", "failed"]
    conn.close()
