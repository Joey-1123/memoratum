"""Dreaming contract (RED)."""

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar


def _db():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "t.db"))


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


def _llm():
    from memoratum.dreaming import ChatLLM

    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm = ChatLLM(endpoint=f"http://127.0.0.1:{server.server_port}/v1", model="m", api_key="k")
    return server, llm


def test_extract_validates_facts() -> None:
    from memoratum.dreaming import extract_facts

    server, llm = _llm()
    try:
        facts = extract_facts(llm, "The user loves Paris.")
        assert facts == [{"subject": "user", "predicate": "loves", "object": "Paris"}]
    finally:
        server.shutdown()


def test_extract_rejects_malformed() -> None:
    from memoratum.dreaming import extract_facts

    _Stub.payload = {"nope": []}
    server, llm = _llm()
    try:
        assert extract_facts(llm, "blah") == []
    finally:
        _Stub.payload = {"facts": [{"subject": "user", "predicate": "loves", "object": "Paris"}]}
        server.shutdown()


def test_dream_document_stores_tagged_facts() -> None:
    from memoratum import db
    from memoratum.dreaming import dream_document
    from memoratum.facts import list_facts

    conn = _db()
    doc = db.create_document(conn, container_tag="u1", content="The user loves Paris.")
    server, llm = _llm()
    try:
        dream_document(conn, llm, doc["id"])
        facts = list_facts(conn, "u1")
        assert [(f["subject"], f["predicate"], f["object"]) for f in facts] == [
            ("user", "loves", "Paris")
        ]
        assert facts[0]["document_id"] == doc["id"]
        assert db.get_document(conn, doc["id"])["dreamed_at"] is not None
    finally:
        server.shutdown()
        conn.close()


def test_dynamic_mode_bundles_tag() -> None:
    from memoratum import db
    from memoratum.dreaming import dream_pending
    from memoratum.facts import list_facts

    conn = _db()
    db.create_document(conn, container_tag="u1", content="Fact one.")
    db.create_document(conn, container_tag="u1", content="Fact two.")
    server, llm = _llm()
    try:
        n = dream_pending(conn, llm, mode="dynamic")
        assert n == 1
        assert len(list_facts(conn, "u1")) == 1
    finally:
        server.shutdown()
        conn.close()
