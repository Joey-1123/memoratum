"""Worker loop contract (RED)."""

import os
import tempfile


def _db():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "t.db"))


def _env():
    from memoratum.embeddings import HashEmbedder

    return HashEmbedder(dims=16), None


def test_run_once_processes_ingest_job() -> None:
    from memoratum import db, jobs
    from memoratum.worker import run_once

    conn = _db()
    e, llm = _env()
    doc = db.create_document(conn, container_tag="u1", content="hello world")
    jid = jobs.enqueue(conn, kind="ingest", payload={"document_id": doc["id"]})
    assert run_once(conn, e, llm, worker_id="w1") == jid
    assert db.get_document(conn, doc["id"])["status"] == "done"
    assert jobs.get(conn, jid)["status"] == "done"
    assert run_once(conn, e, llm, worker_id="w1") is None
    conn.close()


def test_unknown_kind_fails_without_retry_loop() -> None:
    from memoratum import jobs
    from memoratum.worker import run_once

    conn = _db()
    e, llm = _env()
    jid = jobs.enqueue(conn, kind="nope", payload={})
    assert run_once(conn, e, llm, worker_id="w1") == jid
    assert jobs.get(conn, jid)["status"] == "failed"
    conn.close()


def test_repeated_failure_poison_pills_after_max_attempts() -> None:
    from memoratum import db, jobs
    from memoratum.worker import run_once

    class Boom:
        dims = 8

        def embed(self, texts):
            raise RuntimeError("nope")

    conn = _db()
    doc = db.create_document(conn, container_tag="u1", content="x")
    jobs.enqueue(conn, kind="ingest", payload={"document_id": doc["id"]})
    for _ in range(4):
        run_once(conn, Boom(), None, worker_id="w1")
    rows = conn.execute("SELECT status FROM jobs").fetchall()
    assert [r["status"] for r in rows] == ["failed"]
    assert db.get_document(conn, doc["id"])["status"] == "failed"
    conn.close()
