"""Key issuance contract (RED)."""

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


def test_admin_can_issue_scoped_key() -> None:
    c = _client()
    denied = c.post("/v4/keys", json={"containerTag": "proj-a"})
    assert denied.status_code == 401
    r = c.post(
        "/v4/keys", json={"containerTag": "proj-a"}, headers={"Authorization": "Bearer admin-key"}
    )
    assert r.status_code == 201, r.text
    scoped = r.json()["key"]
    assert scoped.startswith("mm_")
    ok = c.post(
        "/v3/documents",
        json={"content": "x", "containerTag": "proj-a"},
        headers={"Authorization": f"Bearer {scoped}"},
    )
    assert ok.status_code == 201, ok.text
    wrong_tag = c.post(
        "/v3/documents",
        json={"content": "x", "containerTag": "proj-b"},
        headers={"Authorization": f"Bearer {scoped}"},
    )
    assert wrong_tag.status_code == 403
