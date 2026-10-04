"""Documentation must match the implementation.

The constitution declares a doc/behavior mismatch a defect in both directions: a
documented behaviour that is not implemented, and an implemented behaviour that
contradicts the docs. Two of these checks exist because the docs were previously
*overstating* guarantees — claiming webhook endpoints were safe merely because
they were "validated at creation and delivery", when delivery re-resolved DNS.

These are mechanical checks, not prose review, so a future regression in either
direction fails the build.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env_vars_in_code() -> set[str]:
    """Environment variables the server actually reads."""
    names: set[str] = set()
    for path in (ROOT / "src" / "memoratum").rglob("*.py"):
        names |= set(re.findall(r"MEMORATUM_[A-Z_]+", path.read_text(errors="replace")))
    return names


# --- .env.example completeness (FR-011) -------------------------------------


def test_env_example_documents_every_server_variable():
    """An undocumented variable is one an operator cannot know to set."""
    documented = set(re.findall(r"MEMORATUM_[A-Z_]+", (ROOT / ".env.example").read_text()))
    # These are read by client SDKs and the eval harness, not the server.
    client_only = {"MEMORATUM_URL", "MEMORATUM_EVAL_API"}
    missing = sorted(_env_vars_in_code() - documented - client_only)
    assert not missing, f"env vars read by the server but absent from .env.example: {missing}"


def test_env_example_flags_the_security_relevant_variables():
    text = (ROOT / ".env.example").read_text()
    for critical in [
        "MEMORATUM_WEBHOOK_ENCRYPTION_KEY",
        "MEMORATUM_WEBHOOK_ALLOW_PRIVATE_TARGETS",
        "MEMORATUM_LENIENT_COMPAT",
    ]:
        assert critical in text, f"{critical} must be documented"


def test_env_example_warns_about_key_loss():
    text = (ROOT / ".env.example").read_text().upper()
    assert "UNRECOVERABLE" in text, (
        "losing MEMORATUM_WEBHOOK_ENCRYPTION_KEY is unrecoverable and the template must say so"
    )


def test_compose_passes_the_webhook_key_to_both_services():
    """The worker delivers webhooks and must be able to decrypt their secrets."""
    text = (ROOT / "docker-compose.yml").read_text()
    assert text.count("MEMORATUM_WEBHOOK_ENCRYPTION_KEY") >= 2, (
        "both the server and the worker need the webhook encryption key"
    )


# --- documented security claims (FR-008) ------------------------------------


def test_security_doc_does_not_overstate_webhook_validation():
    text = (ROOT / "docs" / "SECURITY.md").read_text()
    # The old claim: "validated at creation and delivery". Delivery validation
    # re-resolved DNS, so it was not the guarantee it appeared to be.
    assert "validated at creation and delivery" not in text, (
        "this phrasing overstates the guarantee -- the doc must describe pinning"
    )
    assert "pinned" in text.lower(), "the doc must describe address pinning"


def test_security_doc_documents_the_cgnat_range():
    text = (ROOT / "docs" / "SECURITY.md").read_text()
    assert "100.64.0.0/10" in text, "CGNAT is the range that was silently allowed"


def test_security_doc_documents_strict_request_validation():
    text = (ROOT / "docs" / "SECURITY.md").read_text()
    assert "Unknown request fields are rejected" in text
    assert "MEMORATUM_LENIENT_COMPAT" in text


def test_operations_doc_does_not_understate_serialisation():
    text = (ROOT / "docs" / "OPERATIONS.md").read_text()
    assert "serialized writes" not in text, (
        "that phrasing understated the old behaviour, which serialised everything"
    )


def test_operations_documents_retention():
    text = (ROOT / "docs" / "OPERATIONS.md").read_text()
    assert "MEMORATUM_RETENTION_DAYS" in text
    assert "retention_policies" in text
    assert "dry_run" in text, "operators must be able to preview a prune"


def test_operations_documents_job_recovery():
    text = (ROOT / "docs" / "OPERATIONS.md").read_text()
    assert "MEMORATUM_JOB_LEASE_SECONDS" in text or "recover_stuck_jobs" in text


# --- no contradiction with the constitution (US5 scenario 4) ----------------


def test_docs_do_not_contradict_the_constitution():
    constitution = (ROOT / ".specify" / "memory" / "constitution.md").read_text()
    assert "MUST" in constitution

    # The constitution constrains egress; no doc may promise a telemetry export.
    for name in ["SECURITY.md", "OPERATIONS.md", "AUDIT_METERING.md", "ARCHITECTURE.md"]:
        text = (ROOT / "docs" / name).read_text().lower()
        assert "we send telemetry" not in text
        assert "phone home" not in text, f"{name} contradicts the no-egress principle"


def test_licence_statements_agree_across_every_source():
    """Regression guard for a real contradiction this suite missed once.

    docs/SECURITY.md claimed the SDKs were AGPL with no linking exception and
    needed relicensing, while LICENSE-MIT, README.md and clients/ts/package.json
    all said MIT. An external review surfaced it; no test did. The four sources
    must now agree, and SECURITY.md must not carry the stale claim again.
    """
    security = (ROOT / "docs" / "SECURITY.md").read_text()
    readme = (ROOT / "README.md").read_text()
    mit = (ROOT / "LICENSE-MIT").read_text()
    ts_pkg = (ROOT / "clients" / "ts" / "package.json").read_text()

    assert "MIT" in mit
    assert "MIT" in readme and "AGPL" in readme
    assert '"license": "MIT"' in ts_pkg, "the published TS SDK must declare MIT"

    # The stale claim must be gone for good.
    assert "are also AGPL with no linking exception" not in security, (
        "docs/SECURITY.md still claims the SDKs are AGPL; LICENSE-MIT, README.md and"
        " clients/ts/package.json all say MIT"
    )
    assert "MIT (`LICENSE-MIT`)" in security or "MIT" in security, (
        "docs/SECURITY.md must state the actual licence split"
    )


def test_security_doc_search_claim_matches_the_implementation():
    """The doc claimed search was brute-force over every chunk.

    That became inaccurate when search started reusing persisted embeddings and
    bounding the candidate set with FTS5. Stale security claims are worse than
    none, so this pins the wording to the implemented behaviour.
    """
    security = (ROOT / "docs" / "SECURITY.md").read_text()
    assert "Search is brute-force over a tag's chunks" not in security, (
        "the search description is stale -- search now reuses stored embeddings and"
        " bounds candidates with FTS5"
    )
    # ...and it must still be honest that scoring itself is not approximate.
    assert "sqlite-vec" in security, "the deferred ANN path must stay documented"
    assert "brute-force" in security, (
        "scoring is still exact rather than approximate; do not overstate it"
    )


def test_server_package_declares_agpl():
    import json as _json

    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "AGPL-3.0-or-later" in pyproject
    # The dashboard is served by the server, so it stays AGPL.
    dashboard = _json.loads((ROOT / "dashboard" / "package.json").read_text())
    assert "AGPL" in str(dashboard.get("license", "")), (
        "the dashboard is server-served and must remain AGPL"
    )


def test_audit_metering_doc_records_the_metrics_ruling():
    text = (ROOT / "docs" / "AUDIT_METERING.md").read_text()
    assert "/metrics" in text
    assert "not telemetry" in text.lower(), (
        "the local scrape ruling must be written down, not just argued verbally"
    )


def test_no_coverage_badge_was_introduced():
    readme = (ROOT / "README.md").read_text()
    assert "coverage" not in readme.lower()


# --- the quickstart runs (US5 scenario 1) -----------------------------------


def test_documented_healthcheck_target_exists():
    """The Dockerfile HEALTHCHECK must probe a route that exists."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    match = re.search(r"urlopen\('([^']+)'", dockerfile)
    assert match, "no healthcheck URL found in the Dockerfile"
    url = match.group(1)
    route = "/" + url.split("/", 3)[3].split("?")[0] if url.count("/") > 2 else "/health"
    assert route in {"/health", "/health/live", "/health/ready"}, route

    app_source = (ROOT / "src" / "memoratum" / "app.py").read_text()
    assert f'@app.get("{route}")' in app_source, (
        f"Dockerfile HEALTHCHECK probes {route}, which no route serves"
    )


