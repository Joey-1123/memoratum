"""Bridge importer contract (RED)."""

import json


class FakeAPI:
    def __init__(self, existing=()):
        self.posted = []
        self.deleted = []
        self.existing = list(existing)

    def post(self, path, body):
        self.posted.append((path, body))
        return {"id": f"n{len(self.posted)}"}

    def get(self, path, params=None):
        assert path == "/v4/facts"
        return {"facts": self.existing, "total": len(self.existing)}

    def delete(self, path):
        self.deleted.append(path)
        return {"deleted": True}


def _graph():
    with open("tests/fixtures/graph.json") as f:
        return json.load(f)


def test_mapping_yields_qualified_facts() -> None:
    from memoratum.bridge import fact_records

    recs = fact_records(_graph(), slug="demo", commit="abc123")
    assert len(recs) == 2
    first = recs[0]
    assert first["subject"] == "main @ a.py"
    assert first["predicate"] == "calls"
    assert first["object"] == "helper @ a.py"
    assert first["containerTag"] == "graphify:demo"
    assert first["metadata"]["confidence"] == 1.0


def test_sync_upserts_report_and_deletes_vanished() -> None:
    from memoratum.bridge import sync_graph

    stale = {
        "id": "old1",
        "subject": "main @ a.py",
        "predicate": "calls",
        "object": "gone @ z.py",
        "metadata": {"graphify": True},
    }
    api = FakeAPI(existing=[stale])
    summary = sync_graph(api, _graph(), slug="demo")
    assert summary["facts_upserted"] == 2
    assert summary["facts_deleted"] == 1
    assert api.deleted == ["/v4/memories/old1"]
    assert any(p == "/v3/documents" for p, _ in api.posted)


def test_rerun_is_noop_diff() -> None:
    from memoratum.bridge import fact_records, sync_graph

    recs = fact_records(_graph(), slug="demo", commit="abc123")
    existing = [{"id": f"n{i}", **r} for i, r in enumerate(recs)]
    api = FakeAPI(existing=existing)
    summary = sync_graph(api, _graph(), slug="demo")
    assert summary["facts_deleted"] == 0
