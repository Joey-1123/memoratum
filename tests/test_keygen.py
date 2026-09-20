"""First-boot keygen contract (RED)."""

import os
import tempfile


def test_first_boot_generates_admin_key() -> None:
    import io
    from contextlib import redirect_stdout

    from fastapi.testclient import TestClient

    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.pop("MEMORATUM_API_KEY", None)
    try:
        from memoratum.app import create_app

        buf = io.StringIO()
        with redirect_stdout(buf):
            c = TestClient(create_app())
        printed = buf.getvalue()
        assert "mm_" in printed
        key = printed.split("mm_")[1].split()[0].strip("'\",")
        r = c.post(
            "/v4/keys",
            json={"containerTag": "proj-a"},
            headers={"Authorization": f"Bearer mm_{key}"},
        )
        assert r.status_code == 201, r.text
    finally:
        os.environ.pop("MEMORATUM_DATA_DIR", None)


def test_no_keygen_when_configured_or_existing() -> None:
    import io
    from contextlib import redirect_stdout

    from fastapi.testclient import TestClient

    os.environ["MEMORATUM_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.pop("MEMORATUM_API_KEY", None)
    try:
        from memoratum import db
        from memoratum.app import create_app
        from memoratum.config import Settings

        s = Settings.load()
        conn = db.connect(s.db_path)
        db.create_api_key(conn, container_tag=None)
        conn.close()
        buf = io.StringIO()
        with redirect_stdout(buf):
            TestClient(create_app())
        assert "mm_" not in buf.getvalue()
    finally:
        os.environ.pop("MEMORATUM_DATA_DIR", None)
