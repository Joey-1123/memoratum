"""Job queue contract (RED)."""

import os
import tempfile


def _db():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_enqueue_and_atomic_claim() -> None:
    from memoratum import jobs

    conn = _db()
    jid = jobs.enqueue(conn, kind="ingest", payload={"document_id": "d1"})
    claimed = jobs.claim(conn, worker="w1")
    assert claimed is not None and claimed["id"] == jid
    assert claimed["status"] == "running"
    # second worker gets nothing — the job is already claimed
    assert jobs.claim(conn, worker="w2") is None
    conn.close()


def test_complete_and_fail_transitions() -> None:
    from memoratum import jobs

    conn = _db()
    jid = jobs.enqueue(conn, kind="dream", payload={"document_id": "d1"})
    jobs.claim(conn, worker="w1")
    jobs.complete(conn, jid, result={"facts": 2})
    row = jobs.get(conn, jid)
    assert row["status"] == "done"
    j2 = jobs.enqueue(conn, kind="ingest", payload={})
    jobs.claim(conn, worker="w1")
    jobs.fail(conn, j2, error="boom")
    row = jobs.get(conn, j2)
    assert row["status"] == "failed" and row["error"] == "boom"
    assert jobs.get(conn, "nope") is None
    conn.close()


def test_pending_listing() -> None:
    from memoratum import jobs

    conn = _db()
    jobs.enqueue(conn, kind="ingest", payload={})
    jobs.enqueue(conn, kind="dream", payload={})
    assert len(jobs.pending(conn)) == 2
    conn.close()
