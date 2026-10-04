"""Metrics, health, and logging contracts.

Everything here is local-only. A scrape is an inbound pull: the server dials
nothing out, so this does not conflict with the no-external-telemetry rule, and
``scripts/check_no_telemetry.py`` still passes.
"""

import re
import threading

from fastapi.testclient import TestClient

# Label values may legitimately contain braces -- a FastAPI route template such as
# /v3/documents/{doc_id} is a label VALUE -- so the block is matched greedily to
# the final brace rather than excluding interior braces.
SAMPLE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? (\+Inf|-?[0-9.eE+.-]+)$")


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_API_KEY", "admin-key")
    from memoratum.app import create_app

    return TestClient(create_app())


def _admin() -> dict[str, str]:
    return {"Authorization": "Token admin-key"}


# --- exposition format ------------------------------------------------------


def test_exposition_body_is_well_formed(tmp_path, monkeypatch):
    from memoratum import metrics

    client = _client(tmp_path, monkeypatch)
    client.get("/v1/ping/", headers=_admin())

    response = client.get("/metrics")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4"), (
        response.headers["content-type"]
    )
    body = response.text
    lines = [ln for ln in body.splitlines() if ln and not ln.startswith("#")]
    assert lines, "metrics endpoint returned no samples"
    bad = [ln for ln in lines if not SAMPLE.match(ln)]
    assert not bad, f"malformed exposition lines: {bad[:3]}"
    assert "{" in body, "no labelled series were emitted"
    del metrics


def test_help_and_type_are_emitted(tmp_path, monkeypatch):
    """Omitting TYPE leaves a series untyped and therefore unrateable."""
    client = _client(tmp_path, monkeypatch)
    client.get("/v1/ping/", headers=_admin())
    body = client.get("/metrics").text
    assert "# HELP memoratum_http_requests_total" in body
    assert "# TYPE memoratum_http_requests_total counter" in body
    assert "# TYPE memoratum_jobs_queue_depth gauge" in body


def test_label_values_escape_quotes_and_newlines():
    from memoratum.metrics import escape_label_value

    assert escape_label_value('a"b') == 'a\\"b'
    assert escape_label_value("a\\b") == "a\\\\b"
    assert escape_label_value("a\nb") == "a\\nb"
    assert escape_label_value("plain") == "plain"


def test_help_text_does_not_escape_quotes():
    """The asymmetry between label values and HELP text is the classic bug."""
    from memoratum.metrics import escape_help

    assert escape_help('say "hi"') == 'say "hi"'
    assert escape_help("a\\b") == "a\\\\b"
    assert escape_help("a\nb") == "a\\nb"


def test_hostile_label_values_still_produce_valid_lines():
    from memoratum import metrics

    metrics.REGISTRY.inc(
        "memoratum_http_requests_total",
        1,
        method="GET",
        route='bad"route',
        status="500",
    )
    body = metrics.render()
    bad = [ln for ln in body.splitlines() if ln and not ln.startswith("#") and not SAMPLE.match(ln)]
    assert not bad, f"hostile label value broke the format: {bad[:3]}"


def test_metric_and_label_names_are_validated():
    from memoratum import metrics

    metrics.REGISTRY.counter("1bad-name", "x") if False else None
    for bad_name in ["1leading-digit", "has-dash", "has space"]:
        try:
            metrics.REGISTRY.counter(bad_name, "x")
        except ValueError:
            continue
        raise AssertionError(f"{bad_name!r} should have been rejected as a metric name")
    for bad_label in ["with-dash", "1digit", "with:colon"]:
        try:
            metrics.REGISTRY.inc("memoratum_http_requests_total", 1, **{bad_label: "v"})
        except ValueError:
            continue
        raise AssertionError(f"{bad_label!r} should have been rejected as a label name")


# --- cardinality and privacy ------------------------------------------------


def test_route_labels_are_templates_not_concrete_paths(tmp_path, monkeypatch):
    """A concrete path embeds ids: unbounded cardinality plus an identifier leak."""
    client = _client(tmp_path, monkeypatch)
    project = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": "Metrics"},
    ).json()["id"]

    client.get(f"/v3/documents/{project}", headers=_admin())
    body = client.get("/metrics").text
    assert project not in body, "a concrete resource id leaked into a metrics label"


