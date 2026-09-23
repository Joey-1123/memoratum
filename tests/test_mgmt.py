"""Memory types + management endpoints contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client():
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.pop("MEMORATUM_API_KEY", None)
    from memoratum.app import create_app

    try:
        return TestClient(create_app())
    finally:
        os.environ.pop("MEMORATUM_DATA_DIR", None)


def test_fact_memory_type_roundtrip_and_filter() -> None:
    c = _client()
    r = c.post(
        "/v4/facts",
        json={
            "subject": "s",
            "predicate": "p",
            "object": "o",
            "containerTag": "u1",
            "memory_type": "procedural",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["memory_type"] == "procedural"
    all_facts = c.get("/v4/facts", params={"containerTag": "u1"}).json()
    assert all_facts["total"] == 1
    filtered = c.get("/v4/facts", params={"containerTag": "u1", "memory_type": "episodic"}).json()
    assert filtered["total"] == 0


def test_update_document_reingests() -> None:
    c = _client()
    doc_id = c.post("/v3/documents", json={"content": "v1 text here", "containerTag": "u1"}).json()[
        "id"
    ]
    r = c.patch(f"/v3/documents/{doc_id}", json={"content": "v2 text here"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    assert r.json()["id"] == doc_id
    assert c.patch("/v3/documents/nope", json={"content": "x"}).status_code == 404


def test_list_documents_paginated() -> None:
    c = _client()
    for i in range(3):
        c.post("/v3/documents", json={"content": f"doc number {i} here", "containerTag": "u1"})
    page1 = c.get("/v3/documents", params={"containerTag": "u1", "limit": 2}).json()
    assert page1["total"] == 3 and len(page1["documents"]) == 2
    page2 = c.get("/v3/documents", params={"containerTag": "u1", "limit": 2, "offset": 2}).json()
    assert len(page2["documents"]) == 1
    assert page2["documents"][0]["id"] != page1["documents"][0]["id"]
