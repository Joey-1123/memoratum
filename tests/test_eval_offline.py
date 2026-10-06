"""SC-006 / SC-010 / FR-011: the axes run on SQLite alone, and nothing leaves the host.

Three claims, each verified rather than asserted:

* **No outbound socket.** Every axis runs with ``socket.socket`` replaced by a raising
  stub. This is stronger than checking a telemetry script, because it catches a connection
  attempt anywhere in the call stack — including inside a provider SDK that happens to be
  installed.
* **No credential is read.** ``MEMORATUM_*_KEY`` and friends are set to a sentinel and the
  environment is asserted untouched by the run.
* **The remote backends are not needed.** ``openai`` and ``qdrant-client`` *are* installed
  in this environment, so "SQLite only" cannot be demonstrated by their absence — it has to
  be demonstrated by the axes completing with them installed and unreachable.

This is the check that matters for a self-hosted product: a benchmark harness that quietly
uploads a corpus is worse than no harness, because the numbers look fine.
"""

from __future__ import annotations

import io
import os
import socket
import tempfile
from contextlib import redirect_stderr, redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL = os.path.join(REPO_ROOT, "eval")
FIXTURE = os.path.join(EVAL, "fixtures", "longmemeval_smoke.json")

#: Credentials the axes must never read. A distinctive value, so "was this touched?" is
#: answerable rather than assumed.
_SENTINEL_KEYS = {
    "MEMORATUM_EMBEDDINGS_KEY": "sentinel-not-a-real-key",
    "OPENAI_API_KEY": "sentinel-not-a-real-key",
    "MEMORATUM_API_KEY": "sentinel-not-a-real-key",
    "HF_TOKEN": "sentinel-not-a-real-key",
    "MEMORATUM_EMBEDDING_PROVIDER": "",  # deliberately empty: must not select a provider
}

AXIS_ARGV = {
    "eval_isolation": ["--projects", "2", "--sessions-per-project", "3", "--queries", "2"],
    "eval_cost": ["--data", FIXTURE, "--n", "1", "--seed", "42"],
    "eval_latency": ["--ladder", "100,200,400", "--samples", "120", "--warmup", "5"],
    "eval_grounding": ["--data", FIXTURE, "--n", "1", "--seed", "42", "--inject-ungrounded"],
}

_TEMP_ROOTS: list[tempfile.TemporaryDirectory] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_temp_roots():
    yield
    while _TEMP_ROOTS:
        _TEMP_ROOTS.pop().cleanup()


def _tempdir() -> str:
    holder = tempfile.TemporaryDirectory(prefix="memoratum-offline-")
    _TEMP_ROOTS.append(holder)
    return holder.name