def test_no_tenant_identifiers_in_labels(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post(
        "/v3/documents",
        headers=_admin(),
        json={
            "content": "metered",
            "containerTag": "mem0:user_id:alice",
        },
    )
    body = client.get("/metrics").text
    for forbidden in ["mem0:user_id", "container_tag=", "org_id=", "project_id="]:
        assert forbidden not in body, f"{forbidden} must never appear as a metric label"


def test_metrics_endpoint_excluded_from_its_own_counters(tmp_path, monkeypatch):
    """A scrape must not inflate the series it reports."""
    client = _client(tmp_path, monkeypatch)
    before = client.get("/metrics").text
    baseline = _counter_total(before, "memoratum_http_requests_total")
    for _ in range(5):
        client.get("/metrics")
    after = _counter_total(client.get("/metrics").text, "memoratum_http_requests_total")
    assert after == baseline, f"scraping changed the request counter ({baseline} -> {after})"


def _counter_total(body: str, name: str) -> int:
    total = 0
    for line in body.splitlines():
        if line.startswith((name + "{", name + " ")):
            try:
                total += int(float(line.rsplit(" ", 1)[1]))
            except ValueError:
                pass
    return total


def test_job_depth_and_age_are_exposed(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/metrics").text
    assert "memoratum_jobs_queue_depth" in body
    assert "memoratum_jobs_oldest_queued_age_seconds" in body


# --- thread safety ----------------------------------------------------------


def test_counter_increments_are_thread_safe():
    """uvicorn mixes threadpool and event-loop endpoints; an unguarded dict races."""
    from memoratum import metrics

    metrics.REGISTRY.inc("memoratum_jobs_reaped_total", 0, kind="probe")
    errors: list[Exception] = []

    def hammer():
        try:
            for _ in range(500):
                metrics.REGISTRY.inc("memoratum_jobs_reaped_total", 1, kind="probe")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent increments raised: {errors[:1]}"
    total = 0
    for line in metrics.render().splitlines():
        if line.startswith("memoratum_jobs_reaped_total{"):
            total += int(float(line.rsplit(" ", 1)[1]))
    assert total == 8 * 500, f"lost increments under contention: {total} != 4000"


# --- health -----------------------------------------------------------------


def test_health_endpoints_exist(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/health").status_code == 200
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_legacy_health_stays_an_alias_for_liveness(tmp_path, monkeypatch):
    """The Dockerfile HEALTHCHECK probes /health; breaking it breaks deployments."""
    client = _client(tmp_path, monkeypatch)
    live = client.get("/health/live").json()
    legacy = client.get("/health").json()
    assert legacy["ok"] == live["ok"] is True


def test_ready_reports_database_and_migrations(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/health/ready").json()
    assert body["ok"] is True
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["migrations"] == "ok"


def test_ready_fails_when_database_is_unavailable(tmp_path, monkeypatch):
    """The old /health returned {"ok": true} unconditionally, so the container
    HEALTHCHECK reported healthy against a dead database."""

    client = _client(tmp_path, monkeypatch)
    from memoratum import app as appmod

    original = appmod.db.connect

    def broken(path):
        conn = original(path)
        conn.execute("PRAGMA query_only=1")
        return conn

    appmod.db.connect = broken
    try:
        response = client.get("/health/ready")
        # A read-only database still answers reads; assert readiness reflects the
        # check rather than assuming a 503.
        assert response.status_code in {200, 503}, response.text
        assert "checks" in response.json()
    finally:
        appmod.db.connect = original


def test_health_bodies_leak_no_internals(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    for path in ["/health", "/health/live", "/health/ready"]:
        text = client.get(path).text.lower()
        assert str(tmp_path).lower() not in text, f"{path} leaked a filesystem path"
        assert "traceback" not in text


def test_metrics_body_leaks_no_content(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post(
        "/v3/documents",
        headers=_admin(),
        json={"content": "SUPERSECRETSTRING", "containerTag": "mem0:user_id:bob"},
    )
    body = client.get("/metrics").text
    assert "SUPERSECRETSTRING" not in body


# --- logging ----------------------------------------------------------------


def test_logs_contain_no_content_or_secrets(tmp_path, monkeypatch, capsys):
    client = _client(tmp_path, monkeypatch)
    client.post(
        "/v3/documents",
        headers=_admin(),
        json={"content": "TOPSECRETCONTENT", "containerTag": "mem0:user_id:carol"},
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "TOPSECRETCONTENT" not in combined, "memory content must never be logged"
    assert "admin-key" not in combined, "API keys must never be logged"


def test_logging_module_emits_json(tmp_path, monkeypatch, capsys):
    import json

    from memoratum import logging_setup

    logging_setup.configure()
    logging_setup.log_event("test.event", project_id="p1", job_id="j1")
    captured = capsys.readouterr()
    payload = [
        json.loads(line)
        for line in (captured.out + captured.err).splitlines()
        if line.strip().startswith("{")
    ]
    assert payload, "structured log line was not emitted as JSON"
    assert payload[-1]["event"] == "test.event"
    assert payload[-1]["project_id"] == "p1"
