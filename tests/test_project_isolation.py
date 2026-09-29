"""Adversarial project-isolation regressions for native and worker surfaces."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_API_KEY", "admin-key")
    from memoratum.app import create_app

    return TestClient(create_app())


def _admin() -> dict[str, str]:
    return {"Authorization": "Token admin-key"}


def _project(client: TestClient, name: str) -> str:
    response = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": name},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _key(client: TestClient, project_id: str, tag: str = "mem0:user_id:alice") -> dict[str, str]:
    response = client.post(
        "/v4/keys",
        headers=_admin(),
        json={"containerTag": tag, "project_id": project_id},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['key']}"}


def test_native_documents_facts_search_and_jobs_are_project_isolated(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_a = _project(client, "Alpha")
    project_b = _project(client, "Beta")
    headers_a = _key(client, project_a)
    headers_b = _key(client, project_b)

    document_a = client.post(
        "/v3/documents",
        headers=headers_a,
        json={"containerTag": "mem0:user_id:alice", "content": "alpha private note"},
    )
    document_b = client.post(
        "/v3/documents",
        headers=headers_b,
        json={"containerTag": "mem0:user_id:alice", "content": "beta private note"},
    )
    assert document_a.status_code == 201, document_a.text
    assert document_b.status_code == 201, document_b.text
    assert document_a.json()["id"] != document_b.json()["id"]
    assert (
        client.get(f"/v3/documents/{document_a.json()['id']}", headers=headers_a).status_code == 200
    )
    assert (
        client.get(f"/v3/documents/{document_a.json()['id']}", headers=headers_b).status_code == 404
    )
    assert (
        client.get(
            "/v3/documents", headers=headers_a, params={"containerTag": "mem0:user_id:alice"}
        ).json()["total"]
        == 1
    )
    assert (
        client.get(
            "/v3/documents", headers=headers_b, params={"containerTag": "mem0:user_id:alice"}
        ).json()["total"]
        == 1
    )

    from helpers import drain

    drain()
    search_a = client.post(
        "/v4/search",
        headers=headers_a,
        json={"q": "private", "containerTag": "mem0:user_id:alice"},
    )
    search_b = client.post(
        "/v4/search",
        headers=headers_b,
        json={"q": "private", "containerTag": "mem0:user_id:alice"},
    )
    assert search_a.status_code == 200 and search_b.status_code == 200
    assert "alpha" in str(search_a.json())
    assert "beta" not in str(search_a.json())
    assert "beta" in str(search_b.json())
    assert "alpha" not in str(search_b.json())

    assert (
        client.get("/v4/jobs/" + document_a.json()["job_id"], headers=headers_a).status_code == 200
    )
    assert (
        client.get("/v4/jobs/" + document_b.json()["job_id"], headers=headers_a).status_code == 404
    )


def test_project_scoped_fact_writes_and_lists_do_not_cross_projects(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_a = _project(client, "Fact Alpha")
    project_b = _project(client, "Fact Beta")
    headers_a = _key(client, project_a)
    headers_b = _key(client, project_b)
    body_a = {
        "containerTag": "mem0:user_id:alice",
        "subject": "user",
        "predicate": "likes",
        "object": "alpha",
    }
    body_b = {**body_a, "object": "beta"}
    assert client.post("/v4/facts", headers=headers_a, json=body_a).status_code == 201
    assert client.post("/v4/facts", headers=headers_b, json=body_b).status_code == 201

    listed_a = client.get(
        "/v4/facts", headers=headers_a, params={"containerTag": "mem0:user_id:alice"}
    )
    listed_b = client.get(
        "/v4/facts", headers=headers_b, params={"containerTag": "mem0:user_id:alice"}
    )
    assert listed_a.status_code == 200 and listed_b.status_code == 200
    assert [fact["object"] for fact in listed_a.json()["facts"]] == ["alpha"]
    assert [fact["object"] for fact in listed_b.json()["facts"]] == ["beta"]


def test_custom_ids_are_isolated_per_project(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_a = _project(client, "Custom Alpha")
    project_b = _project(client, "Custom Beta")
    headers_a = _key(client, project_a)
    headers_b = _key(client, project_b)
    body = {"containerTag": "mem0:user_id:alice", "customId": "same", "content": "content"}
    first = client.post("/v3/documents", headers=headers_a, json=body)
    second = client.post("/v3/documents", headers=headers_b, json=body)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


def test_purge_is_project_scoped_and_removes_canonical_memories(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    project_a = _project(client, "Purge Alpha")
    project_b = _project(client, "Purge Beta")
    headers_a = _key(client, project_a)
    headers_b = _key(client, project_b)
    for headers, text in ((headers_a, "alpha memory"), (headers_b, "beta memory")):
        response = client.post(
            "/v3/memories/add/",
            headers=headers,
            json={
                "messages": [{"role": "user", "content": text}],
                "user_id": "alice",
                "infer": False,
            },
        )
        assert response.status_code == 200, response.text

    from helpers import drain

    drain()
    purged = client.delete("/v4/tags/mem0:user_id:alice", headers=headers_a)
    assert purged.status_code == 200, purged.text
    headers_a = _key(client, project_a)
    listed_a = client.get("/v1/memories/", headers=headers_a, params={"user_id": "alice"})
    assert listed_a.status_code == 200, listed_a.text
    assert listed_a.json()["results"] == []
    assert (
        len(
            client.get("/v1/memories/", headers=headers_b, params={"user_id": "alice"}).json()[
                "results"
            ]
        )
        == 1
    )


def test_vector_records_filter_by_project() -> None:
    from memoratum.vectorstore import InMemoryVectorStore, VectorRecord

    store = InMemoryVectorStore()
    store.upsert(
        [
            VectorRecord(
                id="a",
                vector=[1.0, 0.0],
                text="alpha",
                kind="chunk",
                container_tag="t",
                project_id="project-a",
            ),
            VectorRecord(
                id="b",
                vector=[1.0, 0.0],
                text="beta",
                kind="chunk",
                container_tag="t",
                project_id="project-b",
            ),
        ]
    )
    assert [
        hit.id for hit in store.query([1.0, 0.0], container_tag="t", project_id="project-a")
    ] == ["a"]
    assert store.delete(container_tag="t", project_id="project-a") == 1
    assert [hit.id for hit in store.query([1.0, 0.0], container_tag="t")] == ["b"]
