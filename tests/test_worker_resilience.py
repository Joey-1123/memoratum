"""The worker process must survive failures it did not expect.

`worker.main()` previously had a `finally` but no `except`, and `jobs.claim` sat
outside `run_once`'s try block, so any transient SQLite lock or provider blip
terminated the process — taking with it whatever job it was holding, which is the
first half of the P0 wedge.
"""

import sqlite3

from helpers import use_tmp_data_dir


def _conn():
    use_tmp_data_dir()
    from memoratum import db
    from memoratum.config import Settings

    return db.connect(Settings.load().db_path)


def test_run_once_propagates_a_lock_error():
    """Documents the escape hatch: a lock error is NOT handled inside run_once.

    This is why main() must have its own handler. If this test ever starts
    failing because run_once swallows the error, main() still needs the guard for
    failures raised elsewhere (connect, build_vector_store).
    """
    from memoratum import jobs, worker
    from memoratum.embeddings import HashEmbedder

    conn = _conn()
    jobs.enqueue(conn, kind="ingest", payload={"document_id": "missing"})

    original = jobs.claim

    def locked(c, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    jobs.claim = locked
    worker.jobs.claim = locked
    try:
        try:
            worker.run_once(conn, HashEmbedder(dims=8), None, worker_id="w")
            raised = None
        except sqlite3.OperationalError as exc:
            raised = exc
        assert isinstance(raised, sqlite3.OperationalError)
    finally:
        jobs.claim = original
        worker.jobs.claim = original
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        conn.close()


def test_main_loop_body_is_guarded():
    """Structural guard: main() must catch, not just clean up."""
    import inspect

    from memoratum import worker

    source = inspect.getsource(worker.main)
    loop = source[source.index("while not _stop") :]
    assert "except Exception" in loop, "worker main loop has no exception handler"
    assert "reap_stale" in loop, "worker main loop never reaps stale jobs"


def test_worker_reaps_before_claiming():
    """Reaping must precede claiming, or a dead job delays recovery by one cycle."""
    import inspect

    from memoratum import worker

    source = inspect.getsource(worker.main)
    assert source.index("reap_stale") < source.index("run_once")


def test_poison_pill_still_terminates():
    """Adding leases must not make a permanently failing job loop forever."""
    from memoratum import jobs, worker
    from memoratum.embeddings import HashEmbedder

    conn = _conn()
    job_id = jobs.enqueue(conn, kind="ingest", payload={"document_id": "does-not-exist"})
    for _ in range(worker.MAX_ATTEMPTS + 2):
        worker.run_once(conn, HashEmbedder(dims=8), None, worker_id="w")

    final = jobs.get(conn, job_id)
    assert final["status"] == "failed"
    assert final["attempts"] == worker.MAX_ATTEMPTS
    conn.close()


def test_reaping_does_not_reset_attempts_of_a_poison_pill():
    """A reaped job keeps its attempt count, so it still dies at MAX_ATTEMPTS."""
    from memoratum import jobs, worker
    from memoratum.embeddings import HashEmbedder

    conn = _conn()
    job_id = jobs.enqueue(conn, kind="ingest", payload={"document_id": "does-not-exist"})

    for _ in range(worker.MAX_ATTEMPTS):
        worker.run_once(conn, HashEmbedder(dims=8), None, worker_id="w")
        # Simulate the worker dying after each attempt: expire the lease by hand.
        conn.execute(
            "UPDATE jobs SET lease_expires_at = 0 WHERE id = ? AND status = 'running'",
            (job_id,),
        )
        conn.commit()

    final = jobs.get(conn, job_id)
    assert final["status"] == "failed", f"poison pill survived: {final}"
    assert final["attempts"] == worker.MAX_ATTEMPTS
    conn.close()
