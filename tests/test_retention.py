"""Retention: every table has a policy, and timed tables actually age out.

Measured before this work: `memory_history` and `share_links` had **zero** DELETE
statements in the entire codebase, and `jobs`, `domain_events` and
`webhook_deliveries` were only removed by an explicit project purge. Nothing bounded
growth.

The policy table is configuration, not tenant data, so an unclassified table is a
test failure rather than a documentation gap someone has to notice.
"""

import time

import pytest
from fastapi.testclient import TestClient

# The 19 tables the schema defines, with the class each one must carry.
EXPECTED_POLICIES = {
    "organizations": "retain_indefinitely",
    "projects": "retain_indefinitely",
    "project_members": "cascaded",
    "api_keys": "retain_indefinitely",
    "revoked_keys": "timed",
    "webhooks": "retain_indefinitely",
    "documents": "retain_indefinitely",
    "chunks": "cascaded",
    "memories": "retain_indefinitely",
    "memory_history": "timed",
    "facts": "retain_indefinitely",
    "vector_points": "cascaded",
    "jobs": "timed",
    "domain_events": "timed",
    "webhook_deliveries": "timed",
    "idempotency_keys": "timed",
    "share_links": "timed",
    "audit_events": "timed",
    "usage_counters": "retain_indefinitely",
}


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_API_KEY", "admin-key")
    from memoratum.app import create_app

    return TestClient(create_app())


def _admin() -> dict[str, str]:
    return {"Authorization": "Token admin-key"}


def _conn():
    from memoratum import db
    from memoratum.config import Settings

    return db.connect(Settings.load().db_path)


# FTS5 creates shadow tables alongside the virtual table. They are an
# implementation detail of chunks_fts, rebuilt with it, and are not separately
# governed -- so they are excluded rather than given a policy.
_FTS_SHADOW_SUFFIXES = ("_config", "_data", "_docsize", "_idx", "_content")


