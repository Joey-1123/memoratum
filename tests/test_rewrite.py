"""Query-rewrite contract (RED)."""


def test_expand_query_unit() -> None:
    from memoratum.search import expand_query

    class FakeLLM:
        def complete(self, system: str, user: str) -> str:
            assert "alternative" in system
            return '{"queries": ["alpha docs", "alpha guide"]}'

    assert expand_query(FakeLLM(), "alpha") == ["alpha", "alpha docs", "alpha guide"]

    class BrokenLLM:
        def complete(self, system: str, user: str) -> str:
            raise RuntimeError("down")

    assert expand_query(BrokenLLM(), "alpha") == ["alpha"]


def test_merge_dedups_by_best_score() -> None:
    from memoratum.search import merge_hits

    a = [{"id": "x", "similarity": 0.5}, {"id": "y", "similarity": 0.9}]
    b = [{"id": "x", "similarity": 0.7}, {"id": "z", "similarity": 0.1}]
    merged = merge_hits([a, b], limit=10)
    assert [(h["id"], h["similarity"]) for h in merged] == [("y", 0.9), ("x", 0.7), ("z", 0.1)]


def test_rewrite_expands_and_merges() -> None:
    import json
    import os
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from fastapi.testclient import TestClient

    class _Stub(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(n)
            variants = {"queries": ["alpha docs", "alpha guide"]}
            body = json.dumps(
                {"choices": [{"message": {"content": json.dumps(variants)}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ["MEMORATUM_LLM_ENDPOINT"] = f"http://127.0.0.1:{server.server_port}/v1"
    os.environ["MEMORATUM_LLM_MODEL"] = "m"
    try:
        from memoratum.app import create_app

        c = TestClient(create_app())
        c.post(
            "/v3/documents", json={"content": "alpha project guide text here", "containerTag": "u1"}
        )
        r = c.post(
            "/v4/search",
            json={"q": "zzz-no-match", "containerTag": "u1", "rewriteQuery": True, "limit": 5},
        )
        assert r.status_code == 200, r.text
        # stub LLM expands to on-topic variants, so the rewrite path finds the doc
        assert r.json()["total"] >= 1
    finally:
        server.shutdown()
        os.environ.pop("MEMORATUM_DATA_DIR", None)
        os.environ.pop("MEMORATUM_LLM_ENDPOINT", None)
        os.environ.pop("MEMORATUM_LLM_MODEL", None)
