"""Shared test helpers (not tests themselves)."""

import contextlib
import os
import time


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


@contextlib.contextmanager
def count_commits():
    """Yield a list that accumulates one entry per COMMIT on connections from ``db.connect``.

    ``sqlite3.Connection.commit`` is an immutable C method and cannot be
    monkeypatched, so counting uses ``set_trace_callback`` instead. That also
    catches COMMITs issued implicitly by ``executescript``.
    """
    from memoratum import db

    counter: list[int] = []
    original = db.connect

    def traced(path):
        conn = original(path)

        def on_trace(statement: str) -> None:
            if statement.strip().upper().startswith("COMMIT"):
                counter.append(1)

        conn.set_trace_callback(on_trace)
        return conn

    db.connect = traced
    try:
        yield counter
    finally:
        db.connect = original


@contextlib.contextmanager
def frozen_time(start: float = 1_000_000.0):
    """Yield a one-element list holding the current fake epoch time.

    ``time.time`` is patched for the duration so lease-expiry assertions are
    deterministic and never sleep. Mutate the yielded list to advance the clock.

    Note this patches the real ``time.time`` globally for the duration, so it must
    not be used around code that measures its own wall-clock duration.
    """
    clock = [start]
    original = time.time
    time.time = lambda: clock[0]  # type: ignore[assignment]
    try:
        yield clock
    finally:
        time.time = original  # type: ignore[assignment]
