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
from memoratum.vectorstore import VectorRecord, VectorStore, build_vector_store

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
    webhook_allow_private_targets: bool = False,
    webhook_timeout_seconds: float = 5.0,
    webhook_max_attempts: int = 5,
) -> str | None:
    """Claim and run one job. Returns the job id, or None when the queue is empty."""
    job = jobs.claim(conn, worker=worker_id)
    if job is None:
        return None
    try:
        result = _dispatch(
            conn,
            embedder,
            llm,
            job,
            vector_store=vector_store,
            webhook_allow_private_targets=webhook_allow_private_targets,
            webhook_timeout_seconds=webhook_timeout_seconds,
            webhook_max_attempts=webhook_max_attempts,
        )
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
    webhook_allow_private_targets: bool = False,
    webhook_timeout_seconds: float = 5.0,
    webhook_max_attempts: int = 5,
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
                    "project_id": doc.get("project_id"),
                },
            )
        if doc.get("project_id"):
            from memoratum.webhooks import record_event

            record_event(
                conn,
                project_id=str(doc["project_id"]),
                event_type=(
                    "ingest_job_completed" if doc["status"] == "done" else "ingest_job_failed"
                ),
                data={
                    "document_id": doc["id"],
                    "status": doc["status"],
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
            org_id=payload.get("org_id"),
            project_id=payload.get("project_id"),
        )
        return {"calls": calls}
    if kind == "webhook_delivery":
        from memoratum.webhooks import deliver_delivery

        return deliver_delivery(
            conn,
            payload["delivery_id"],
            allow_private=webhook_allow_private_targets,
            timeout_seconds=webhook_timeout_seconds,
            max_attempts=webhook_max_attempts,
        )
    if kind == "delete_all_memories":
        filters = payload.get("filters") or {}
        memories = db.list_all_memories(
            conn,
            org_id=payload.get("org_id"),
            project_id=payload.get("project_id"),
            show_expired=True,
        )
        selected = []
        for memory in memories:
            tag = memory["container_tag"]
            for key, value in filters.items():
                prefix = f"mem0:{key}:"
                if not tag.startswith(prefix):
                    break
                if value != "*" and tag != f"{prefix}{value}":
                    break
            else:
                selected.append(memory)
        deleted_ids: list[str] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            for memory in selected:
                result = db.soft_delete_memory(conn, memory["id"])
                if result["deleted"]:
                    deleted_ids.append(memory["id"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if deleted_ids and vector_store is not None:
            vector_store.delete(ids=deleted_ids)
        return {"deleted": len(deleted_ids), "memory_ids": deleted_ids}
    if kind == "bulk_memories":
        operations = payload.get("operations") or []
        previous = jobs.get(conn, str(job["id"])) or {}
        prior_items = {
            int(item["index"]): item
            for item in (previous.get("result") or {}).get("items", [])
            if isinstance(item, dict) and item.get("index") is not None
        }
        progress: list[dict[str, Any]] = []
        terminal = {"completed", "completed_with_vector_error", "not_found"}
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                progress.append(
                    {"index": index, "status": "failed", "error": "operation must be an object"}
                )
                continue
            existing = prior_items.get(index)
            if existing is not None and existing.get("status") in terminal:
                progress.append(existing)
                continue
            item_result: dict[str, Any] = {
                "index": index,
                "memory_id": operation.get("memory_id"),
                "action": operation.get("action"),
            }
            try:
                memory_id = str(operation["memory_id"])
                action = str(operation.get("action", "update"))
                if action not in {"update", "delete"}:
                    raise ValueError("action must be update or delete")
                memory = db.get_memory(conn, memory_id)
                if (
                    payload.get("container_tag")
                    and memory["container_tag"] != payload["container_tag"]
                ):
                    raise KeyError(memory_id)
                if payload.get("org_id") and memory.get("org_id") != payload["org_id"]:
                    raise KeyError(memory_id)
                if payload.get("project_id") and memory.get("project_id") != payload["project_id"]:
                    raise KeyError(memory_id)
                if action == "update":
                    updated = db.update_memory(
                        conn,
                        memory_id,
                        text=operation.get("text"),
                        metadata=operation.get("metadata"),
                        actor_key_hash=payload.get("actor_key_hash"),
                    )
                    conn.commit()
                    item_result["status"] = "completed"
                    if vector_store is not None:
                        try:
                            vector = embedder.embed([updated["text"]])[0]
                            vector_store.upsert(
                                [
                                    VectorRecord(
                                        id=str(updated["id"]),
                                        vector=vector,
                                        text=updated["text"],
                                        kind="memory",
                                        container_tag=updated["container_tag"],
                                        org_id=updated.get("org_id"),
                                        project_id=updated.get("project_id"),
                                        metadata=updated["metadata"],
                                        created_at=updated.get("created_at") or time.time(),
                                    )
                                ]
                            )
                        except Exception as exc:  # noqa: BLE001 - authoritative row is committed
                            jobs.enqueue(
                                conn,
                                kind="reindex_memory",
                                payload={
                                    "memory_id": str(updated["id"]),
                                    "container_tag": updated["container_tag"],
                                    "org_id": updated.get("org_id"),
                                    "project_id": updated.get("project_id"),
                                },
                            )
                            item_result["status"] = "completed_with_vector_error"
                            item_result["vector_error"] = type(exc).__name__
                else:
                    deleted = db.soft_delete_memory(
                        conn, memory_id, actor_key_hash=payload.get("actor_key_hash")
                    )
                    conn.commit()
                    item_result["status"] = "completed"
                    item_result["deleted"] = bool(deleted["deleted"])
                    if deleted["deleted_ids"] and vector_store is not None:
                        try:
                            vector_store.delete(ids=deleted["deleted_ids"])
                        except Exception as exc:  # noqa: BLE001 - authoritative row is committed
                            jobs.enqueue(
                                conn,
                                kind="reindex_memory",
                                payload={
                                    "memory_id": memory_id,
                                    "container_tag": memory["container_tag"],
                                    "org_id": memory.get("org_id"),
                                    "project_id": memory.get("project_id"),
                                },
                            )
                            item_result["status"] = "completed_with_vector_error"
                            item_result["vector_error"] = type(exc).__name__
            except KeyError:
                conn.rollback()
                item_result["status"] = "not_found"
            except Exception as exc:  # noqa: BLE001 - preserve per-item progress
                conn.rollback()
                item_result["status"] = "failed"
                item_result["error"] = type(exc).__name__
            progress.append(item_result)
            jobs.update_result(
                conn,
                str(job["id"]),
                {
                    "total": len(operations),
                    "completed": sum(
                        1 for item in progress if item.get("status", "").startswith("completed")
                    ),
                    "failed": sum(
                        1 for item in progress if item.get("status") in {"failed", "not_found"}
                    ),
                    "items": progress,
                },
            )
        return {
            "total": len(operations),
            "completed": sum(
                1 for item in progress if item.get("status", "").startswith("completed")
            ),
            "failed": sum(1 for item in progress if item.get("status") in {"failed", "not_found"}),
            "items": progress,
        }
    if kind == "purge_project":
        project_id = str(payload["project_id"])
        org_id = payload.get("org_id")
        project = conn.execute("SELECT org_id FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            return {"project_id": project_id, "status": "already_deleted"}
        if org_id is not None and project["org_id"] != org_id:
            raise ValueError("project does not belong to org_id")
        tags = [
            str(row["container_tag"])
            for row in conn.execute(
                "SELECT DISTINCT container_tag FROM documents WHERE project_id = ?"
                " UNION SELECT DISTINCT container_tag FROM facts WHERE project_id = ?"
                " UNION SELECT DISTINCT container_tag FROM memories WHERE project_id = ?",
                (project_id, project_id, project_id),
            ).fetchall()
        ]
        for tag in tags:
            if vector_store is not None:
                vector_store.delete(container_tag=tag, org_id=org_id, project_id=project_id)
        counts = db.purge_project(
            conn,
            project_id=project_id,
            org_id=org_id,
            preserve_job_id=job.get("id"),
        )
        return {"project_id": project_id, "status": "deleted", **counts}
    if kind == "reindex_memory":
        memory = db.get_memory(conn, payload["memory_id"])
        if memory["state"] == "deleted":
            if vector_store is not None:
                vector_store.delete(ids=[memory["id"]])
            return {"memory_id": memory["id"], "status": "deleted"}
        if vector_store is None:
            return {"memory_id": memory["id"], "status": "skipped"}

        vector = embedder.embed([memory["text"]])[0]
        vector_store.upsert(
            [
                VectorRecord(
                    id=memory["id"],
                    vector=vector,
                    text=memory["text"],
                    kind="memory",
                    container_tag=memory["container_tag"],
                    org_id=memory.get("org_id"),
                    project_id=memory.get("project_id"),
                    metadata=memory["metadata"],
                    created_at=memory["created_at"],
                )
            ]
        )
        return {"memory_id": memory["id"], "status": "indexed"}
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
                    webhook_allow_private_targets=settings.webhook_allow_private_targets,
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
