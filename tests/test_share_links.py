"""Read-only document share links (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client(auth: bool = False) -> TestClient:
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    if auth:
        os.environ["MEMORATUM_API_KEY"] = "test-key"
    else:
        os.environ.pop("MEMORATUM_API_KEY", None)
    from memoratum.app import create_app

    return TestClient(create_app())


def test_share_link_roundtrip_and_revoke() -> None:
    c = _client()
    document = c.post(
        "/v3/documents", json={"content": "Shared private note.", "containerTag": "u1"}
    ).json()
    created = c.post("/v4/share-links", json={"document_id": document["id"], "expires_in": 3600})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["token"].startswith("share_")
    assert body["url"].endswith(body["token"])

    public = c.get(f"/v1/share/{body['token']}")
    assert public.status_code == 200, public.text
    assert public.json()["content"] == "Shared private note."
    assert "metadata" not in public.json()

    revoked = c.delete(f"/v4/share-links/{body['id']}")
    assert revoked.status_code == 200, revoked.text
    assert c.get(f"/v1/share/{body['token']}").status_code == 404


def test_share_link_respects_document_scope_and_validation() -> None:
    c = _client(auth=True)
    headers = {"Authorization": "Bearer test-key"}
    document = c.post(
        "/v3/documents",
        json={"content": "Admin note", "containerTag": "private"},
        headers=headers,
    ).json()
    assert c.post("/v4/share-links", json={"document_id": document["id"]}).status_code == 401
    assert (
        c.post(
            "/v4/share-links", json={"document_id": "missing", "expires_in": 3600}, headers=headers
        ).status_code
        == 404
    )
    assert (
        c.post(
            "/v4/share-links",
            json={"document_id": document["id"], "expires_in": 0},
            headers=headers,
        ).status_code
        == 422
    )
