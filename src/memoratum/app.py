# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FastAPI app: compat routes, Bearer auth with scoped keys, error envelope."""

from __future__ import annotations

import hmac
import json
import math
import os
import sqlite3
import threading
import time
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from memoratum import db, jobs
from memoratum import facts as fact_store
from memoratum.config import Settings
from memoratum.dreaming import ChatLLM
from memoratum.embeddings import ApiEmbedder, Embedder, HashEmbedder
from memoratum.facts import list_facts
from memoratum.rerank import build_reranker
from memoratum.search import expand_query, merge_hits, pack_vector, search


class DocumentIn(BaseModel):
    content: str = Field(min_length=1, max_length=500_000)
    containerTag: str = "default"
    customId: str | None = None
    dreaming: Literal["dynamic", "instant"] = "dynamic"
    metadata: dict[str, Any] | None = None
    expires_at: float | None = None
    org_id: str | None = None


class SearchIn(BaseModel):
    q: str = Field(min_length=1, max_length=2000)
    containerTag: str = "default"
    limit: int = Field(default=10, ge=1, le=100)
    threshold: float = Field(default=0.0, ge=0.0)
    searchMode: str = "hybrid"
    filters: dict[str, Any] | None = None
    rerank: bool = False
    rewriteQuery: bool = False
    org_id: str | None = None


def _error(
    code: str, message: str, status: int, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}, headers=headers
    )


class KeyIn(BaseModel):
    containerTag: str | None = None
    org_id: str | None = None


class DocPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=500_000)
    metadata: dict[str, Any] | None = None


