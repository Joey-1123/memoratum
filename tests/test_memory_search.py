"""Memory search modes contract (RED)."""

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar


class _Stub(BaseHTTPRequestHandler):
    payload: ClassVar[dict] = {
        "facts": [{"subject": "user", "predicate": "loves", "object": "Paris"}]
    }

    def do_POST(self) -> None:
        body = json.dumps(
            {"choices": [{"message": {"content": json.dumps(self.payload)}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _setup():
    from memoratum import db
    from memoratum.dreaming import ChatLLM, dream_document
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_all

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
    e = HashEmbedder(dims=16)
    doc = db.create_document(conn, container_tag="u1", content="The user loves Paris.")
    process_all(conn, e)
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm = ChatLLM(endpoint=f"http://127.0.0.1:{server.server_port}/v1", model="m", api_key="k")
    dream_document(conn, llm, doc["id"])
    server.shutdown()
    return conn, e


def test_memories_mode_returns_facts() -> None:
    from memoratum.search import search

    conn, e = _setup()
    hits = search(conn, e, "what does the user love", container_tag="u1", search_mode="memories")
    assert len(hits) >= 1
    assert hits[0]["memory"] == "user loves Paris"
    assert set(hits[0]) >= {"id", "memory", "similarity"}
    conn.close()


def test_documents_mode_excludes_facts() -> None:
    from memoratum.search import search

    conn, e = _setup()
    hits = search(conn, e, "user loves Paris", container_tag="u1", search_mode="documents")
    assert hits
    assert all("memory" not in h for h in hits)
    conn.close()


def test_hybrid_returns_both_kinds() -> None:
    from memoratum.search import search

    conn, e = _setup()
    hits = search(conn, e, "user loves Paris", container_tag="u1", search_mode="hybrid", limit=10)
    kinds = {("memory" if "memory" in h else "chunk") for h in hits}
    assert kinds == {"memory", "chunk"}
    conn.close()
