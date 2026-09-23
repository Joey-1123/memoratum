"""Audit and usage API contract (RED)."""

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


def test_usage_and_audit_are_admin_reads_over_local_key_activity() -> None:
    c = _client()
    scoped = c.post(
        "/v4/keys", json={"containerTag": "t", "org_id": "org-a"}, headers=_admin()
    ).json()["key"]
    scoped_headers = {"Authorization": f"Bearer {scoped}"}
    created = c.post(
        "/v3/documents",
        json={"content": "private alpha", "containerTag": "t"},
        headers=scoped_headers,
    )
    assert created.status_code == 201, created.text
    assert (
        c.get("/v4/profile", params={"containerTag": "t"}, headers=scoped_headers).status_code
        == 200
    )

    usage = c.get("/v4/usage", params={"containerTag": "t", "org_id": "org-a"}, headers=_admin())
    assert usage.status_code == 200, usage.text
    rows = usage.json()["usage"]
    scoped_row = next(row for row in rows if row["containerTag"] == "t" and row["orgId"] == "org-a")
    assert scoped_row["documentWrites"] == 1
    assert scoped_row["requests"] >= 2
    assert len(scoped_row["keyFingerprint"]) == 16
    assert scoped not in str(usage.json())

    audit = c.get("/v4/audit", params={"containerTag": "t", "org_id": "org-a"}, headers=_admin())
    assert audit.status_code == 200, audit.text
    events = audit.json()["events"]
    assert any(event["action"] == "document.created" for event in events)
    assert any(event["action"] == "key.issued" for event in events)
    assert all("private alpha" not in str(event) for event in events)
    assert all(scoped not in str(event) for event in events)
    assert c.get("/v4/audit", headers=scoped_headers).status_code == 403
    assert c.get("/v4/usage", headers=scoped_headers).status_code == 403
    assert c.get("/v4/audit").status_code == 401
    assert c.get("/v4/usage").status_code == 401
