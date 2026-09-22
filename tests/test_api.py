"""API contract (RED)."""

import os
import tempfile

from fastapi.testclient import TestClient


def _client(auth: bool = False):
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    if auth:
        os.environ["MEMORATUM_API_KEY"] = "test-key"
    else:
        os.environ.pop("MEMORATUM_API_KEY", None)
    from memoratum.app import create_app

    return TestClient(create_app())


def _drain() -> None:
    """Run the worker until the queue is empty (async ingest in tests)."""
    from helpers import drain

    drain()


def test_ingest_then_search_roundtrip() -> None:
    c = _client()
    r = c.post("/v3/documents", json={"content": "The cat sat on the mat.", "containerTag": "u1"})
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    _drain()
    s = c.get(f"/v3/documents/{doc_id}")
    assert s.json()["status"] == "done"
    q = c.post("/v4/search", json={"q": "cat mat", "containerTag": "u1", "limit": 5})
    assert q.status_code == 200, q.text
    body = q.json()
    assert body["total"] >= 1
    assert "cat" in body["results"][0]["chunk"]


def test_validation_and_errors() -> None:
    c = _client()
    assert c.post("/v3/documents", json={}).status_code == 422
    assert c.post("/v3/documents", json={"content": 123}).status_code == 422
    assert c.get("/v3/documents/nope").status_code == 404
    assert c.get("/v3/documents/nope").json()["error"]["code"] == "NOT_FOUND"


def test_bearer_auth_and_scoping() -> None:
    c = _client(auth=True)
    assert c.post("/v3/documents", json={"content": "x", "containerTag": "u1"}).status_code == 401
    authed = {"Authorization": "Bearer test-key"}
    r = c.post("/v3/documents", json={"content": "secret", "containerTag": "u1"}, headers=authed)
    assert r.status_code == 201, r.text
    q = c.post("/v4/search", json={"q": "secret", "containerTag": "u1"}, headers=authed)
    assert q.status_code == 200


def test_dreaming_param_and_memory_search_mode() -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from fastapi.testclient import TestClient

    class _Stub(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            facts = {"facts": [{"subject": "user", "predicate": "loves", "object": "Paris"}]}
            body = json.dumps({"choices": [{"message": {"content": json.dumps(facts)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/v1"
        os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
        os.environ.pop("MEMORATUM_API_KEY", None)
        os.environ["MEMORATUM_LLM_ENDPOINT"] = base
        os.environ["MEMORATUM_LLM_MODEL"] = "m"
        from memoratum.app import create_app

        c = TestClient(create_app())
        r = c.post(
            "/v3/documents",
            json={"content": "The user loves Paris.", "containerTag": "u1", "dreaming": "instant"},
        )
        assert r.status_code == 201, r.text
        _drain()
        q = c.post(
            "/v4/search",
            json={"q": "what does the user love", "containerTag": "u1", "searchMode": "memories"},
        )
        assert q.status_code == 200, q.text
        assert q.json()["results"][0]["memory"] == "user loves Paris"
        d = c.post(
            "/v4/search",
            json={"q": "user loves Paris", "containerTag": "u1", "searchMode": "documents"},
        )
        assert all("memory" not in h for h in d.json()["results"])
    finally:
        server.shutdown()
        os.environ.pop("MEMORATUM_LLM_ENDPOINT", None)
        os.environ.pop("MEMORATUM_LLM_MODEL", None)
