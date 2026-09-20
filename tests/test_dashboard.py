"""Dashboard static mount contract (RED)."""

import os
import tempfile


def _client(dist_dir: str | None):
    from fastapi.testclient import TestClient

    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.pop("MEMORATUM_API_KEY", None)
    if dist_dir is None:
        os.environ.pop("MEMORATUM_DASHBOARD_DIR", None)
    else:
        os.makedirs(dist_dir, exist_ok=True)
        with open(os.path.join(dist_dir, "index.html"), "w") as f:
            f.write("<html>dash</html>")
        os.environ["MEMORATUM_DASHBOARD_DIR"] = dist_dir
    from memoratum.app import create_app

    try:
        return TestClient(create_app())
    finally:
        os.environ.pop("MEMORATUM_DATA_DIR", None)
        os.environ.pop("MEMORATUM_DASHBOARD_DIR", None)


def test_missing_dist_leaves_api_intact() -> None:
    c = _client(None)
    assert c.get("/health").json() == {"ok": True}
    assert c.get("/dashboard").status_code == 404


def test_present_dist_is_served() -> None:
    c = _client(os.path.join(tempfile.mkdtemp(), "dist"))
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "dash" in r.text
