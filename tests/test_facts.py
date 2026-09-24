"""Fact store contract (RED)."""

import os
import tempfile


def _db():
    from memoratum.db import connect

    return connect(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_add_and_list_facts() -> None:
    from memoratum.facts import add_fact, list_facts

    conn = _db()
    f = add_fact(
        conn,
        container_tag="u1",
        subject="user",
        predicate="loves",
        object="Paris",
        document_id=None,
    )
    assert f["valid_to"] is None and f["superseded_by"] is None
    assert len(list_facts(conn, "u1")) == 1
    conn.close()


def test_contradiction_supersedes_without_deleting() -> None:
    from memoratum.facts import add_fact, list_facts

    conn = _db()
    old = add_fact(
        conn,
        container_tag="u1",
        subject="user",
        predicate="works_at",
        object="Tencent",
        document_id=None,
    )
    new = add_fact(
        conn,
        container_tag="u1",
        subject="user",
        predicate="works_at",
        object="Moonshot",
        document_id=None,
    )
    assert new["id"] != old["id"]
    current = [f for f in list_facts(conn, "u1") if f["valid_to"] is None]
    assert [(f["subject"], f["predicate"], f["object"]) for f in current] == [
        ("user", "works_at", "Moonshot")
    ]
    history = list_facts(conn, "u1", include_superseded=True)
    assert len(history) == 2
    assert next(f for f in history if f["id"] == old["id"])["superseded_by"] == new["id"]
    conn.close()


def test_tags_are_isolated() -> None:
    from memoratum.facts import add_fact, list_facts

    conn = _db()
    add_fact(conn, container_tag="u1", subject="a", predicate="b", object="c", document_id=None)
    assert list_facts(conn, "u2") == []
    conn.close()


def test_readding_identical_fact_is_noop() -> None:
    from memoratum.facts import add_fact, list_facts

    conn = _db()
    first = add_fact(
        conn, container_tag="u1", subject="a", predicate="b", object="c", document_id=None
    )
    again = add_fact(
        conn, container_tag="u1", subject="a", predicate="b", object="c", document_id=None
    )
    assert again["id"] == first["id"]
    assert len(list_facts(conn, "u1", include_superseded=True)) == 1
    conn.close()


def test_multi_valued_predicates_coexist() -> None:
    from memoratum.facts import add_fact, list_facts

    conn = _db()
    add_fact(
        conn,
        container_tag="u1",
        subject="f",
        predicate="contains",
        object="a",
        document_id=None,
        supersede=False,
    )
    add_fact(
        conn,
        container_tag="u1",
        subject="f",
        predicate="contains",
        object="b",
        document_id=None,
        supersede=False,
    )
    assert len(list_facts(conn, "u1")) == 2
    conn.close()


def test_reassert_revives_superseded_fact() -> None:
    from memoratum.facts import add_fact, list_facts

    conn = _db()
    add_fact(
        conn,
        container_tag="u1",
        subject="user",
        predicate="works_at",
        object="Tencent",
        document_id=None,
    )
    add_fact(
        conn,
        container_tag="u1",
        subject="user",
        predicate="works_at",
        object="Moonshot",
        document_id=None,
    )
    revived = add_fact(
        conn,
        container_tag="u1",
        subject="user",
        predicate="works_at",
        object="Tencent",
        document_id=None,
    )
    assert revived["valid_to"] is None
    assert "Tencent" in [f["object"] for f in list_facts(conn, "u1")]
    conn.close()


def test_list_facts_supports_limit_and_offset() -> None:
    from memoratum.facts import add_fact, count_facts, list_facts

    conn = _db()
    for index in range(3):
        add_fact(
            conn,
            container_tag="u1",
            subject=f"s{index}",
            predicate="knows",
            object=str(index),
            document_id=None,
        )
    assert len(list_facts(conn, "u1", limit=2)) == 2
    assert [fact["subject"] for fact in list_facts(conn, "u1", limit=2, offset=2)] == ["s2"]
    assert count_facts(conn, "u1") == 3
    conn.close()
