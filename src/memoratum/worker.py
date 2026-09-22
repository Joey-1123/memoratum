# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Background worker: claims jobs and runs ingest/dream/backfill. Separate process."""

from __future__ import annotations

import os
import signal
import sqlite3
import time
from typing import Any

from memoratum import db, ingest, jobs
from memoratum.config import Settings
from memoratum.dreaming import ChatLLM, dream_document, dream_pending
from memoratum.embeddings import Embedder

MAX_ATTEMPTS = 3

_stop = False


def _handle_stop(signum, frame) -> None:
    global _stop
    _stop = True


def build_llm(settings: Settings) -> ChatLLM | None:
    if settings.llm_endpoint and settings.llm_model:
        return ChatLLM(
            endpoint=settings.llm_endpoint,
            model=settings.llm_model,
            api_key=os.environ.get("MEMORATUM_LLM_KEY", ""),
        )
    return None


def run_once(
    conn: sqlite3.Connection, embedder: Embedder, llm: ChatLLM | None, *, worker_id: str
) -> str | None:
    """Claim and run one job. Returns the job id, or None when the queue is empty."""
    job = jobs.claim(conn, worker=worker_id)
    if job is None:
        return None
    try:
        result = _dispatch(conn, embedder, llm, job)
        jobs.complete(conn, job["id"], result=result)
    except ValueError as exc:
        jobs.fail(conn, job["id"], error=f"permanent: {exc}")
    except Exception as exc:  # noqa: BLE001 — record, retry or poison-pill
        if job["attempts"] >= MAX_ATTEMPTS:
            jobs.fail(conn, job["id"], error=str(exc))
        else:
            jobs.requeue(conn, job["id"])
    return job["id"]


def _dispatch(
    conn: sqlite3.Connection, embedder: Embedder, llm: ChatLLM | None, job: dict[str, Any]
) -> dict[str, Any]:
    kind = job["kind"]
    payload = job["payload"] or {}
    if kind == "ingest":
        doc = ingest.process_document(conn, embedder, payload["document_id"])
        if llm is not None and doc["status"] == "done":
            jobs.enqueue(
                conn,
                kind="dream",
                payload={
                    "document_id": doc["id"],
                    "mode": payload.get("dreaming", "dynamic"),
                    "container_tag": doc["container_tag"],
                },
            )
        return {"document_id": doc["id"], "status": doc["status"]}
    if kind == "dream":
        if llm is None:
            return {"skipped": "no LLM configured"}
        if payload.get("document_id"):
            dream_document(conn, llm, payload["document_id"])
            return {"document_id": payload["document_id"]}
        calls = dream_pending(
            conn,
            llm,
            mode=payload.get("mode", "dynamic"),
            container_tag=payload.get("container_tag"),
        )
        return {"calls": calls}
    if kind == "backfill":
        from memoratum.search import search

        search(
            conn,
            embedder,
            "warmup",
            container_tag=payload["container_tag"],
            limit=1,
            search_mode="memories",
        )
        return {"container_tag": payload["container_tag"]}
    raise ValueError(f"unknown job kind: {kind}")


def main() -> None:
    settings = Settings.load()
    os.makedirs(settings.data_dir, exist_ok=True)
    from memoratum.app import build_embedder

    embedder = build_embedder(settings)
    llm = build_llm(settings)
    interval = float(os.environ.get("MEMORATUM_WORKER_INTERVAL", "2") or 2)
    worker_id = f"worker-{os.getpid()}"
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    print(f"memoratum-worker: {worker_id} polling {settings.db_path}")
    while not _stop:
        conn = db.connect(settings.db_path)
        try:
            if run_once(conn, embedder, llm, worker_id=worker_id) is None:
                time.sleep(interval)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
