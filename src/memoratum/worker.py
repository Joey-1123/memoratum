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
from memoratum.dreaming import dream_document, dream_pending
from memoratum.embeddings import Embedder
from memoratum.llm import ChatModel, build_chat
from memoratum.vectorstore import VectorStore, build_vector_store

MAX_ATTEMPTS = 3

_stop = False


def _handle_stop(signum, frame) -> None:
    global _stop
    _stop = True


def build_llm(settings: Settings) -> ChatModel | None:
    if not settings.llm_model:
        return None
    return build_chat(
        settings.llm_provider,
        endpoint=settings.llm_endpoint,
        model=settings.llm_model,
        api_key=os.environ.get("MEMORATUM_LLM_KEY", ""),
    )


def run_once(
    conn: sqlite3.Connection,
    embedder: Embedder,
    llm: ChatModel | None,
    *,
    worker_id: str,
    vector_store: VectorStore | None = None,
) -> str | None:
    """Claim and run one job. Returns the job id, or None when the queue is empty."""
    job = jobs.claim(conn, worker=worker_id)
    if job is None:
        return None
    try:
        result = _dispatch(conn, embedder, llm, job, vector_store=vector_store)
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
    conn: sqlite3.Connection,
    embedder: Embedder,
    llm: ChatModel | None,
    job: dict[str, Any],
    *,
    vector_store: VectorStore | None = None,
) -> dict[str, Any]:
    kind = job["kind"]
    payload = job["payload"] or {}
    if kind == "ingest":
        doc = ingest.process_document(
            conn, embedder, payload["document_id"], vector_store=vector_store
        )
        if llm is not None and doc["status"] == "done":
            jobs.enqueue(
                conn,
                kind="dream",
                payload={
                    "document_id": doc["id"],
                    "mode": payload.get("dreaming", "dynamic"),
                    "container_tag": doc["container_tag"],
                    "org_id": doc.get("org_id"),
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
            org_id=payload.get("org_id"),
        )
        return {"calls": calls}
    if kind == "backfill":
        from memoratum.search import search

        search(
            conn,
            embedder,
            "warmup",
            container_tag=payload["container_tag"],
            org_id=payload.get("org_id"),
            limit=1,
            search_mode="memories",
            vector_store=vector_store,
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
    shared_vector_store = None
    vector_provider = settings.vector_store.strip().lower()
    if vector_provider not in {"", "sqlite", "local"}:
        shared_vector_store = build_vector_store(
            settings.vector_store,
            conn=None,
            endpoint=settings.vector_store_endpoint,
            path=settings.vector_store_path,
            api_key=os.environ.get("MEMORATUM_VECTOR_STORE_KEY", ""),
            collection_name=settings.vector_store_collection,
            dims=settings.vector_store_dims or settings.embeddings_dims,
        )
    print(f"memoratum-worker: {worker_id} polling {settings.db_path}")
    while not _stop:
        conn = db.connect(settings.db_path)
        try:
            vector_store = shared_vector_store or build_vector_store(
                settings.vector_store,
                conn=conn,
                endpoint=settings.vector_store_endpoint,
                path=settings.vector_store_path,
                api_key=os.environ.get("MEMORATUM_VECTOR_STORE_KEY", ""),
                collection_name=settings.vector_store_collection,
                dims=settings.vector_store_dims or settings.embeddings_dims,
            )
            if (
                run_once(
                    conn,
                    embedder,
                    llm,
                    worker_id=worker_id,
                    vector_store=vector_store,
                )
                is None
            ):
                time.sleep(interval)
        finally:
            conn.close()
    if shared_vector_store is not None:
        shared_vector_store.close()


if __name__ == "__main__":
    main()
