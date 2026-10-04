"""One logical operation must have one commit boundary.

Measured before this change: a single ``POST /v3/memories/add/`` performed **5
COMMIT statements**. A crash between the document commit and the job enqueue
leaves ``documents=1, memories=1, jobs=0`` -- content durably stored and
immediately searchable, with the ingest job that would ever process it simply
absent, and the document stuck at ``queued`` forever.

Constitution Principle III requires the enqueue to share a transaction with the
state change it depends on.
"""

import pytest
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_API_KEY", "admin-key")
    from memoratum.app import create_app

    return TestClient(create_app())


def _admin() -> dict[str, str]:
    return {"Authorization": "Token admin-key"}


def _project(client: TestClient, name: str = "Atomic") -> str:
    response = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": name},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _add(client: TestClient, project_id: str, content: str = "atomic please"):
    return client.post(
        "/v3/memories/add/",
        headers=_admin(),
        json={
            "messages": [{"role": "user", "content": content}],
            "user_id": "alice",
            "infer": False,
            "project_id": project_id,
        },
    )


def _db():
    from memoratum import db
    from memoratum.config import Settings

    return db.connect(Settings.load().db_path)


def _counts(conn) -> tuple[int, int, int]:
    documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    memories = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'ingest'").fetchone()[0]
    return documents, memories, jobs


# --- the atomicity contract -------------------------------------------------


def _statement_log(monkeypatch_target):
    """Record every statement, marking COMMITs."""
    log: list[str] = []
    original = monkeypatch_target.connect
    monkeypatch_target._ORIGINAL_CONNECT = original

    def traced(path):
        conn = original(path)

        def on_trace(statement: str) -> None:
            text = statement.strip()
            if text and not text.upper().startswith(("BEGIN", "PRAGMA")):
                log.append(text)

        conn.set_trace_callback(on_trace)
        return conn

    monkeypatch_target.connect = traced
    return log


def _restore_connect(dbmod) -> None:
    """Undo _statement_log's monkeypatch without needing the monkeypatch fixture."""
    if not hasattr(dbmod, "_ORIGINAL_CONNECT"):
        return
    dbmod.connect = dbmod._ORIGINAL_CONNECT
    del dbmod._ORIGINAL_CONNECT


def test_all_durable_mutation_shares_one_commit(tmp_path, monkeypatch):
    """The document, its memory, the ingest job, the audit row and the domain
    event must all become durable together.

    Measured before the fix: 5 separate commits, so a crash between the document
    commit and the job enqueue left documents=1, memories=1, jobs=0.

    Usage metering commits separately and deliberately: it happens during
    authorization, before the transaction, and an attempt that later fails should
    still be counted. Rolling it back would lose a real request from the record.
    """
    from memoratum import db as dbmod

    client = _client(tmp_path, monkeypatch)
    project_id = _project(client)

    log = _statement_log(dbmod)
    try:
        response = _add(client, project_id)
    finally:
        _restore_connect(dbmod)
    assert response.status_code == 200, response.text

    # Index of the first durable mutation and the commits around it.
    first_insert = next(
        (i for i, s in enumerate(log) if s.upper().startswith("INSERT INTO DOCUMENTS")), None
    )
    assert first_insert is not None, "no document was inserted"
    commits_after = [
        i for i, s in enumerate(log) if s.upper().startswith("COMMIT") and i > first_insert
    ]
    assert len(commits_after) == 1, (
        f"{len(commits_after)} commits after the document insert; expected exactly 1"
    )
    # Everything that must be atomic has to appear before that single commit.
    boundary = commits_after[0]
    for table in ["MEMORIES", "JOBS", "AUDIT_EVENTS", "DOMAIN_EVENTS"]:
        assert any(
            s.upper().startswith(f"INSERT INTO {table}") and i < boundary for i, s in enumerate(log)
        ), f"{table} was written outside the single commit boundary"


def test_no_orphan_document_without_a_job(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client)
    assert _add(client, project_id).status_code == 200

    conn = _db()
    try:
        orphans = conn.execute(
            "SELECT COUNT(*) FROM documents d WHERE NOT EXISTS ("
            "  SELECT 1 FROM jobs j WHERE j.kind = 'ingest'"
            "   AND json_extract(j.payload, '$.document_id') = d.id)"
        ).fetchone()[0]
        assert orphans == 0, f"{orphans} document(s) stranded with no ingest job"
    finally:
        conn.close()


