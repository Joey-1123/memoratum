"""End-to-end recovery of the P0 wedge, at the HTTP boundary.

The original defect: a worker killed mid-`purge_project` left the job `running`
forever. Retrying DELETE returned the same dead job id (looking like progress),
every write returned 409, and cancel returned 409. The project could never be
purged or written to again, with no recovery path.
"""

import json
import time

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    """Client with auth ENABLED, so admin gating is actually exercised.

    Without MEMORATUM_API_KEY the project treats the deployment as unauthenticated
    and every admin endpoint is open by design, which would make the authz
    assertions below vacuous.
    """
    monkeypatch.setenv("MEMORATUM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORATUM_API_KEY", "admin-key")
    from memoratum.app import create_app

    return TestClient(create_app())


def _admin() -> dict[str, str]:
    return {"Authorization": "Token admin-key"}


def _project(client: TestClient, name: str) -> str:
    response = client.post(
        "/api/v1/orgs/organizations/local-org/projects/",
        headers=_admin(),
        json={"name": name},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _write(client: TestClient, project_id: str, content: str):
    return client.post(
        "/v3/documents",
        headers=_admin(),
        json={
            "containerTag": "mem0:user_id:alice",
            "content": content,
            "project_id": project_id,
        },
    )


def _strand_purge_job(client: TestClient, project_id: str) -> str:
    """Simulate a worker that claimed the purge job and was then SIGKILLed.

    Leaves the row exactly as a kill does: status running, lease in the past,
    nothing cleaned up.
    """
    from memoratum import db
    from memoratum.config import Settings

    conn = db.connect(Settings.load().db_path)
    job_id = conn.execute(
        "SELECT id FROM jobs WHERE kind = 'purge_project'"
        " AND json_extract(payload, '$.project_id') = ?",
        (project_id,),
    ).fetchone()["id"]
    conn.execute(
        "UPDATE jobs SET status = 'running', worker = 'killed-worker',"
        " lease_expires_at = ?, heartbeat_at = ? WHERE id = ?",
        (time.time() - 1, time.time() - 120, job_id),
    )
    conn.commit()
    conn.close()
    return job_id


def _purge_done(client: TestClient, project_id: str) -> bool:
    response = client.get(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/", headers=_admin()
    )
    return response.status_code == 404


def test_wedged_project_is_recoverable_end_to_end(tmp_path, monkeypatch):
    """The exact scenario from the review, now with an exit."""
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Wedge")
    _write(client, project_id, "before the kill")
    job_id = _project_purge(client, project_id)
    _strand_purge_job(client, project_id)

    # 1. Retrying the delete must NOT hand back the dead job id as if it were progress.
    retry = client.delete(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/", headers=_admin()
    )
    assert retry.status_code == 409, retry.text
    assert "stranded" in retry.json()["error"]["message"].lower(), retry.text
    assert job_id not in retry.text

    # 2. Writes are still blocked -- the project is genuinely wedged at this point.
    assert _write(client, project_id, "during the wedge").status_code == 409

    # 3. The operator's route out: cancel the stranded job.
    cancelled = client.post(f"/v4/jobs/{job_id}/cancel", headers=_admin())
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    # 4. deleting flag cleared, so the project accepts writes again.
    assert _write(client, project_id, "after recovery").status_code == 201

    # 5. And the delete can now be retried for real.
    again = client.delete(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/", headers=_admin()
    )
    assert again.status_code == 202, again.text


def _project_purge(client: TestClient, project_id: str) -> str:
    response = client.delete(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/", headers=_admin()
    )
    assert response.status_code == 202, response.text
    return str(response.json()["job_id"])


def test_reaper_endpoint_recovers_a_stranded_job(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "Reap")
    job_id = _project_purge(client, project_id)
    _strand_purge_job(client, project_id)

    reaped = client.post("/v4/maintenance/reap-jobs", headers=_admin())
    assert reaped.status_code == 200, reaped.text
    body = reaped.json()
    assert [r["id"] for r in body["reaped"]] == [job_id]
    assert body["reaped"][0]["kind"] == "purge_project"

    job = client.get(f"/v4/jobs/{job_id}", headers=_admin())
    assert job.json()["status"] == "queued", "reaped job should be claimable again"


def test_reap_endpoint_requires_admin(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    key = client.post(
        "/v4/keys",
        headers=_admin(),
        json={"containerTag": "mem0:user_id:alice"},
    ).json()["key"]
    response = client.post("/v4/maintenance/reap-jobs", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code in {401, 403}, response.text


def test_a_live_purge_job_is_not_reported_as_stranded(tmp_path, monkeypatch):
    """A queued purge is real progress and must still report PENDING."""
    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "LiveQueue")
    first = client.delete(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/", headers=_admin()
    )
    second = client.delete(
        f"/api/v1/orgs/organizations/local-org/projects/{project_id}/", headers=_admin()
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]


def test_cancelling_a_live_running_job_is_refused(tmp_path, monkeypatch):
    """The other half of the contract: live work is never interrupted."""
    from memoratum import db
    from memoratum.config import Settings

    client = _client(tmp_path, monkeypatch)
    project_id = _project(client, "LiveLease")
    job_id = _project_purge(client, project_id)

    conn = db.connect(Settings.load().db_path)
    conn.execute(
        "UPDATE jobs SET status = 'running', worker = 'busy-worker',"
        " lease_expires_at = ?, heartbeat_at = ? WHERE id = ?",
        (time.time() + 300, time.time(), job_id),
    )
    conn.commit()
    conn.close()

    refused = client.post(f"/v4/jobs/{job_id}/cancel", headers=_admin())
    assert refused.status_code == 409, refused.text
    assert "actively progressing" in refused.json()["error"]["message"].lower(), refused.text


def test_job_payload_survives_the_lease_round_trip(tmp_path, monkeypatch):
    """Regression guard: the lease columns must not disturb payload handling."""
    from memoratum import db, jobs
    from memoratum.config import Settings

    client = _client(tmp_path, monkeypatch)
    _project(client, "Payload")
    conn = db.connect(Settings.load().db_path)
    try:
        job_id = jobs.enqueue(conn, kind="bulk_memories", payload={"operations": [{"index": 0}]})
        claimed = jobs.claim(conn, worker="w")
        assert claimed is not None
        assert jobs.get(conn, job_id)["payload"]["operations"] == [{"index": 0}]
        assert json.dumps(jobs.get(conn, job_id)["payload"])
    finally:
        conn.close()
