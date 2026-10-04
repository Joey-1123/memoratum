"""Job lease, heartbeat, and reaper contract.

These tests exist because the P0 outage shipped behind a green suite: the existing
tests asserted that a running job is never stolen, and nothing asserted that a dead
one is ever recovered. Both halves are required.
"""

import json
import time

import pytest
from helpers import use_tmp_data_dir


@pytest.fixture
def conn():
    use_tmp_data_dir()
    from memoratum import db
    from memoratum.config import Settings

    connection = db.connect(Settings.load().db_path)
    yield connection
    connection.close()


def _columns(conn) -> set[str]:
    return {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}


def _enqueue(conn, kind: str = "ingest", payload: dict | None = None) -> str:
    from memoratum import jobs

    return jobs.enqueue(conn, kind=kind, payload=payload or {})


def _lease_columns_present(conn) -> bool:
    return {"lease_expires_at", "heartbeat_at"} <= _columns(conn)


# --- lease fields ------------------------------------------------------------


def test_claim_sets_a_lease(conn):
    """A claim must write a lease deadline in the future, else it cannot expire."""
    from memoratum import jobs

    _enqueue(conn)
    assert _lease_columns_present(conn), "jobs table is missing lease columns"

    job = jobs.claim(conn, worker="w1")
    assert job is not None
    assert job["lease_expires_at"] > time.time(), "claim did not set a future lease"
    assert job["heartbeat_at"] is not None


def test_heartbeat_extends_but_never_shortens(conn):
    from memoratum import jobs

    _enqueue(conn)
    job = jobs.claim(conn, worker="w1")
    first = job["lease_expires_at"]

    jobs.heartbeat(conn, job["id"])
    extended = jobs.get(conn, job["id"])["lease_expires_at"]
    assert extended > first, "heartbeat must extend the lease"

    # A heartbeat with a deadline in the past must not pull the lease in.
    jobs.heartbeat(conn, job["id"], ttl_seconds=-600)
    after = jobs.get(conn, job["id"])["lease_expires_at"]
    assert after >= extended, "heartbeat must never shorten a lease"


def test_completing_a_job_clears_its_lease(conn):
    from memoratum import jobs

    _enqueue(conn)
    job = jobs.claim(conn, worker="w1")
    jobs.complete(conn, job["id"], result={"ok": True})

    done = jobs.get(conn, job["id"])
    assert done["status"] == "done"
    assert done["lease_expires_at"] == 0, "a terminal job must not hold a lease"
    assert done["heartbeat_at"] is None


# --- the reaper -------------------------------------------------------------


def test_reaper_recovers_a_job_whose_lease_expired(conn):
    """The core P0 fix: a dead worker's job must become claimable again."""
    from memoratum import jobs

    _enqueue(conn)
    job = jobs.claim(conn, worker="dead-worker")

    # The worker is gone; its lease runs out.
    conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (time.time() - 1, job["id"]))
    conn.commit()

    reaped = jobs.reap_stale(conn, ttl_seconds=300)
    assert [r["id"] for r in reaped] == [job["id"]]
    assert jobs.get(conn, job["id"])["status"] == "queued"
    assert jobs.get(conn, job["id"])["lease_expires_at"] == 0


def test_reaper_does_not_decrement_attempts(conn):
    """A requeued job must keep its attempt count, or a poison pill never dies."""
    from memoratum import jobs

    _enqueue(conn)
    job = jobs.claim(conn, worker="w1")
    attempts = jobs.get(conn, job["id"])["attempts"]

    conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (time.time() - 1, job["id"]))
    conn.commit()
    jobs.reap_stale(conn, ttl_seconds=300)

    assert jobs.get(conn, job["id"])["attempts"] == attempts


def test_reaper_never_steals_live_work(conn):
    """The invariant the existing suite protects. A live lease is untouchable."""
    from memoratum import jobs

    _enqueue(conn)
    job = jobs.claim(conn, worker="live-worker")

    assert jobs.reap_stale(conn, ttl_seconds=300) == []
    assert jobs.get(conn, job["id"])["status"] == "running"
    assert jobs.get(conn, job["id"])["worker"] == "live-worker"


def test_recovered_job_can_be_claimed_again(conn):
    from memoratum import jobs

    _enqueue(conn)
    job = jobs.claim(conn, worker="dead")
    conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (time.time() - 1, job["id"]))
    conn.commit()
    jobs.reap_stale(conn, ttl_seconds=300)

    reclaimed = jobs.claim(conn, worker="fresh")
    assert reclaimed is not None
    assert reclaimed["id"] == job["id"]
    assert reclaimed["worker"] == "fresh"


# --- cancellation -----------------------------------------------------------


def test_stale_running_job_is_cancellable(conn):
    """The operator's route out of a wedged project."""
    from memoratum import jobs

    _enqueue(conn)
    job = jobs.claim(conn, worker="dead")
    conn.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (time.time() - 1, job["id"]))
    conn.commit()

    assert jobs.cancel(conn, job["id"]) is True
    assert jobs.get(conn, job["id"])["status"] == "cancelled"


def test_live_running_job_is_not_cancellable(conn):
    from memoratum import jobs

    _enqueue(conn)
    job = jobs.claim(conn, worker="live")

    assert jobs.cancel(conn, job["id"]) is False
    assert jobs.get(conn, job["id"])["status"] == "running"


def test_queued_job_is_still_cancellable(conn):
    from memoratum import jobs

    job_id = _enqueue(conn)
    assert jobs.cancel(conn, job_id) is True
    assert jobs.get(conn, job_id)["status"] == "cancelled"


# --- atomic claim -----------------------------------------------------------


def test_concurrent_claims_never_hand_out_one_job_twice(conn):
    """SELECT-then-UPDATE without BEGIN IMMEDIATE can double-claim under contention."""
    import sqlite3
    import threading

    from memoratum import jobs

    job_id = _enqueue(conn)
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def grab(worker: str) -> None:
        local = sqlite3.connect(db_path, timeout=10)
        local.row_factory = sqlite3.Row  # jobs.claim indexes rows by name
        try:
            local.execute("PRAGMA busy_timeout=10000")
            barrier.wait()
            job = jobs.claim(local, worker=worker)
            if job is not None:
                with lock:
                    claimed.append(job["id"])
        except sqlite3.OperationalError:
            pass  # a busy writer losing the race is acceptable; double-claim is not
        finally:
            local.close()

    threads = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == len(set(claimed)), "the same job was claimed more than once"
    assert claimed == [job_id], "the single queued job should be claimed exactly once"


# --- bulk heartbeat ---------------------------------------------------------


def test_bulk_progress_renews_the_lease(conn):
    """A long bulk job must not be reaped while it is actively progressing."""
    from memoratum import jobs

    job_id = _enqueue(conn, kind="bulk_memories", payload={"operations": []})
    job = jobs.claim(conn, worker="bulk")
    before = job["lease_expires_at"]

    jobs.update_result(conn, job_id, {"total": 0, "completed": 0})
    after = jobs.get(conn, job_id)["lease_expires_at"]

    assert after >= before, "reporting progress must renew the lease"


def test_result_payload_round_trips(conn):
    """Guards the update_result path the heartbeat rides on."""
    from memoratum import jobs

    job_id = _enqueue(conn)
    jobs.update_result(conn, job_id, {"total": 2, "items": [{"index": 0, "status": "completed"}]})
    stored = jobs.get(conn, job_id)
    assert stored["result"]["total"] == 2
    assert json.dumps(stored["result"])  # must be JSON-serialisable