class _NoNetwork(socket.socket):  # type: ignore[misc]
    """A socket that refuses to exist.

    Subclassing rather than replacing the class keeps ``isinstance`` checks working while
    making every connection attempt fail loudly.
    """

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "the evaluation harness opened a socket; benchmark data must never leave the host "
            "(FR-010)"
        )


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(socket, "socket", _NoNetwork)
    monkeypatch.setattr(socket, "create_connection", _forbidden("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden("getaddrinfo"))


def _forbidden(name: str):
    def refuse(*args, **kwargs):
        raise AssertionError(f"the harness called socket.{name}; nothing may leave the host")

    return refuse


@pytest.fixture
def sentinel_credentials(monkeypatch):
    for key, value in _SENTINEL_KEYS.items():
        monkeypatch.setenv(key, value)
    return dict(_SENTINEL_KEYS)


def _run(axis: str, extra: list[str] | None = None) -> int:
    imported = __import__(f"memoratum.{axis}", fromlist=["main"])
    argv = [
        *AXIS_ARGV[axis],
        *(extra or []),
        "--out-json",
        os.path.join(_tempdir(), f"{axis}.json"),
    ]
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = imported.main(argv)
    assert code in (0, 1), f"{axis} exited {code}: {stderr.getvalue() or stdout.getvalue()}"
    return code


@pytest.mark.parametrize("axis", sorted(AXIS_ARGV))
def test_each_axis_runs_with_every_socket_blocked(axis, no_network, sentinel_credentials) -> None:
    """The claim, exercised: the axis completes and nothing dials out.

    Not skipped when the corpus is absent — the committed fixture is enough for every axis,
    and a skip here would be exactly the "unverified means unproven" hole.
    """
    _run(axis)


def test_a_socket_attempt_is_actually_detected(no_network) -> None:
    """The negative control: the blocker must be capable of blocking.

    Without this, `no_network` could be a no-op fixture and every test above would pass
    while the harness happily uploaded the corpus.
    """
    with pytest.raises(AssertionError, match="opened a socket"):
        socket.socket()


def test_the_environment_keys_are_unmodified_after_a_run(monkeypatch, sentinel_credentials) -> None:
    """A run must not consume, rewrite, or unset a credential it must not read."""
    import os as _os

    before = {key: _os.environ.get(key) for key in _SENTINEL_KEYS}
    _run("eval_cost")
    after = {key: _os.environ.get(key) for key in _SENTINEL_KEYS}
    assert before == after, f"the run modified its environment: {before} -> {after}"


def test_the_provider_extras_are_installed_so_this_is_not_vacuous() -> None:
    """Documented precondition. If these were absent, the socket tests would prove less."""
    import importlib.util

    for module in ("openai", "qdrant_client"):
        assert importlib.util.find_spec(module) is not None, (
            f"{module} is not installed here, so 'the axes never reach for it' is being "
            "demonstrated by absence rather than by a blocked network"
        )


def test_the_axes_use_sqlite_and_record_it_in_the_manifest() -> None:
    """No committed axis manifest may claim a remote backend.

    M2 hard-compares this field, so a false one is a false reproducibility record. The cost
    axis was corrected for exactly this after it recorded `SQLiteVectorStore` while
    `search()` ran with no factory; the isolation axis carried the same record until this
    test found it.
    """
    import json

    checked = 0
    for name in sorted(os.listdir(EVAL)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(EVAL, name)
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        manifest = payload.get("manifest") or {}
        store = manifest.get("vector_store")
        if store is None:
            continue
        checked += 1
        assert "sqlite" in str(store).lower(), (
            f"eval/{name} claims vector_store={store!r}; the axes run on SQLite and must "
            "not be comparable to a remote-backend measurement"
        )
    assert checked >= 3, f"only {checked} manifests carried a vector_store field to check"


def test_no_benchmark_data_is_tracked_in_the_repository() -> None:
    """FR-010: the corpora are never committed.

    `data/` is gitignored in full — including `download.log`, which is present locally and
    untracked. So the assertion is that *nothing* under `data/` is tracked, rather than
    "only download.log", which would have been satisfied by an empty result.
    """
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "data/"], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )
    files = [line for line in tracked.stdout.split() if line.strip()]
    assert files == [], f"benchmark data is tracked in git: {files}"

    # The corpus legitimately *lives* in the working tree and is gitignored. Asserting it
    # is absent would be asserting the wrong thing, and asserting it is present would make
    # the test depend on a 277 MB download. What matters is that git ignores it.
    data_dir = os.path.join(REPO_ROOT, "data")
    if os.path.isdir(data_dir):
        present = sorted(os.listdir(data_dir))
        assert present, "data/ exists but is empty; the tracking check above cannot be trusted"
        for name in present:
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", os.path.join("data", name)],
                cwd=REPO_ROOT,
                check=False,
            )
            assert ignored.returncode == 0, (
                f"data/{name} is not gitignored; a corpus must never be committable"
            )


def test_the_telemetry_check_is_clean() -> None:
    """The repository's own scan, run rather than cited."""
    import subprocess

    proc = subprocess.run(
        ["uv", "run", "python", "scripts/check_no_telemetry.py"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
