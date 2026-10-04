"""Requests must not be fully serialised.

`_DB_LOCK` was held across the `yield` in `get_conn`, i.e. for the entire request
lifetime, so throughput was one request at a time regardless of worker count.
Measured before this change: 4 concurrent `GET /v1/ping/` took 1.48s wall with
per-request durations of 0.57/0.88/1.18/1.48s -- textbook serialisation.

This must land AFTER the commit-boundary work: removing the serialising lock while
writes are still non-atomic would expose partial-write interleavings that the lock
was masking by accident.
"""

import threading
import time

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_API_KEY", "admin-key")
    from memoratum.app import create_app

    return TestClient(create_app())


def _admin() -> dict[str, str]:
    return {"Authorization": "Token admin-key"}


# --- structural guarantee ---------------------------------------------------


def test_db_lock_is_not_held_across_the_request():
    """AST check: the ``yield`` must NOT sit inside the ``with _DB_LOCK`` block.

    Asserting this on source text is unreliable -- an earlier version of this test
    passed while the lock still spanned the whole request, because the slice it
    inspected happened to contain no literal 'yield'. The AST cannot be fooled.
    """
    import ast
    import inspect
    import textwrap

    from memoratum import app as appmod

    tree = ast.parse(textwrap.dedent(inspect.getsource(appmod.get_conn)))

    def is_db_lock(node) -> bool:
        if isinstance(node, ast.Name):
            return node.id == "_DB_LOCK"
        if isinstance(node, ast.Attribute):
            return node.attr == "_DB_LOCK"
        return False

    guarded_withs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With) and any(is_db_lock(item.context_expr) for item in node.items)
    ]
    assert guarded_withs, "get_conn no longer guards connection setup with _DB_LOCK"

    for node in guarded_withs:
        yields = [n for n in ast.walk(node) if isinstance(n, (ast.Yield, ast.YieldFrom))]
        assert not yields, (
            "the request body still executes while _DB_LOCK is held -- "
            "every request remains serialised"
        )

    # ...and there must be a yield outside it, or we just deleted the endpoint.
    assert any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(tree))


def test_each_request_gets_its_own_connection():
    """Distinct connections are what make WAL readers safe to run in parallel."""
    import inspect

    from memoratum import app as appmod

    source = inspect.getsource(appmod.get_conn)
    assert "db.connect(" in source, "requests share a single connection"


# --- concurrency behaviour --------------------------------------------------


def test_lock_is_released_before_the_endpoint_body_runs(tmp_path, monkeypatch):
    """Deterministic proof, driven straight through the dependency.

    Timing a TestClient cannot distinguish real concurrency from a fast serialised
    path, so it would pass either way. Instead we advance get_conn to its yield --
    the point at which the endpoint body would begin -- and assert the lock is
    actually free at that instant.
    """
    from types import SimpleNamespace

    from memoratum import app as appmod

    settings = appmod.Settings.load()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))

    generator = appmod.get_conn(request)
    conn = next(generator)  # runs up to the yield

    try:
        acquired = appmod._DB_LOCK.acquire(blocking=False)
        assert acquired, (
            "_DB_LOCK is still held at the yield, so the endpoint body would run "
            "with it held and every request would be serialised"
        )
        appmod._DB_LOCK.release()
    finally:
        try:
            next(generator)
        except StopIteration:
            pass
        conn.close()


def test_concurrent_writes_do_not_corrupt_or_lose(tmp_path, monkeypatch):
    """Parallel writers must all land, with no partial state."""
    client = _client(tmp_path, monkeypatch)
    created = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": "Concurrent"},
    )
    assert created.status_code == 201, created.text
    project = str(created.json()["id"])

    results: list[int] = []
    lock = threading.Lock()

    def write(index: int):
        response = client.post(
            "/v3/documents",
            headers=_admin(),
            json={
                "content": f"concurrent write {index}",
                "containerTag": "mem0:user_id:alice",
                "project_id": project,
            },
        )
        with lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code == 201 for code in results), f"write failures: {sorted(results)}"

    from memoratum import db
    from memoratum.config import Settings

    conn = db.connect(Settings.load().db_path)
    try:
        documents = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE project_id = ?", (project,)
        ).fetchone()[0]
        assert documents == 8, f"expected 8 documents, found {documents}"
        # Every document must have its ingest job: no orphans under concurrency.
        orphans = conn.execute(
            "SELECT COUNT(*) FROM documents d WHERE d.project_id = ? AND NOT EXISTS ("
            "  SELECT 1 FROM jobs j WHERE j.kind = 'ingest'"
            "   AND json_extract(j.payload, '$.document_id') = d.id)",
            (project,),
        ).fetchone()[0]
        assert orphans == 0, f"{orphans} orphan document(s) under concurrency"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_busy_database_is_retried_not_failed(tmp_path, monkeypatch):
    """SQLite allows one writer; a busy database must wait, not 500."""

    client = _client(tmp_path, monkeypatch)
    created = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": "Busy"},
    )
    assert created.status_code == 201, created.text
    project = str(created.json()["id"])

    from memoratum import db
    from memoratum.config import Settings

    # Hold an IMMEDIATE transaction open so other writers must wait on the lock.
    blocker = db.connect(Settings.load().db_path)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("UPDATE projects SET updated_at = updated_at WHERE id = ?", (project,))

    outcome: list[int] = []

    def write():
        response = client.post(
            "/v3/documents",
            headers=_admin(),
            json={
                "content": "blocked",
                "containerTag": "mem0:user_id:alice",
                "project_id": project,
            },
        )
        outcome.append(response.status_code)

    thread = threading.Thread(target=write)
    thread.start()
    time.sleep(0.2)
    blocker.rollback()
    blocker.close()
    thread.join(timeout=30)

    assert not thread.is_alive(), "the write hung instead of waiting for the lock"
    assert outcome and outcome[0] in {201, 409, 503}, (
        f"expected the busy write to wait or fail cleanly, got {outcome}"
    )


def test_busy_timeout_is_long_enough_to_survive_contention():
    from memoratum import db

    conn = db.connect(":memory:")
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout >= 5000, f"busy_timeout is {timeout}ms; contention will surface as errors"
    finally:
        conn.close()


def test_wal_is_enabled(tmp_path):
    """WAL is what lets readers run while a writer holds the lock.

    Must be file-backed: SQLite reports 'memory' for in-memory databases
    regardless of the pragma, so an in-memory check would fail spuriously.
    """
    from memoratum import db

    conn = db.connect(str(tmp_path / "wal.db"))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal", f"journal_mode is {mode}, expected wal"
    finally:
        conn.close()
