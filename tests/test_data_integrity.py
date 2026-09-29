"""Focused integrity tests for scope, expiration, and lifecycle edges."""

from __future__ import annotations

import time

import pytest


def test_create_memory_strips_identity_metadata() -> None:
    from memoratum import db

    conn = db.connect(":memory:")
    memory = db.create_memory(
        conn,
        text="private",
        container_tag="mem0:user_id:alice",
        metadata={"user_id": "spoofed", "category": "note"},
    )
    assert memory["metadata"] == {"category": "note"}
    history = db.list_memory_history(conn, memory["id"])
    assert history[0]["metadata"] == {"category": "note"}


def test_prune_expired_tolerates_dangling_project_reference() -> None:
    from memoratum import db

    conn = db.connect(":memory:")
    memory = db.create_memory(
        conn,
        text="expired orphan",
        container_tag="mem0:user_id:alice",
        project_id="project-does-not-exist",
        expires_at=time.time() - 10,
    )

    counts = db.prune_expired(conn)

    assert counts["memories"] == 1
    assert db.get_memory(conn, memory["id"])["state"] == "deleted"


def test_search_returns_canonical_memory_ids() -> None:
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.facts import add_fact
    from memoratum.search import search

    conn = db.connect(":memory:")
    fact = add_fact(
        conn,
        container_tag="mem0:user_id:alice",
        subject="user",
        predicate="loves",
        object="Paris",
        document_id=None,
    )
    canonical = db.resolve_memory_reference(conn, fact["id"])
    assert canonical is not None
    hits = search(
        conn,
        HashEmbedder(dims=16),
        "Paris",
        container_tag="mem0:user_id:alice",
        search_mode="memories",
        threshold=0.0,
    )
    assert hits
    assert all(hit["id"] == canonical["id"] for hit in hits)


def test_purge_project_validates_org_before_external_vector_delete() -> None:
    from memoratum import db, worker

    conn = db.connect(":memory:")
    now = time.time()
    conn.execute(
        "INSERT INTO organizations(id, name, created_at, updated_at) VALUES ('org-a', 'Org', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO projects(id, org_id, name, created_at, updated_at)"
        " VALUES ('p1', 'org-a', 'Project', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO documents(id, container_tag, content, status, created_at, updated_at, project_id)"
        " VALUES ('d1', 'tag', 'text', 'done', ?, ?, 'p1')",
        (now, now),
    )
    conn.commit()

    class Store:
        name = "test"

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def delete(self, **kwargs: object) -> int:
            self.calls.append(kwargs)
            return 0

    store = Store()
    with pytest.raises(ValueError, match="org_id"):
        worker._dispatch(
            conn,
            None,  # type: ignore[arg-type]
            None,
            {
                "id": "job-1",
                "kind": "purge_project",
                "payload": {"project_id": "p1", "org_id": "org-b"},
            },
            vector_store=store,  # type: ignore[arg-type]
        )

    assert store.calls == []
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