class FactIn(BaseModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    containerTag: str = "default"
    metadata: dict[str, Any] | None = None
    supersede: bool = True
    skipEmbedding: bool = False
    expires_at: float | None = None
    memory_type: str = "semantic"
    org_id: str | None = None


class ImportIn(BaseModel):
    graph_dir: str = Field(min_length=1)
    tag: str = Field(min_length=1, max_length=128)
    org_id: str | None = None


def _is_admin(authorization: str | None, settings: Settings) -> bool:
    return bool(
        settings.auth_enabled
        and authorization
        and authorization.startswith("Bearer ")
        and hmac.compare_digest(authorization[len("Bearer ") :], settings.api_key)
    )


def build_embedder(settings: Settings) -> Embedder:
    if (
        settings.embeddings_provider == "api"
        and settings.embeddings_endpoint
        and settings.embeddings_model
    ):
        return ApiEmbedder(
            endpoint=settings.embeddings_endpoint,
            model=settings.embeddings_model,
            api_key=os.environ.get("MEMORATUM_EMBEDDINGS_KEY", ""),
            dims=settings.embeddings_dims or 0,
        )
    return HashEmbedder()


# Serializes requests: each gets its own connection, but dependency setup and
# endpoint bodies run on different worker threads, so requests must not overlap
# on SQLite connections. Single-process ceiling; HA needs a real DB server.
_DB_LOCK = threading.Lock()


def get_conn(request: Request):
    with _DB_LOCK:
        conn = db.connect(request.app.state.settings.db_path)
        try:
            yield conn
        finally:
            conn.close()


DbConn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _ensure_boot_key(settings: Settings) -> None:
    """First boot with no auth configured: mint a wildcard admin key and print it once."""
    if settings.auth_enabled:
        return
    conn = db.connect(settings.db_path)
    try:
        existing = conn.execute("SELECT COUNT(*) AS n FROM api_keys").fetchone()["n"]
        if existing:
            return
        raw = db.create_api_key(conn, container_tag=None)
        print(f"memoratum: generated admin key (shown once, store it): {raw}")
    finally:
        conn.close()


def _is_loopback(ip: str) -> bool:
    return ip == "localhost" or ip.startswith("127.") or ip in ("::1", "::ffff:127.0.0.1")


def create_app(settings: Settings | None = None, *, rate_limit_per_minute: int | None = 120):
    settings = settings or Settings.load()
    os.makedirs(settings.data_dir, exist_ok=True)
    app = FastAPI(title="Memoratum")
    app.state.settings = settings
    if rate_limit_per_minute:
        buckets: dict[str, list[float]] = {}

        @app.middleware("http")
        async def _rate_limit(request: Request, call_next):
            if request.url.path == "/health" or request.url.path.startswith("/dashboard"):
                return await call_next(request)
            now = time.time()
            window = 60.0
            ip = request.client.host if request.client else "unknown"
            if _is_loopback(ip):
                return await call_next(request)
            hits = [t for t in buckets.get(ip, []) if now - t < window]
            if len(hits) >= rate_limit_per_minute:
                retry_after = max(1, math.ceil(hits[0] + window - now))
                return _error(
                    "RATE_LIMITED", "rate limit exceeded", 429, {"Retry-After": str(retry_after)}
                )
            hits.append(now)
            buckets[ip] = hits
            return await call_next(request)

    app.state.settings = settings
    app.state.embedder = build_embedder(settings)
    app.state.reranker = build_reranker(
        os.environ.get("MEMORATUM_RERANKER", "heuristic"),
        os.environ.get("MEMORATUM_RERANKER_MODEL", ""),
    )
    app.state.llm = (
        ChatLLM(
            endpoint=settings.llm_endpoint,
            model=settings.llm_model,
            api_key=os.environ.get("MEMORATUM_LLM_KEY", ""),
        )
        if settings.llm_endpoint and settings.llm_model
        else None
    )
    _ensure_boot_key(settings)
    dashboard_index = os.path.join(settings.dashboard_dir, "index.html")
    if os.path.exists(dashboard_index):
        from starlette.staticfiles import StaticFiles

        app.mount(
            "/dashboard", StaticFiles(directory=settings.dashboard_dir, html=True), name="dashboard"
        )

    def scope_of(authorization: str | None, conn: sqlite3.Connection) -> dict | bool:
        """Admin key -> True; known key -> its scope (container_tag, org_id); else False."""
        if not authorization or not authorization.startswith("Bearer "):
            return False
        raw = authorization[len("Bearer ") :]
        if hmac.compare_digest(raw, settings.api_key):
            return True
        row = db.lookup_key(conn, raw)
        if row is None:
            return False
        return {"container_tag": row["container_tag"], "org_id": row["org_id"]}

    def credential_ok(authorization: str | None, conn: sqlite3.Connection) -> bool:
        return scope_of(authorization, conn) is not False

    def may_access(
        authorization: str | None,
        conn: sqlite3.Connection,
        container_tag: str,
        org_id: str | None = None,
    ) -> bool:
        scope = scope_of(authorization, conn)
        if scope is True or scope is None:
            return True
        if scope is False:
            return False
        scope_tag = scope.get("container_tag")
        if scope_tag is not None and scope_tag != container_tag:
            return False
        scope_org = scope.get("org_id")
        return scope_org is None or org_id == scope_org

    def authorize(
        authorization: str | None,
        conn: sqlite3.Connection,
        container_tag: str,
        org_id: str | None = None,
    ) -> str | None:
        if not settings.auth_enabled:
            return org_id
        if not credential_ok(authorization, conn):
            raise HTTPException(status_code=401, detail="UNAUTHORIZED")
        effective_org = org_id
        scope = scope_of(authorization, conn)
        if isinstance(scope, dict) and scope.get("org_id") is not None and org_id is None:
            effective_org = scope["org_id"]
        if not may_access(authorization, conn, container_tag, effective_org):
            raise HTTPException(status_code=403, detail="FORBIDDEN")
        return effective_org

    def _authorized(authorization: str | None, conn: sqlite3.Connection) -> bool:
        return credential_ok(authorization, conn)

    def _may_access(
        authorization: str | None,
        conn: sqlite3.Connection,
        container_tag: str,
        org_id: str | None = None,
    ) -> bool:
        return may_access(authorization, conn, container_tag, org_id)

    def _job_tag(conn: sqlite3.Connection, job: dict[str, Any]) -> str | None:
        payload = job.get("payload") or {}
        if job.get("kind") == "ingest" and payload.get("document_id"):
            try:
                return db.get_document(conn, payload["document_id"])["container_tag"]
            except KeyError:
                return None
        tag = payload.get("container_tag")
        return tag if isinstance(tag, str) else None

    def _job_org(conn: sqlite3.Connection, job: dict[str, Any]) -> str | None:
        payload = job.get("payload") or {}
        if payload.get("document_id"):
            try:
                return db.get_document(conn, payload["document_id"]).get("org_id")
            except KeyError:
                return None
        org_id = payload.get("org_id")
        return org_id if isinstance(org_id, str) else None

    @app.exception_handler(HTTPException)
    async def _http_errors(_req: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "ERROR"
        code = (
            detail if detail.isupper() else ("UNAUTHORIZED" if exc.status_code == 401 else "ERROR")
        )
        return _error(code, detail, exc.status_code)

    @app.post("/v3/documents", status_code=201)
    def add_document(
        doc: DocumentIn, conn: DbConn, authorization: str | None = Header(default=None)
    ):
        org_id = authorize(authorization, conn, doc.containerTag, doc.org_id)
        created = db.create_document(
            conn,
            container_tag=doc.containerTag,
            content=doc.content,
            custom_id=doc.customId,
            metadata=doc.metadata,
            expires_at=doc.expires_at,
            org_id=org_id,
        )
        job_id = jobs.enqueue(
            conn,
            kind="ingest",
            payload={
                "document_id": created["id"],
                "dreaming": doc.dreaming,
                "container_tag": doc.containerTag,
                "org_id": org_id,
            },
        )
        return {"id": created["id"], "status": "queued", "job_id": job_id}

    @app.get("/v3/documents/{doc_id}")
    def get_document(doc_id: str, conn: DbConn, authorization: str | None = Header(default=None)):
        if settings.auth_enabled and not _authorized(authorization, conn):
            return _error("UNAUTHORIZED", "authentication required", 401)
        try:
            doc = db.get_document(conn, doc_id)
        except KeyError:
            return _error("NOT_FOUND", "document not found", 404)
        if settings.auth_enabled and not _may_access(
            authorization, conn, doc["container_tag"], doc.get("org_id")
        ):
            return _error("NOT_FOUND", "document not found", 404)
        return {"id": doc["id"], "containerTag": doc["container_tag"], "status": doc["status"]}

    @app.patch("/v3/documents/{doc_id}")
    def update_document(
        doc_id: str, patch: DocPatch, conn: DbConn, authorization: str | None = Header(default=None)
    ):
        try:
            doc = db.get_document(conn, doc_id)
        except KeyError:
            return _error("NOT_FOUND", "document not found", 404)
        if settings.auth_enabled:
            if not _authorized(authorization, conn):
                return _error("UNAUTHORIZED", "authentication required", 401)
            if not _may_access(authorization, conn, doc["container_tag"], doc.get("org_id")):
                return _error("NOT_FOUND", "document not found", 404)
        updated = db.update_document(conn, doc_id, content=patch.content, metadata=patch.metadata)
        job_id = jobs.enqueue(
            conn,
            kind="ingest",
            payload={
                "document_id": updated["id"],
                "container_tag": updated["container_tag"],
                "org_id": updated.get("org_id"),
            },
        )
        return {"id": updated["id"], "status": "queued", "job_id": job_id}

    @app.get("/v3/documents")
    def list_documents(
        conn: DbConn,
        containerTag: str = "default",
        org_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        authorization: str | None = Header(default=None),
    ):
        org_id = authorize(authorization, conn, containerTag, org_id)
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        where = "container_tag = ?"
        params: tuple[Any, ...] = (containerTag,)
        if org_id is not None:
            where += " AND org_id = ?"
            params += (org_id,)
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM documents WHERE {where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT id, container_tag, custom_id, status, created_at FROM documents WHERE {where} ORDER BY created_at LIMIT ? OFFSET ?",
            params + (limit, offset),
        ).fetchall()
        return {
            "documents": [
                {
                    "id": r["id"],
                    "containerTag": r["container_tag"],
                    "customId": r["custom_id"],
                    "status": r["status"],
                }
                for r in rows
            ],
            "total": total,
        }

    @app.post("/v4/search")
    def run_search(query: SearchIn, conn: DbConn, authorization: str | None = Header(default=None)):
        org_id = authorize(authorization, conn, query.containerTag, query.org_id)
        started = time.time()
        queries = (
            expand_query(app.state.llm, query.q)
            if query.rewriteQuery and app.state.llm is not None
            else [query.q]
        )
        batches = [
            search(
                conn,
                app.state.embedder,
                q,
                container_tag=query.containerTag,
                org_id=org_id,
                limit=query.limit,
                threshold=query.threshold,
                search_mode=query.searchMode,
                filters=query.filters,
                rerank=query.rerank,
                reranker=app.state.reranker,
            )
            for q in queries
        ]
        hits = merge_hits(batches, limit=query.limit)
        return {"results": hits, "timing": int((time.time() - started) * 1000), "total": len(hits)}

    @app.get("/v4/profile")
    def get_profile(
        conn: DbConn,
        containerTag: str = "default",
        org_id: str | None = None,
        authorization: str | None = Header(default=None),
    ):
        org_id = authorize(authorization, conn, containerTag, org_id)
        facts = list_facts(conn, container_tag=containerTag, org_id=org_id)[:20]
        doc_where = "container_tag = ?"
        doc_params: tuple[Any, ...] = (containerTag,)
        chunk_where = "d.container_tag = ?"
        if org_id is not None:
            doc_where += " AND org_id = ?"
            chunk_where += " AND d.org_id = ?"
            doc_params += (org_id,)
        docs = conn.execute(
            f"SELECT COUNT(*) AS n FROM documents WHERE {doc_where}", doc_params
        ).fetchone()["n"]
        chunks = conn.execute(
            f"SELECT COUNT(*) AS n FROM chunks c JOIN documents d ON d.id = c.document_id"
            f" WHERE {chunk_where}",
            doc_params,
        ).fetchone()["n"]
        fact_where = "container_tag = ? AND valid_to IS NULL"
        fact_params: tuple[Any, ...] = (containerTag,)
        if org_id is not None:
            fact_where += " AND org_id = ?"
            fact_params += (org_id,)
        nfacts = conn.execute(
            f"SELECT COUNT(*) AS n FROM facts WHERE {fact_where}", fact_params
        ).fetchone()["n"]
        return {
            "containerTag": containerTag,
            "facts": [f"{f['subject']} {f['predicate']} {f['object']}" for f in facts],
            "stats": {"documents": docs, "chunks": chunks, "facts": nfacts},
        }

    @app.post("/v4/keys", status_code=201)
    def issue_key(body: KeyIn, conn: DbConn, authorization: str | None = Header(default=None)):
        if not settings.auth_enabled:
            return {
                "key": db.create_api_key(conn, container_tag=body.containerTag, org_id=body.org_id)
            }
        if _is_admin(authorization, settings):
            return {
                "key": db.create_api_key(conn, container_tag=body.containerTag, org_id=body.org_id)
            }
        if (
            authorization
            and authorization.startswith("Bearer ")
            and db.lookup_key(conn, authorization[len("Bearer ") :]) is not None
        ):
            return _error("FORBIDDEN", "admin key required", 403)
        return _error("UNAUTHORIZED", "authentication required", 401)

    @app.post("/v4/keys/revoke")
    def revoke(
        body: dict[str, Any], conn: DbConn, authorization: str | None = Header(default=None)
    ):
        if settings.auth_enabled and not _is_admin(authorization, settings):
            return _error("FORBIDDEN", "admin key required", 403)
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key:
            return _error("VALIDATION_ERROR", "key is required", 422)
        return {"revoked": db.revoke_key(conn, key)}

    @app.delete("/v4/memories/{fact_id}")
    def forget_fact(fact_id: str, conn: DbConn, authorization: str | None = Header(default=None)):
        try:
            fact = fact_store.get_fact(conn, fact_id)
        except KeyError:
            return _error("NOT_FOUND", "fact not found", 404)
        if settings.auth_enabled:
            if not credential_ok(authorization, conn):
                return _error("UNAUTHORIZED", "authentication required", 401)
            if not may_access(authorization, conn, fact["container_tag"], fact.get("org_id")):
                return _error("NOT_FOUND", "fact not found", 404)
        fact_store.delete_fact(conn, fact_id)
        return {"deleted": fact_id}

    @app.delete("/v4/tags/{tag}")
    def purge(tag: str, conn: DbConn, authorization: str | None = Header(default=None)):
        if settings.auth_enabled and not credential_ok(authorization, conn):
            return _error("UNAUTHORIZED", "authentication required", 401)
        try:
            org_id = authorize(authorization, conn, tag)
        except HTTPException as exc:
            if exc.status_code == 403:
                return _error("NOT_FOUND", "tag not found", 404)
            raise
        return db.purge_tag(conn, tag, org_id=org_id)

    @app.post("/v4/facts", status_code=201)
    def create_fact(body: FactIn, conn: DbConn, authorization: str | None = Header(default=None)):
        org_id = authorize(authorization, conn, body.containerTag, body.org_id)
        fact = fact_store.add_fact(
            conn,
            container_tag=body.containerTag,
            subject=body.subject,
            predicate=body.predicate,
            object=body.object,
            document_id=None,
            metadata=body.metadata,
            supersede=body.supersede,
            expires_at=body.expires_at,
            memory_type=body.memory_type,
            org_id=org_id,
        )
        try:
            if not body.skipEmbedding:
                text = f"{fact['subject']} {fact['predicate']} {fact['object']}"
                vec = app.state.embedder.embed([text])[0]
                conn.execute(
                    "UPDATE facts SET embedding = ? WHERE id = ?", (pack_vector(vec), fact["id"])
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001 — fact exists; vector backfills on search
            print(f"memoratum: inline fact embedding skipped: {exc}")
        return {
            "id": fact["id"],
            "subject": fact["subject"],
            "predicate": fact["predicate"],
            "object": fact["object"],
            "containerTag": fact["container_tag"],
            "memory_type": fact.get("memory_type", "semantic"),
        }

    @app.get("/v4/facts")
    def list_facts_ep(
        conn: DbConn,
        containerTag: str,
        include_superseded: bool = False,
        limit: int = 100,
        memory_type: str | None = None,
        org_id: str | None = None,
        authorization: str | None = Header(default=None),
    ):
        org_id = authorize(authorization, conn, containerTag, org_id)
        facts = list_facts(
            conn,
            container_tag=containerTag,
            include_superseded=include_superseded,
            memory_type=memory_type,
            org_id=org_id,
        )[: max(limit, 0)]
        return {
            "facts": [
                {
                    "id": f["id"],
                    "subject": f["subject"],
                    "predicate": f["predicate"],
                    "object": f["object"],
                    "document_id": f["document_id"],
                    "metadata": f["metadata"],
                    "memory_type": f.get("memory_type", "semantic"),
                    "valid_from": f["valid_from"],
                    "valid_to": f["valid_to"],
                    "superseded_by": f["superseded_by"],
                }
                for f in facts
            ],
            "total": len(facts),
        }

    @app.post("/v4/import")
    def import_graph(
        body: ImportIn, conn: DbConn, authorization: str | None = Header(default=None)
    ):
        # Server-local path by design (single-host tool). Any authenticated caller
        # may import, but only into tags they can write.
        slug = body.tag.removeprefix("graphify:")
        org_id = authorize(authorization, conn, f"graphify:{slug}", body.org_id)
        graph_path = os.path.join(os.path.abspath(body.graph_dir), "graph.json")
        if not os.path.isfile(graph_path):
            return _error("VALIDATION_ERROR", "graph_dir must contain graph.json", 422)
        try:
            with open(graph_path) as f:
                graph = json.load(f)
        except (ValueError, OSError) as e:
            return _error("VALIDATION_ERROR", f"unreadable graph.json: {e}", 422)
        if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
            return _error("VALIDATION_ERROR", "graph.json must have nodes[]", 422)
        report_path = os.path.join(os.path.dirname(graph_path), "GRAPH_REPORT.md")
        if os.path.isfile(report_path):
            try:
                with open(report_path) as f:
                    graph["report"] = f.read()
            except OSError:
                pass
        from memoratum.bridge import sync_records

        return sync_records(conn, graph, slug, org_id=org_id)

    @app.get("/v4/jobs/{job_id}")
    def get_job(job_id: str, conn: DbConn, authorization: str | None = Header(default=None)):
        job = jobs.get(conn, job_id)
        if job is None:
            return _error("NOT_FOUND", "job not found", 404)
        if settings.auth_enabled:
            if not credential_ok(authorization, conn):
                return _error("UNAUTHORIZED", "authentication required", 401)
            tag = _job_tag(conn, job)
            org_id = _job_org(conn, job)
            if tag is None or not may_access(authorization, conn, tag, org_id):
                return _error("NOT_FOUND", "job not found", 404)
        return {
            "id": job["id"],
            "kind": job["kind"],
            "status": job["status"],
            "attempts": job["attempts"],
            "result": job["result"],
            "error": job["error"],
        }

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True}

    return app