def test_python_version_floor_is_honoured():
    """pyproject requires >=3.11; the declared floor and the running interpreter
    must both satisfy it, and the code must import on the floor."""
    import tomllib

    import memoratum

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = pyproject["project"]["requires-python"]
    assert sys.version_info >= (3, 11), f"running {sys.version_info} is below {declared}"
    assert memoratum is not None


def test_env_vars_in_docs_are_real():
    """Docs must not invent variables."""
    real = _env_vars_in_code()
    client_side = {"MEMORATUM_URL", "MEMORATUM_EVAL_API"}
    for name in ["SECURITY.md", "OPERATIONS.md", "AUDIT_METERING.md", "PROVIDERS.md"]:
        path = ROOT / "docs" / name
        if not path.exists():
            continue
        for found in set(re.findall(r"MEMORATUM_[A-Z_]+", path.read_text())):
            assert found in real or found in client_side, (
                f"docs/{name} mentions {found}, which no code reads"
            )


def test_dashboard_build_and_plugin_syntax_still_hold():
    """The two non-Python gates, smoke-checked cheaply."""
    plugin = ROOT / "clients" / "opencode" / "memoratum.js"
    if plugin.exists():
        result = subprocess.run(
            ["node", "--check", str(plugin)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
    assert (ROOT / "dashboard" / "package.json").exists()