def _schema_tables(conn) -> set[str]:
    """Real tables only: no SQLite internals, no FTS5 shadow tables."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows} - {"sqlite_sequence", "schema_migrations"}
    virtual = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE sql LIKE '%VIRTUAL TABLE%'")
    }
    shadows = {f"{v}{suffix}" for v in virtual for suffix in _FTS_SHADOW_SUFFIXES} | virtual
    return names - shadows


# --- the policy registry ----------------------------------------------------


def test_policy_table_exists_with_expected_schema():
    from memoratum import db

    conn = db.connect(":memory:")
    try:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(retention_policies)")}
        assert {"table_name", "policy_class", "retention_days", "rationale"} <= columns
    finally:
        conn.close()


def test_every_table_has_a_policy(tmp_path, monkeypatch):
    """A future migration that adds a table without a policy must fail here."""
    _client(tmp_path, monkeypatch)  # bootstraps the data dir and schema
    conn = _conn()
    try:
        tables = _schema_tables(conn)
        unregistered = []
        for table in sorted(tables):
            row = conn.execute(
                "SELECT policy_class FROM retention_policies WHERE table_name = ?", (table,)
            ).fetchone()
            if row is None:
                unregistered.append(table)
        assert not unregistered, f"tables with no retention policy: {unregistered}"
    finally:
        conn.close()


def test_policy_classes_match_the_documented_assignment(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)  # bootstraps the data dir and schema
    conn = _conn()
    try:
        for table, expected in EXPECTED_POLICIES.items():
            row = conn.execute(
                "SELECT policy_class FROM retention_policies WHERE table_name = ?", (table,)
            ).fetchone()
            assert row is not None, f"{table} has no policy"
            assert row["policy_class"] == expected, (
                f"{table}: expected {expected}, found {row['policy_class']}"
            )
    finally:
        conn.close()


def test_every_policy_has_a_rationale(tmp_path, monkeypatch):
    """An undocumented policy is a failed policy."""
    _client(tmp_path, monkeypatch)  # bootstraps the data dir and schema
    conn = _conn()
    try:
        rows = conn.execute("SELECT table_name, rationale FROM retention_policies").fetchall()
        assert rows
        empty = [r["table_name"] for r in rows if not (r["rationale"] or "").strip()]
        assert not empty, f"policies with no rationale: {empty}"
    finally:
        conn.close()


def test_timed_policies_declare_a_positive_window(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)  # bootstraps the data dir and schema
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT table_name, retention_days FROM retention_policies WHERE policy_class = 'timed'"
        ).fetchall()
        assert rows, "no timed policies registered"
        bad = [r["table_name"] for r in rows if not r["retention_days"] or r["retention_days"] <= 0]
        assert not bad, f"timed policies with no window: {bad}"
    finally:
        conn.close()


def test_invalid_policy_class_is_rejected():
    from memoratum import db

    conn = db.connect(":memory:")
    try:
        with pytest.raises(Exception):  # noqa: B017 - sqlite IntegrityError
            conn.execute(
                "INSERT INTO retention_policies(table_name, policy_class, rationale)"
                " VALUES ('bogus', 'whenever', 'nope')"
            )
    finally:
        conn.close()


# --- the sweep --------------------------------------------------------------


def _seed_project(conn, project_id: str = "retention-proj") -> None:
    """domain_events and webhooks reference projects, so seed a real one."""
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO projects(id, org_id, name, created_at, updated_at)"
        " VALUES (?, 'local-org', 'Retention', ?, ?)",
        (project_id, now, now),
    )
    conn.commit()


def _seed_terminal_rows(conn, *, days_old: int = 400) -> None:
    """Seed terminal rows old enough to be swept, plus live rows that must not be."""
    _seed_project(conn)
    old = time.time() - days_old * 86400

    for index in range(5):
        conn.execute(
            "INSERT INTO jobs(id, kind, payload, status, attempts, created_at, updated_at,"
            " run_after, lease_expires_at)"
            " VALUES (?, 'ingest', '{}', 'done', 1, ?, ?, 0, 0)",
            (f"old-job-{index}", old, old),
        )
        conn.execute(
            "INSERT INTO jobs(id, kind, payload, status, attempts, created_at, updated_at,"
            " run_after, lease_expires_at)"
            " VALUES (?, 'ingest', '{}', 'queued', 0, ?, ?, 0, 0)",
            (f"live-job-{index}", time.time(), time.time()),
        )

    conn.execute(
        "INSERT INTO memories(id, container_tag, text, metadata, version, state,"
        " created_at, updated_at) VALUES ('m-hist','t','v1','{}',1,'superseded',?,?)",
        (old, old),
    )
    conn.execute(
        "INSERT INTO memory_history(id, memory_id, event, new_text, metadata, version,"
        " created_at) VALUES ('h-old','m-hist','update','v1','{}',1,?)",
        (old,),
    )
    conn.execute(
        "INSERT INTO memory_history(id, memory_id, event, new_text, metadata, version,"
        " created_at) VALUES ('h-new','m-hist','update','v2','{}',2,?)",
        (time.time(),),
    )
    conn.commit()


def test_terminal_jobs_are_swept_and_live_ones_kept(tmp_path, monkeypatch):
    from memoratum import retention

    _client(tmp_path, monkeypatch)  # bootstraps the data dir and schema
    conn = _conn()
    try:
        _seed_terminal_rows(conn)
        report = retention.sweep(conn)
        assert report["deleted"]["jobs"] == 5, (
            f"expected 5 terminal jobs swept, got {report['jobs']}"
        )

        remaining = conn.execute("SELECT COUNT(*) FROM jobs WHERE id LIKE 'old-job-%'").fetchone()[
            0
        ]
        assert remaining == 0
        live = conn.execute("SELECT COUNT(*) FROM jobs WHERE id LIKE 'live-job-%'").fetchone()[0]
        assert live == 5, "queued jobs must never be swept"
    finally:
        conn.close()


def test_memory_history_ages_out(tmp_path, monkeypatch):
    """This table had zero DELETE statements in the entire codebase."""
    from memoratum import retention

    _client(tmp_path, monkeypatch)  # bootstraps the data dir and schema
    conn = _conn()
    try:
        _seed_terminal_rows(conn)
        retention.sweep(conn)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM memory_history WHERE id = 'h-old'"
        ).fetchone()[0]
        assert remaining == 0, "old memory_history survived the sweep"
        fresh = conn.execute("SELECT COUNT(*) FROM memory_history WHERE id = 'h-new'").fetchone()[0]
        assert fresh == 1, "recent memory_history must be retained"
    finally:
        conn.close()


def test_queued_deliveries_are_never_pruned(tmp_path, monkeypatch):
    """Deleting a queued delivery would silently drop an obligation."""
    from memoratum import retention

    _client(tmp_path, monkeypatch)  # bootstraps the data dir and schema
    conn = _conn()
    try:
        now = time.time()
        old = now - 400 * 86400
        _seed_project(conn, "p")
        conn.execute(
            "INSERT INTO webhooks(id, project_id, name, url, event_types, secret,"
            " created_at, updated_at) VALUES ('w','p','h','https://e.example/h','[]','x',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO domain_events(id, project_id, event_type, payload, created_at)"
            " VALUES ('e-old','p','memory_add','{}',?)",
            (old,),
        )
        conn.execute(
            "INSERT INTO domain_events(id, project_id, event_type, payload, created_at)"
            " VALUES ('e-live','p','memory_add','{}',?)",
            (now,),
        )
        for event_id, status in [("e-old", "succeeded"), ("e-live", "queued")]:
            conn.execute(
                "INSERT INTO webhook_deliveries(id, event_id, webhook_id, project_id,"
                " event_type, payload, status, attempts, created_at, updated_at)"
                " VALUES (?,?,?,'p','memory_add','{}',?,1,?,?)",
                (
                    f"d-{event_id}",
                    event_id,
                    "w",
                    status,
                    old if event_id == "e-old" else now,
                    old if event_id == "e-old" else now,
                ),
            )
        conn.commit()

        retention.sweep(conn)

        queued = conn.execute(
            "SELECT COUNT(*) FROM webhook_deliveries WHERE status = 'queued'"
        ).fetchone()[0]
        assert queued == 1, "a queued delivery was pruned"
        terminal = conn.execute(
            "SELECT COUNT(*) FROM webhook_deliveries WHERE status = 'succeeded'"
        ).fetchone()[0]
        assert terminal == 0, "an old terminal delivery survived"
    finally:
        conn.close()


def test_sweep_respects_foreign_key_order(tmp_path, monkeypatch):
    """Deliveries reference events; removing the parent first would orphan."""
    from memoratum import retention

    _client(tmp_path, monkeypatch)  # bootstraps the data dir and schema
    conn = _conn()
    try:
        _seed_terminal_rows(conn)
        retention.sweep(conn)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == [], f"sweep left FK violations: {violations}"
    finally:
        conn.close()


def test_dry_run_deletes_nothing(tmp_path, monkeypatch):
    from memoratum import retention

    _client(tmp_path, monkeypatch)  # bootstraps the data dir and schema
    conn = _conn()
    try:
        _seed_terminal_rows(conn)
        before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        report = retention.sweep(conn, dry_run=True)
        assert report["dry_run"] is True
        assert report["would_delete"]["jobs"] == 5
        after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert after == before, "dry run modified data"
    finally:
        conn.close()


def test_sweep_is_idempotent(tmp_path, monkeypatch):
    from memoratum import retention

    _client(tmp_path, monkeypatch)  # bootstraps the data dir and schema
    conn = _conn()
    try:
        _seed_terminal_rows(conn)
        first = retention.sweep(conn)
        second = retention.sweep(conn)
        assert first["deleted"]["jobs"] == 5
        assert second["deleted"]["jobs"] == 0, "second sweep should find nothing left"
    finally:
        conn.close()


def test_sweep_leaves_innermost_content_alone(tmp_path, monkeypatch):
    """memories/documents are user content: retention must never touch them."""
    from memoratum import retention

    _client(tmp_path, monkeypatch)  # bootstraps the data dir and schema
    conn = _conn()
    try:
        old = time.time() - 800 * 86400
        conn.execute(
            "INSERT INTO memories(id, container_tag, text, metadata, version, state,"
            " created_at, updated_at) VALUES ('m-keep','t','precious','{}',1,'active',?,?)",
            (old, old),
        )
        conn.commit()
        retention.sweep(conn)
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE id='m-keep'").fetchone()[0] == 1
    finally:
        conn.close()


# --- the endpoint -----------------------------------------------------------


def test_prune_endpoint_requires_admin(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    key = client.post("/v4/keys", headers=_admin(), json={"containerTag": "mem0:user_id:x"}).json()[
        "key"
    ]
    response = client.post("/v4/maintenance/prune", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code in {401, 403}, response.text


def test_prune_endpoint_supports_dry_run(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    conn = _conn()
    try:
        _seed_terminal_rows(conn)
    finally:
        conn.close()

    dry = client.post("/v4/maintenance/prune?dry_run=true", headers=_admin())
    assert dry.status_code == 200, dry.text
    assert dry.json()["dry_run"] is True
    assert dry.json()["would_delete"]["jobs"] == 5

    conn = _conn()
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM jobs WHERE id LIKE 'old-job-%'").fetchone()[0] == 5
        )
    finally:
        conn.close()

    wet = client.post("/v4/maintenance/prune", headers=_admin())
    assert wet.status_code == 200, wet.text
    assert wet.json()["deleted"]["jobs"] == 5
