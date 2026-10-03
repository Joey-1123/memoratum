#!/usr/bin/env python
"""Operator recovery for jobs stranded by a dead worker.

A worker killed with SIGKILL (or OOM-killed) cannot release its claim. Under the
lease protocol the reaper normally returns that work to the queue on its own, but
an operator still needs a way to:

  * see which jobs are stranded and which projects they wedge, and
  * force recovery when the automatic reaper is not running or a lease has not
    yet lapsed.

This is a last-resort tool. It is deliberately separate from the HTTP API because
the API cannot distinguish "a live worker is slow" from "the worker is gone" any
better than the lease can, and because an operator recovering a wedged tenant
should be doing it deliberately.

Usage:
    uv run python scripts/recover_stuck_jobs.py --dry-run
    uv run python scripts/recover_stuck_jobs.py --force
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoratum import db, jobs
from memoratum.config import Settings


def stranded(conn) -> list[dict]:
    """Running jobs whose lease has already lapsed."""
    now = time.time()
    rows = conn.execute(
        "SELECT id, kind, worker, lease_expires_at, attempts, payload FROM jobs"
        " WHERE status = 'running' AND lease_expires_at < ? ORDER BY created_at",
        (now,),
    ).fetchall()
    return [dict(r) for r in rows]


def live_running(conn) -> list[dict]:
    """Running jobs whose lease is still valid. These are left strictly alone."""
    now = time.time()
    rows = conn.execute(
        "SELECT id, kind, worker, lease_expires_at, attempts FROM jobs"
        " WHERE status = 'running' AND lease_expires_at >= ? ORDER BY created_at",
        (now,),
    ).fetchall()
    return [dict(r) for r in rows]


def describe(row: dict) -> str:
    payload = json.loads(row.get("payload") or "{}")
    subject = payload.get("project_id") or payload.get("document_id") or ""
    age = time.time() - float(row.get("lease_expires_at") or 0.0)
    return (
        f"  {row['id']}  kind={row['kind']:<16} worker={row.get('worker') or '-':<16} "
        f"attempts={row['attempts']}  lease expired {age:.0f}s ago  {subject}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report only; make no changes (default)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="also requeue jobs whose lease has NOT yet lapsed. Refuses while any "
        "worker may still be running them.",
    )
    args = parser.parse_args()

    settings = Settings.load()
    conn = db.connect(settings.db_path)
    try:
        dead = stranded(conn)
        alive = live_running(conn)
        wedged = jobs.wedged_projects(conn)

        print(f"database: {settings.db_path}")
        print(f"lease length: {jobs.lease_seconds()}s")
        print()
        print(f"stranded jobs ({len(dead)}) -- worker is gone, safe to recover:")
        for row in dead:
            print(describe(row))
        if not dead:
            print("  (none)")
        print()
        print(f"live running jobs ({len(alive)}) -- NOT touched:")
        for row in alive:
            print(
                f"  {row['id']}  kind={row['kind']:<16} worker={row.get('worker') or '-':<16}"
                f"  lease valid for {float(row['lease_expires_at']) - time.time():.0f}s"
            )
        if not alive:
            print("  (none)")
        print()
        print(f"wedged projects (deleting=1 with no live purge job): {wedged or '(none)'}")
        print()

        if args.dry_run or (not dead and not args.force):
            if args.force and alive:
                print("--force refused: live jobs are still leased. Stop the workers first.")
                return 1
            print("dry run; nothing changed. Re-run with --force to recover.")
            return 0

        recovered = jobs.reap_stale(conn)
        forced = 0
        if args.force:
            for row in alive:
                conn.execute(
                    "UPDATE jobs SET lease_expires_at = 0 WHERE id = ? AND status = 'running'",
                    (row["id"],),
                )
                forced += 1
            if forced:
                conn.commit()
                recovered = recovered + jobs.reap_stale(conn)

        still_wedged = jobs.wedged_projects(conn)
        print(f"recovered {len(recovered)} job(s) ({forced} forced).")
        print(f"wedged projects remaining: {still_wedged or '(none)'}")
        if still_wedged:
            print()
            print("A project stays wedged if its purge job was already terminal.")
            print("Cancel that job to clear the deleting flag:")
            for project_id in still_wedged:
                print(f"  POST /v4/jobs/<purge_job_id>/cancel   # project {project_id}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
