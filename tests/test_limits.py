"""Rate limit + input caps contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client():
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.pop("MEMORATUM_API_KEY", None)
    from memoratum.app import create_app

    try:
        return TestClient(create_app(rate_limit_per_minute=3))
    finally:
        os.environ.pop("MEMORATUM_DATA_DIR", None)


def test_rate_limit_429s_with_envelope() -> None:
    c = _client()
    codes = [
        c.post("/v4/search", json={"q": "x", "containerTag": "u1"}).status_code for _ in range(5)
    ]
    assert codes[0] == 200
    assert 429 in codes
    r = c.post("/v4/search", json={"q": "x", "containerTag": "u1"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "RATE_LIMITED"


def test_health_exempt_from_limit() -> None:
    c = _client()
    for _ in range(5):
        c.post("/v4/search", json={"q": "x", "containerTag": "u1"})
    assert c.get("/health").status_code == 200


def test_loopback_helper() -> None:
    from memoratum.app import _is_loopback

    assert _is_loopback("127.0.0.1")
    assert _is_loopback("::1")
    assert not _is_loopback("testclient")
    assert not _is_loopback("203.0.113.9")


def test_oversized_inputs_rejected() -> None:
    c = _client()
    big = "x" * 600_000
    assert c.post("/v3/documents", json={"content": big, "containerTag": "u1"}).status_code == 422
    assert c.post("/v4/search", json={"q": "y" * 3000, "containerTag": "u1"}).status_code == 422
