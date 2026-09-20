"""Graph import endpoint contract (RED)."""

import json
import os
import tempfile


def _client():
    from fastapi.testclient import TestClient

    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.pop("MEMORATUM_API_KEY", None)
    from memoratum.app import create_app

    try:
        return TestClient(create_app())
    finally:
        os.environ.pop("MEMORATUM_DATA_DIR", None)


def _graph_dir():
    d = tempfile.mkdtemp()
    graph = {
        "built_at_commit": "c0ffee",
        "nodes": [
            {"id": "a_main", "label": "main", "source_file": "a.py"},
            {"id": "a_help", "label": "helper", "source_file": "a.py"},
        ],
        "links": [
            {
                "source": "a_main",
                "target": "a_help",
                "relation": "calls",
                "weight": 1.0,
                "confidence_score": 1.0,
            }
        ],
    }
    with open(os.path.join(d, "graph.json"), "w") as f:
        json.dump(graph, f)
    return d


def test_import_graph_dir() -> None:
    c = _client()
    r = c.post("/v4/import", json={"graph_dir": _graph_dir(), "tag": "t"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["facts_upserted"] == 1
    q = c.post(
        "/v4/search",
        json={"q": "main helper", "containerTag": "graphify:t", "searchMode": "memories"},
    )
    assert any("main @ a.py" in h.get("memory", "") for h in q.json()["results"])
    again = c.post("/v4/import", json={"graph_dir": _graph_dir(), "tag": "t"})
    assert again.json()["facts_deleted"] == 0


def test_import_rejects_bad_dir() -> None:
    c = _client()
    assert c.post("/v4/import", json={"graph_dir": "/nonexistent", "tag": "t"}).status_code == 422
    assert c.post("/v4/import", json={"tag": "t"}).status_code == 422
