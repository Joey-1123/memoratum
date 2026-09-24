"""Versioned Mem0 self-hosted compatibility contract tests."""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "mem0" / "v0.1"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_fixture_manifest_matches_contract_version() -> None:
    from memoratum.mem0_contract import CONTRACT_VERSION

    manifest = _fixture("manifest.json")
    assert manifest["contract"] == CONTRACT_VERSION
    for name in manifest["fixtures"]:
        assert (FIXTURES / name).is_file()


def test_contract_fixtures_validate_against_the_local_profile() -> None:
    from memoratum.mem0_contract import (
        validate_add_request,
        validate_list_query,
        validate_response,
        validate_search_request,
    )

    validate_add_request(_fixture("add_request.json"))
    validate_search_request(_fixture("search_request.json"))
    validate_list_query(_fixture("list_query.json"))
    validate_response(_fixture("add_response.json"), kind="add")
    validate_response(_fixture("event_response.json"), kind="event")
    validate_response(_fixture("search_response.json"), kind="search")
    validate_response(_fixture("list_response.json"), kind="list")
    error = _fixture("error_response.json")
    validate_response(error, kind="error")


def test_contract_rejects_missing_entity_scope() -> None:
    from memoratum.mem0_contract import validate_add_request, validate_search_request

    with pytest.raises(ValueError, match="entity"):
        validate_add_request({"messages": [{"role": "user", "content": "hello"}]})
    with pytest.raises(ValueError, match="entity"):
        validate_search_request({"query": "hello", "filters": {}})


def test_add_rejects_ambiguous_scope_forms() -> None:
    from memoratum.mem0_contract import validate_add_request

    with pytest.raises(ValueError, match="containerTag"):
        validate_add_request(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "user_id": "user-1",
                "containerTag": "mem0:user_id:user-1",
            }
        )
    with pytest.raises(ValueError, match="namespace"):
        validate_add_request(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "containerTag": "user-1",
            }
        )
    with pytest.raises(ValueError, match="namespace"):
        validate_add_request(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "containerTag": "mem0",
            }
        )
    with pytest.raises(ValueError, match="exactly one"):
        validate_add_request(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "user_id": "user-1",
                "agent_id": "agent-1",
            }
        )


def test_capability_matrix_distinguishes_supported_and_unsupported_features() -> None:
    from memoratum.mem0_contract import CAPABILITIES, CAPABILITY_STATUSES

    by_name = {item["name"]: item for item in CAPABILITIES}
    assert by_name["add_memories"]["status"] == "supported"
    assert by_name["search_memories"]["status"] == "supported"
    assert by_name["update_memory"]["status"] == "planned"
    assert by_name["python_sdk_add_search"]["status"] == "supported"
    assert by_name["typescript_sdk_add_search"]["status"] == "supported"
    assert by_name["managed_billing"]["status"] == "out_of_scope"
    assert all(item["status"] in CAPABILITY_STATUSES for item in CAPABILITIES)


def test_fixture_add_and_search_replay_against_compat_routes(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_API_KEY", "contract-admin")
    from memoratum.app import create_app

    client = TestClient(create_app())
    headers = {"Authorization": "Token contract-admin"}
    add_response = client.post(
        "/v3/memories/add/", headers=headers, json=_fixture("add_request.json")
    )
    assert add_response.status_code == 200, add_response.text

    from helpers import drain

    drain()
    event_id = add_response.json()["event_id"]
    event = client.get(f"/v1/event/{event_id}/", headers=headers)
    assert event.status_code == 200
    assert event.json()["status"] == "SUCCEEDED"

    search_response = client.post(
        "/v3/memories/search/", headers=headers, json=_fixture("search_request.json")
    )
    assert search_response.status_code == 200, search_response.text
    assert search_response.json()["results"]

    from memoratum.mem0_contract import validate_add_request, validate_response

    validate_add_request(_fixture("add_request.json"))
    validate_response(add_response.json(), kind="add")
    validate_response(event.json(), kind="event")
    validate_response(search_response.json(), kind="search")

    listed = client.get("/v1/memories/", headers=headers, params=_fixture("list_query.json"))
    assert listed.status_code == 200, listed.text
    validate_response(listed.json(), kind="list")
