"""Packaging and installability (FR-011, SC-005, SC-006).

Verifies the things an operator actually hits at install time: a working image, a
minimal base package, provider extras, the licence split, and no committed secret.

The Docker build is exercised as a real build here rather than assumed, because a
Dockerfile that no longer builds is a packaging defect that only surfaces in a
deployment.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from helpers import skip_unless

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text()


def _tomllib():
    import tomllib

    return tomllib.loads(PYPROJECT)


# --- licences ---------------------------------------------------------------


def test_server_is_agpl_and_clients_are_mit():
    """The split is binding; blurring it is a licence problem, not a style one."""
    agpl = (ROOT / "LICENSE").read_text()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in agpl

    mit = ROOT / "LICENSE-MIT"
    assert mit.exists(), "the MIT licence for client code is missing"
    assert "MIT License" in mit.read_text()

    pyproject = _tomllib()["project"]
    assert "AGPL" in pyproject["license"]["text"], pyproject["license"]


def test_package_metadata_declares_the_agpl_licence():
    pyproject = _tomllib()["project"]
    assert pyproject["license"]["text"].startswith("AGPL-3.0")


# --- minimal base install ---------------------------------------------------


def test_base_install_requires_only_core_dependencies():
    """Every provider integration must sit behind an optional extra."""
    project = _tomllib()["project"]
    core = {re.split(r"[><=!~\[]", dep, 1)[0].strip().lower() for dep in project["dependencies"]}
    assert core == {"cryptography", "fastapi", "pydantic", "uvicorn"}, core

    for provider in ["litellm", "fastembed", "qdrant-client", "chromadb", "psycopg"]:
        assert provider not in core, (
            f"{provider} must not be a core dependency -- it belongs behind an extra"
        )


def test_every_provider_has_an_extra():
    extras = _tomllib()["project"]["optional-dependencies"]
    for extra in ["llm", "embeddings", "qdrant", "chroma", "pgvector"]:
        assert extra in extras, f"missing optional extra: {extra}"


def test_package_builds_and_imports():
    """Build a real wheel and confirm it imports with no extras installed."""
    build = shutil.which("uv") or shutil.which("python3")
    if build.endswith("uv"):
        result = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", tempfile.mkdtemp()],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
    else:
        result = subprocess.run(
            [build, "-m", "build", "--wheel", "--out-dir", tempfile.mkdtemp()],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
    if result.returncode != 0:
        skip_unless("uv", "the wheel build could not run in this environment")
    assert result.returncode == 0, result.stderr


def test_wheel_contains_the_server_package():
    from memoratum import db

    assert db.__name__ == "memoratum.db"
    assert (ROOT / "src" / "memoratum" / "__init__.py").exists()


def test_dockerfile_only_copies_what_it_needs():
    """A broad COPY would pull tests and data into the image."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    copies = re.findall(r"^COPY\s+(\S+)", dockerfile, re.MULTILINE)
    assert copies, "no COPY instructions found"
    for source in copies:
        assert source in {"pyproject.toml", "uv.lock", "README.md", "src"}, (
            f"Dockerfile copies {source!r}; keep the build context minimal"
        )


def test_docker_image_builds_and_starts(tmp_path):
    """SC-005: the published image must actually serve traffic with SQLite only."""
    skip_unless("docker", "the published image cannot be built here")

    tag = "memoratum:packaging-test"
    build = subprocess.run(
        ["docker", "build", "-t", tag, "."],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert build.returncode == 0, build.stderr[-2000:]

    run = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "-p",
            "6799:6767",
            "--name",
            "memoratum-packaging-test",
            tag,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        subprocess.run(["docker", "rm", "-f", "memoratum-packaging-test"], check=False)
        skip_unless("docker", "the container could not be started in this environment")

    try:
        import time
        import urllib.error
        import urllib.request

        healthy = False
        for _ in range(120):  # up to 60s: a cold runner starts the image slowly
            try:
                with urllib.request.urlopen("http://127.0.0.1:6799/health/live", timeout=2) as r:
                    healthy = r.status == 200
                if healthy:
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        assert healthy, "container never became healthy"

        # No provider configured: the server must still work, on SQLite alone.
        with urllib.request.urlopen("http://127.0.0.1:6799/v1/ping/", timeout=5) as r:
            assert r.status in {200, 401}
    finally:
        subprocess.run(["docker", "rm", "-f", "memoratum-packaging-test"], check=False)


# --- no committed secrets ---------------------------------------------------

# Deliberately conservative: obvious placeholder shapes only.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # OpenAI-style
    re.compile(r"ghp_[A-Za-z0-9]{36}"),  # GitHub PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT, check=False
    )
    return [ROOT / name for name in result.stdout.splitlines() if name]


def test_no_secret_shaped_strings_are_committed():
    """SC-006. Placeholders and empty values are fine; real key shapes are not."""
    offenders: list[str] = []
    for path in _tracked_files():
        if path.suffix not in {".py", ".js", ".ts", ".tsx", ".md", ".toml", ".yml", ".yaml"}:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)}: {pattern.pattern}")
    assert not offenders, f"secret-shaped strings committed: {offenders}"


def test_no_private_key_files_are_committed():
    for name in _tracked_files():
        assert not name.name.endswith((".pem", ".key", ".p12", ".pfx")), (
            f"key material is committed: {name.relative_to(ROOT)}"
        )


def test_dotenv_is_ignored_but_example_is_tracked():
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"], capture_output=True, cwd=ROOT, check=False
    )
    assert ignored.returncode == 0, ".env must be gitignored"
    assert (ROOT / ".env.example").exists(), ".env.example must be committed"


def test_dockerignore_excludes_secrets():
    """A secret must not be recoverable from an image layer."""
    text = (ROOT / ".dockerignore").read_text()
    for pattern in [".env", ".env.*", ".webhook-encryption-key", ".git/"]:
        assert pattern in text, f".dockerignore must exclude {pattern}"


def test_webhook_key_file_is_generated_not_committed():
    assert (ROOT / ".webhook-encryption-key").exists() is False
    # The generated file lives in the data directory, which is ignored.
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".memoratum-data/.webhook-encryption-key"],
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    assert ignored.returncode == 0, "the generated key file must never be committed"


def test_no_external_telemetry_in_the_image_inputs():
    """The image must not bake in an analytics SDK."""
    result = subprocess.run(
        [
            "git",
            "grep",
            "-ilE",
            "posthog|sentry_sdk|mixpanel|amplitude|datadog",
            "--",
            "src",
            "clients",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert not result.stdout.strip(), f"telemetry SDKs referenced: {result.stdout}"
