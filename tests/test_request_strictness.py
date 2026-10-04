"""Unknown request fields must fail closed, not silently widen scope.

Pydantic's default is to discard unknown fields. For a multi-tenant store that is
the worst possible failure mode: a caller who misspells `project_id` gets 201 and
their data is written to the *global* scope, readable by any unscoped key.

Verified before the fix:

    POST /v4/facts      {"projct_id": p}  -> 201, stored project_id=None
    POST /v3/documents  {"projectId": p}  -> 201, stored project_id=None
    POST /v1/memories/  {"project_id": p} -> 200, field not modelled at all
"""

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
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


def _stored_project_ids(client: TestClient, table: str) -> list:
    import os

    from memoratum import db
    from memoratum.config import Settings

    conn = db.connect(Settings.load().db_path)
    try:
        return [r["project_id"] for r in conn.execute(f"SELECT project_id FROM {table}")]
    finally:
        conn.close()
        assert os.environ["MEMORATUM_DATA_DIR"]


# --- typos must be rejected -------------------------------------------------


def test_misspelled_scope_field_is_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Typo")
    response = client.post(
        "/v4/facts",
        headers=_admin(),
        json={
            "subject": "user",
            "predicate": "loves",
            "object": "Paris",
            "containerTag": "mem0:user_id:a",
            "projct_id": project_id,
        },
    )
    assert response.status_code == 422, response.text
    assert "projct_id" in response.text
    assert _stored_project_ids(client, "facts") == [], "a rejected write must store nothing"


def test_wrong_case_scope_field_is_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Case")
    response = client.post(
        "/v3/documents",
        headers=_admin(),
        json={
            "content": "x",
            "containerTag": "mem0:user_id:a",
            "projectId": project_id,
        },
    )
    assert response.status_code == 422, response.text
    assert _stored_project_ids(client, "documents") == []


# --- the Mem0 route must accept explicit scope ------------------------------


def test_mem0_add_honours_an_explicit_project_id(tmp_path, monkeypatch):
    """Previously Mem0AddIn had no project_id field at all, so the write landed
    in the global NULL scope while returning 200."""
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Scoped")
    response = client.post(
        "/v1/memories/",
        headers=_admin(),
        json={
            "messages": [{"role": "user", "content": "scoped secret"}],
            "user_id": "alice",
            "infer": False,
            "project_id": project_id,
        },
    )
    assert response.status_code == 200, response.text
    stored = _stored_project_ids(client, "memories")
    assert stored == [project_id], f"expected the write to land in {project_id}, got {stored}"


def test_mem0_search_accepts_scope_fields(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "SearchScope")
    response = client.post(
        "/v3/memories/search/",
        headers=_admin(),
        json={
            "query": "anything",
            "filters": {"user_id": "alice"},
            "project_id": project_id,
        },
    )
    assert response.status_code == 200, response.text


def test_other_unscoped_keys_are_still_readable(tmp_path, monkeypatch):
    """Unscoped writes must keep working -- strictness is about typos, not scope."""
    client = _client(tmp_path, monkeypatch)
    response = client.post(
        "/v1/memories/",
        headers=_admin(),
        json={
            "messages": [{"role": "user", "content": "global"}],
            "user_id": "alice",
            "infer": False,
        },
    )
    assert response.status_code == 200, response.text
    assert _stored_project_ids(client, "memories") == [None]


# --- the escape hatch -------------------------------------------------------


def test_lenient_compat_restores_ignore_extras(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORATUM_LENIENT_COMPAT", "true")
    client = _client(tmp_path, monkeypatch)
    response = client.post(
        "/v4/facts",
        headers=_admin(),
        json={
            "subject": "user",
            "predicate": "likes",
            "object": "tea",
            "containerTag": "mem0:user_id:a",
            "some_client_specific_field": 1,
        },
    )
    assert response.status_code == 201, response.text


def test_strict_is_the_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORATUM_LENIENT_COMPAT", raising=False)
    from memoratum.config import Settings

    assert Settings.load().lenient_compat is False


def test_lenient_compat_is_logged_at_startup(tmp_path, monkeypatch, capsys):
    """A relaxed deployment must never be silent."""
    monkeypatch.setenv("MEMORATUM_LENIENT_COMPAT", "true")
    from memoratum.app import create_app

    TestClient(create_app())
    captured = capsys.readouterr()
    assert "LENIENT" in (captured.out + captured.err).upper(), (
        "enabling lenient compat must announce itself"
    )


# --- structural guarantee ---------------------------------------------------


def test_every_request_model_is_strict():
    import inspect

    from pydantic import BaseModel

    import memoratum.app as appmod

    models = [
        obj
        for _name, obj in vars(appmod).items()
        if inspect.isclass(obj)
        and issubclass(obj, BaseModel)
        and obj is not BaseModel
        and obj.__module__ == appmod.__name__
        and not issubclass(obj, BaseModel) is False
    ]
    assert models, "no request models discovered"
    lenient = [
        m.__name__
        for m in models
        if m.model_config.get("extra") != "forbid" and _is_request_model(m)
    ]
    assert not lenient, f"request models still accepting unknown fields: {lenient}"


def _is_request_model(model) -> bool:
    """Only judge models that carry request-ish fields, not response helpers."""
    fields = set(model.model_fields)
    return bool(fields & {"messages", "content", "subject", "query", "url", "name"})
