"""OIDC identity-provider seam contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def test_oidc_settings_are_loaded_without_enabling_a_verifier() -> None:
    from memoratum.config import Settings

    names = ("MEMORATUM_OIDC_ISSUER", "MEMORATUM_OIDC_AUDIENCE", "MEMORATUM_OIDC_JWKS_URL")
    values = ("https://issuer.example", "memoratum", "https://issuer.example/jwks")
    for name, value in zip(names, values):
        os.environ[name] = value
    try:
        settings = Settings.load()
        assert settings.oidc_issuer == values[0]
        assert settings.oidc_audience == values[1]
        assert settings.oidc_jwks_url == values[2]
    finally:
        for name in names:
            os.environ.pop(name, None)


def test_external_identity_maps_to_local_scope_and_audit() -> None:
    from memoratum.app import create_app
    from memoratum.auth import Identity, StaticIdentityProvider

    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MEMORATUM_API_KEY"] = "admin-key"
    provider = StaticIdentityProvider(
        {"jwt-token": Identity(subject="alice", container_tag="u1", org_id="org-a")}
    )
    client = TestClient(create_app(identity_provider=provider))
    headers = {"Authorization": "Bearer jwt-token"}
    created = client.post(
        "/v3/documents", json={"content": "OIDC scoped note", "containerTag": "u1"}, headers=headers
    )
    assert created.status_code == 201, created.text
    search = client.post("/v4/search", json={"q": "OIDC", "containerTag": "u1"}, headers=headers)
    assert search.status_code == 200
    audit = client.get("/v4/audit", headers={"Authorization": "Bearer admin-key"})
    assert audit.status_code == 200
    assert any(event["actorKind"] == "oidc" for event in audit.json()["events"])


def test_external_identity_cannot_cross_scope() -> None:
    from memoratum.app import create_app
    from memoratum.auth import Identity, StaticIdentityProvider

    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MEMORATUM_API_KEY"] = "admin-key"
    provider = StaticIdentityProvider(
        {"jwt-token": Identity(subject="alice", container_tag="u1", org_id="org-a")}
    )
    client = TestClient(create_app(identity_provider=provider))
    response = client.post(
        "/v3/documents",
        json={"content": "forbidden", "containerTag": "u2"},
        headers={"Authorization": "Bearer jwt-token"},
    )
    assert response.status_code == 403
