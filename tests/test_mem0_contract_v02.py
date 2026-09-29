"""Mem0 v0.2 fixture validation (RED)."""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "mem0" / "v0.2"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_v02_manifest_and_fixtures_are_complete() -> None:
    from memoratum.mem0_contract import CONTRACT_VERSION

    manifest = _fixture("manifest.json")
    assert manifest["contract"] == CONTRACT_VERSION
    assert manifest["contract"] == "mem0-self-hosted-v0.2"
    for name in manifest["fixtures"]:
        assert (FIXTURES / name).is_file(), name


def test_v02_request_and_response_validators_cover_lifecycle() -> None:
    from memoratum.mem0_contract import (
        validate_add_request,
        validate_batch_request,
        validate_get_all_request,
        validate_response,
        validate_update_request,
        validate_webhook_request,
    )

    validate_webhook_request(_fixture("webhook_create_request.json"))

    validate_add_request(_fixture("add_request.json"))
    validate_update_request(_fixture("update_request.json"))
    validate_batch_request(_fixture("batch_update_request.json"), update=True)
    validate_batch_request(_fixture("batch_delete_request.json"), update=False)
    validate_get_all_request(_fixture("get_all_request.json"))
    for name, kind in (
        ("add_response.json", "add"),
        ("get_response.json", "get"),
        ("update_response.json", "get"),
        ("delete_response.json", "delete"),
        ("delete_all_response.json", "delete_all"),
        ("history_response.json", "history"),
        ("batch_update_response.json", "batch"),
        ("batch_delete_response.json", "batch"),
        ("get_all_response.json", "get_all"),
        ("ping_response.json", "ping"),
        ("error_response.json", "error"),
        ("webhook_create_response.json", "webhook"),
        ("webhook_event_payload.json", "webhook_event"),
    ):
        validate_response(_fixture(name), kind=kind)


def test_v02_validation_rejects_unsafe_batch_and_missing_scope() -> None:
    from memoratum.mem0_contract import validate_batch_request, validate_get_all_request

    with pytest.raises(ValueError, match="1000"):
        validate_batch_request({"memories": [{"memory_id": "x", "text": "y"}] * 1001}, update=True)
    with pytest.raises(ValueError, match="memory_id"):
        validate_batch_request({"memories": [{"text": "y"}]}, update=True)
    with pytest.raises(ValueError, match="entity"):
        validate_get_all_request({"filters": {}})
