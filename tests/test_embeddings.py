"""Embedder contract (RED)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar


def test_hash_embedder_is_deterministic_and_normalized() -> None:
    from memoratum.embeddings import HashEmbedder

    e = HashEmbedder(dims=64)
    a = e.embed(["hello world"])
    b = e.embed(["hello world"])
    assert a == b
    assert len(a[0]) == 64
    norm = sum(x * x for x in a[0]) ** 0.5
    assert abs(norm - 1.0) < 1e-6
    assert e.embed(["other"])[0] != a[0]


class _Stub(BaseHTTPRequestHandler):
    calls: ClassVar[list] = []

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        _Stub.calls.append(len(body["input"]))
        vecs = [[float(len(t))] * 4 for t in body["input"]]
        payload = json.dumps(
            {"data": [{"embedding": v, "index": i} for i, v in enumerate(vecs)]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


def test_api_embedder_against_local_stub() -> None:
    from memoratum.embeddings import ApiEmbedder

    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        e = ApiEmbedder(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1", model="m", api_key="k", dims=4
        )
        out = e.embed(["ab", "cdef"])
        assert out == [[2.0] * 4, [4.0] * 4]
    finally:
        server.shutdown()


def test_api_embedder_batches_large_inputs() -> None:
    from memoratum.embeddings import ApiEmbedder

    _Stub.calls.clear()
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        e = ApiEmbedder(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1", model="m", api_key="k", dims=4
        )
        out = e.embed([f"t{i}" for i in range(130)])
        assert len(out) == 130
        assert len(_Stub.calls) >= 2
        assert all(n <= 64 for n in _Stub.calls)
    finally:
        server.shutdown()