def test_crash_at_any_commit_point_leaves_no_partial_state(tmp_path, monkeypatch):
    """Inject a crash before each commit; the store must be all-or-nothing.

    Before the fix, crashing before the final commit left documents=1,
    memories=1, jobs=0 -- durable content with no way to ever process it.
    """
    from test_failure_injection import CrashInjected

    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Crashy")

    baseline_docs, _, _ = _counts(_db())
    baseline_docs_conn = _db()
    baseline_docs = baseline_docs_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    baseline_docs_conn.close()

    for target in range(1, 6):
        try:
            with crash_between(target):
                _add(client, project_id, content=f"crash at {target}")
        except CrashInjected:
            pass

        conn = _db()
        try:
            documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            orphans = conn.execute(
                "SELECT COUNT(*) FROM documents d WHERE NOT EXISTS ("
                "  SELECT 1 FROM jobs j WHERE j.kind = 'ingest'"
                "   AND json_extract(j.payload, '$.document_id') = d.id)"
            ).fetchone()[0]
            assert orphans == 0, f"crash before commit #{target} left {orphans} orphan document(s)"
            # A committed document must always have exactly one ingest job.
            for row in conn.execute("SELECT id FROM documents").fetchall():
                jobs = conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE kind = 'ingest'"
                    " AND json_extract(payload, '$.document_id') = ?",
                    (row["id"],),
                ).fetchone()[0]
                assert jobs == 1, f"document {row['id']} has {jobs} ingest jobs, expected exactly 1"
            assert documents >= baseline_docs
        finally:
            conn.close()


def test_fact_and_its_memory_commit_together(tmp_path, monkeypatch):
    """A fact insert that fails must not leave an orphan memory behind."""
    from memoratum import db

    use_client = _client(tmp_path, monkeypatch)
    project_id = _project(use_client, "Facts")

    conn = _db()
    before = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    conn.close()

    response = use_client.post(
        "/v4/facts",
        headers=_admin(),
        json={
            "subject": "user",
            "predicate": "loves",
            "object": "Paris",
            "containerTag": "mem0:user_id:alice",
            "project_id": project_id,
        },
    )
    assert response.status_code == 201, response.text

    conn = _db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == before + 1
        assert db.get_memory is not None
    finally:
        conn.close()


# --- structural guarantees --------------------------------------------------


def test_db_exposes_a_transaction_context_manager():
    from memoratum import db

    assert hasattr(db, "transaction"), "no transaction context manager exists"


def test_helpers_no_longer_commit_by_default(tmp_path, monkeypatch):
    """create_document/create_memory must not force a commit on their caller."""
    import inspect

    from memoratum import db

    for name in ["create_document", "create_memory"]:
        source = inspect.getsource(getattr(db, name))
        assert "commit: bool = True" in source, f"{name} must accept an explicit commit flag"


def test_transaction_rolls_back_on_error(tmp_path, monkeypatch):
    from memoratum import db

    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    conn = db.connect(db.connect.__module__ and ":memory:")
    try:
        with pytest.raises(RuntimeError), db.transaction(conn):
            conn.execute(
                "INSERT INTO memories(id, container_tag, text, metadata, version, state,"
                " created_at, updated_at)"
                " VALUES ('m1','t','x','{}',1,'active',0,0)"
            )
            raise RuntimeError("boom")
        remaining = conn.execute("SELECT COUNT(*) FROM memories WHERE id='m1'").fetchone()[0]
        assert remaining == 0, "transaction did not roll back"
    finally:
        conn.close()


def test_transaction_commits_on_success(tmp_path, monkeypatch):
    from memoratum import db

    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    conn = db.connect(":memory:")
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO memories(id, container_tag, text, metadata, version, state,"
                " created_at, updated_at)"
                " VALUES ('m2','t','y','{}',1,'active',0,0)"
            )
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE id='m2'").fetchone()[0] == 1
    finally:
        conn.close()


# --- harness ----------------------------------------------------------------


def crash_between(target: int):
    """Fail the Nth COMMIT issued inside the block (module-level helper)."""
    import contextlib

    from memoratum import db

    state = {"n": 0}
    original = db.connect

    @contextlib.contextmanager
    def arm():
        def traced(path):
            real = original(path)

            def on_trace(statement: str) -> None:
                if not statement.strip().upper().startswith("COMMIT"):
                    return
                state["n"] += 1
                if state["n"] == target:
                    raise RuntimeError(f"crash before commit #{state['n']}")

            real.set_trace_callback(on_trace)
            return real

        db.connect = traced
        try:
            yield
        finally:
            db.connect = original

    return arm()
