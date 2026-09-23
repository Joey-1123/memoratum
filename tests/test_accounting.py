"""Local audit and usage accounting contract (RED)."""

import os
import tempfile


def _db():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "accounting.db"))


def test_usage_rolls_up_counters_by_key_and_scope() -> None:
    from memoratum import db

    conn = _db()
    db.record_usage(
        conn,
        key_hash="key-a",
        container_tag="t",
        org_id="org-a",
        operation="document",
        input_chars=12,
    )
    db.record_usage(
        conn,
        key_hash="key-a",
        container_tag="t",
        org_id="org-a",
        operation="search",
        input_chars=4,
    )
    rows = db.list_usage(conn, container_tag="t", org_id="org-a")
    assert len(rows) == 1
    assert rows[0]["requests"] == 2
    assert rows[0]["document_writes"] == 1
    assert rows[0]["searches"] == 1
    assert rows[0]["input_chars"] == 16
    assert rows[0]["key_hash"] == "key-a"
    conn.close()


def test_usage_scopes_are_isolated() -> None:
    from memoratum import db

    conn = _db()
    db.record_usage(conn, key_hash="key-a", container_tag="t", org_id="org-a", operation="request")
    db.record_usage(conn, key_hash="key-a", container_tag="t", org_id="org-b", operation="request")
    db.record_usage(conn, key_hash="key-b", container_tag="t", org_id="org-a", operation="request")
    assert len(db.list_usage(conn, container_tag="t", org_id="org-a")) == 2
    assert len(db.list_usage(conn, container_tag="t", org_id="org-b")) == 1
    assert len(db.list_usage(conn, container_tag="other")) == 0
    conn.close()


def test_audit_events_are_filterable_and_metadata_is_json() -> None:
    from memoratum import db

    conn = _db()
    event_id = db.append_audit_event(
        conn,
        actor_kind="key",
        actor_key_hash="key-a",
        container_tag="t",
        org_id="org-a",
        action="document.created",
        resource_type="document",
        resource_id="doc-a",
        metadata={"source": "test"},
    )
    db.append_audit_event(
        conn,
        actor_kind="admin",
        actor_key_hash="key-admin",
        container_tag="t",
        org_id="org-b",
        action="key.issued",
        resource_type="api_key",
        resource_id="key-b",
    )
    rows = db.list_audit_events(conn, container_tag="t", org_id="org-a")
    assert len(rows) == 1
    assert rows[0]["id"] == event_id
    assert rows[0]["action"] == "document.created"
    assert rows[0]["metadata"] == {"source": "test"}
    assert rows[0]["actor_key_hash"] != "key-a"
    conn.close()
