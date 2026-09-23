"""Org isolation groundwork contract (RED)."""

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


def test_org_isolation() -> None:
    c = _client()
    ka = c.post("/v4/keys", json={"containerTag": "t", "org_id": "org-a"}, headers=_admin()).json()[
        "key"
    ]
    ha = {"Authorization": f"Bearer {ka}"}
    r = c.post(
        "/v3/documents",
        json={"content": "hello world", "containerTag": "t", "org_id": "org-b"},
        headers=ha,
    )
    assert r.status_code == 403
    ok = c.post(
        "/v3/documents",
        json={"content": "hello world", "containerTag": "t", "org_id": "org-a"},
        headers=ha,
    )
    assert ok.status_code == 201, ok.text
    doc_id = ok.json()["id"]
    assert c.get(f"/v3/documents/{doc_id}", headers=ha).status_code == 200
    kb = c.post("/v4/keys", json={"containerTag": "t", "org_id": "org-b"}, headers=_admin()).json()[
        "key"
    ]
    hb = {"Authorization": f"Bearer {kb}"}
    assert c.get(f"/v3/documents/{doc_id}", headers=hb).status_code == 404
    assert c.get(f"/v3/documents/{doc_id}", headers=_admin()).status_code == 200


def test_default_org_is_shared_null() -> None:
    c = _client()
    r = c.post(
        "/v3/documents", json={"content": "open hello", "containerTag": "t"}, headers=_admin()
    )
    assert r.status_code == 201, r.text
    k = c.post("/v4/keys", json={"containerTag": "t"}, headers=_admin()).json()["key"]
    assert (
        c.get(
            f"/v3/documents/{r.json()['id']}", headers={"Authorization": f"Bearer {k}"}
        ).status_code
        == 200
    )


def test_custom_ids_are_unique_within_each_org() -> None:
    c = _client()
    a = c.post(
        "/v3/documents",
        json={"content": "alpha", "containerTag": "t", "customId": "shared", "org_id": "org-a"},
        headers=_admin(),
    )
    b = c.post(
        "/v3/documents",
        json={"content": "beta", "containerTag": "t", "customId": "shared", "org_id": "org-b"},
        headers=_admin(),
    )
    assert a.status_code == 201, a.text
    assert b.status_code == 201, b.text
    assert a.json()["id"] != b.json()["id"]


def test_scoped_key_defaults_to_its_org_for_document_reads() -> None:
    c = _client()
    a = c.post(
        "/v3/documents",
        json={"content": "alpha private", "containerTag": "t", "org_id": "org-a"},
        headers=_admin(),
    ).json()["id"]
    c.post(
        "/v3/documents",
        json={"content": "beta private", "containerTag": "t", "org_id": "org-b"},
        headers=_admin(),
    )
    key = c.post(
        "/v4/keys", json={"containerTag": "t", "org_id": "org-a"}, headers=_admin()
    ).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}

    listed = c.get("/v3/documents", params={"containerTag": "t"}, headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["documents"][0]["id"] == a
    assert (
        c.get(
            "/v3/documents", params={"containerTag": "t", "org_id": "org-b"}, headers=headers
        ).status_code
        == 403
    )


def test_scoped_key_scopes_facts_and_search() -> None:
    c = _client()
    c.post(
        "/v4/facts",
        json={
            "subject": "user",
            "predicate": "likes",
            "object": "alpha",
            "containerTag": "t",
            "org_id": "org-a",
        },
        headers=_admin(),
    )
    c.post(
        "/v4/facts",
        json={
            "subject": "user",
            "predicate": "likes",
            "object": "beta",
            "containerTag": "t",
            "org_id": "org-b",
        },
        headers=_admin(),
    )
    c.post(
        "/v3/documents",
        json={"content": "alpha private", "containerTag": "t", "org_id": "org-a"},
        headers=_admin(),
    )
    c.post(
        "/v3/documents",
        json={"content": "beta private", "containerTag": "t", "org_id": "org-b"},
        headers=_admin(),
    )
    key = c.post(
        "/v4/keys", json={"containerTag": "t", "org_id": "org-a"}, headers=_admin()
    ).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}

    facts = c.get("/v4/facts", params={"containerTag": "t"}, headers=headers)
    assert facts.status_code == 200, facts.text
    assert [f["object"] for f in facts.json()["facts"]] == ["alpha"]
    assert (
        c.get(
            "/v4/facts", params={"containerTag": "t", "org_id": "org-b"}, headers=headers
        ).status_code
        == 403
    )

    from helpers import drain

    drain()
    found = c.post(
        "/v4/search",
        json={"q": "private", "containerTag": "t", "searchMode": "hybrid"},
        headers=headers,
    )
    assert found.status_code == 200, found.text
    assert found.json()["results"]
    assert all("alpha" in h.get("chunk", h.get("memory", "")) for h in found.json()["results"])
    assert all("beta" not in h.get("chunk", h.get("memory", "")) for h in found.json()["results"])


def test_scoped_key_scopes_profile_purge_and_jobs() -> None:
    c = _client()
    a_doc = c.post(
        "/v3/documents",
        json={"content": "alpha private", "containerTag": "t", "org_id": "org-a"},
        headers=_admin(),
    ).json()
    b_doc = c.post(
        "/v3/documents",
        json={"content": "beta private", "containerTag": "t", "org_id": "org-b"},
        headers=_admin(),
    ).json()
    c.post(
        "/v4/facts",
        json={
            "subject": "a",
            "predicate": "p",
            "object": "alpha",
            "containerTag": "t",
            "org_id": "org-a",
        },
        headers=_admin(),
    )
    c.post(
        "/v4/facts",
        json={
            "subject": "b",
            "predicate": "p",
            "object": "beta",
            "containerTag": "t",
            "org_id": "org-b",
        },
        headers=_admin(),
    )
    key = c.post(
        "/v4/keys", json={"containerTag": "t", "org_id": "org-a"}, headers=_admin()
    ).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}

    profile = c.get("/v4/profile", params={"containerTag": "t"}, headers=headers)
    assert profile.status_code == 200, profile.text
    assert profile.json()["stats"] == {"documents": 1, "chunks": 0, "facts": 1}
    assert c.get(f"/v4/jobs/{a_doc['job_id']}", headers=headers).status_code == 200
    assert c.get(f"/v4/jobs/{b_doc['job_id']}", headers=headers).status_code == 404

    purged = c.delete("/v4/tags/t", headers=headers)
    assert purged.status_code == 200, purged.text
    assert purged.json()["documents"] == 1
    assert (
        c.get(
            "/v4/profile", params={"containerTag": "t", "org_id": "org-b"}, headers=_admin()
        ).json()["stats"]["documents"]
        == 1
    )
