"""Shared test helpers (not tests themselves)."""

import os


def drain() -> None:
    """Run the worker until the queue is empty (async ingest in tests)."""
    from memoratum import db
    from memoratum.app import build_embedder
    from memoratum.config import Settings
    from memoratum.worker import build_llm, run_once

    settings = Settings.load()
    conn = db.connect(settings.db_path)
    try:
        while (
            run_once(conn, build_embedder(settings), build_llm(settings), worker_id="t") is not None
        ):
            pass
    finally:
        conn.close()


def use_tmp_data_dir() -> str:
    import tempfile

    d = tempfile.mkdtemp()
    os.environ["MEMORATUM_DATA_DIR"] = d
    return d
