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

from memoratum import db, ingest
from memoratum import facts as fact_store
from memoratum.config import Settings
from memoratum.dreaming import ChatLLM, dream_pending
from memoratum.embeddings import ApiEmbedder, Embedder, HashEmbedder
from memoratum.facts import list_facts
from memoratum.search import search


class DocumentIn(BaseModel):
    content: str = Field(min_length=1, max_length=500_000)
    containerTag: str = "default"
    customId: str | None = None
    dreaming: Literal["dynamic", "instant"] = "dynamic"
    metadata: dict[str, Any] | None = None


class SearchIn(BaseModel):
    q: str = Field(min_length=1, max_length=2000)
    containerTag: str = "default"
    limit: int = Field(default=10, ge=1, le=100)
    threshold: float = Field(default=0.0, ge=0.0)
    searchMode: str = "hybrid"
    filters: dict[str, Any] | None = None
    rerank: bool = False


def _error(
    code: str, message: str, status: int, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}, headers=headers
    )


class KeyIn(BaseModel):
    containerTag: str | None = None


class FactIn(BaseModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    containerTag: str = "default"
    metadata: dict[str, Any] | None = None
    supersede: bool = True


class ImportIn(BaseModel):
    graph_dir: str = Field(min_length=1)
    tag: str = Field(min_length=1, max_length=128)


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

    def scope_of(authorization: str | None, conn: sqlite3.Connection) -> str | None | bool:
        """Admin key -> True; known key -> its scope (None = wildcard); else False."""
        if not authorization or not authorization.startswith("Bearer "):
            return False
        raw = authorization[len("Bearer ") :]
        if hmac.compare_digest(raw, settings.api_key):
            return True
        row = db.lookup_key(conn, raw)
        if row is None:
            return False
        return row["container_tag"]

    def credential_ok(authorization: str | None, conn: sqlite3.Connection) -> bool:
        return scope_of(authorization, conn) is not False

    def may_access(authorization: str | None, conn: sqlite3.Connection, container_tag: str) -> bool:
        scope = scope_of(authorization, conn)
        return scope is True or scope is None or scope == container_tag

    def authorize(authorization: str | None, conn: sqlite3.Connection, container_tag: str) -> None:
        if not settings.auth_enabled:
            return
        if not credential_ok(authorization, conn):
            raise HTTPException(status_code=401, detail="UNAUTHORIZED")
        if not may_access(authorization, conn, container_tag):
            raise HTTPException(status_code=403, detail="FORBIDDEN")

    def _authorized(authorization: str | None, conn: sqlite3.Connection) -> bool:
        return credential_ok(authorization, conn)

    def _may_access(
        authorization: str | None, conn: sqlite3.Connection, container_tag: str
    ) -> bool:
        return may_access(authorization, conn, container_tag)

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
        authorize(authorization, conn, doc.containerTag)
        created = db.create_document(
            conn,
            container_tag=doc.containerTag,
            content=doc.content,
            custom_id=doc.customId,
            metadata=doc.metadata,
        )
        ingest.process_one(conn, app.state.embedder)
        if app.state.llm is not None:
            dream_pending(conn, app.state.llm, mode=doc.dreaming, container_tag=doc.containerTag)
        return {"id": created["id"], "status": db.get_document(conn, created["id"])["status"]}

    @app.get("/v3/documents/{doc_id}")
    def get_document(doc_id: str, conn: DbConn, authorization: str | None = Header(default=None)):
        if settings.auth_enabled and not _authorized(authorization, conn):
            return _error("UNAUTHORIZED", "authentication required", 401)
        try:
            doc = db.get_document(conn, doc_id)
        except KeyError:
            return _error("NOT_FOUND", "document not found", 404)
        if settings.auth_enabled and not _may_access(authorization, conn, doc["container_tag"]):
            return _error("NOT_FOUND", "document not found", 404)
        return {"id": doc["id"], "containerTag": doc["container_tag"], "status": doc["status"]}

    @app.post("/v4/search")
    def run_search(query: SearchIn, conn: DbConn, authorization: str | None = Header(default=None)):
        authorize(authorization, conn, query.containerTag)
        started = time.time()
        hits = search(
            conn,
            app.state.embedder,
            query.q,
            container_tag=query.containerTag,
            limit=query.limit,
            threshold=query.threshold,
            search_mode=query.searchMode,
            filters=query.filters,
            rerank=query.rerank,
        )
        return {"results": hits, "timing": int((time.time() - started) * 1000), "total": len(hits)}

    @app.get("/v4/profile")
    def get_profile(
        conn: DbConn,
        containerTag: str = "default",
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization, conn, containerTag)
        facts = list_facts(conn, container_tag=containerTag)[:20]
        docs = conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE container_tag = ?", (containerTag,)
        ).fetchone()["n"]
        chunks = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.container_tag = ?",
            (containerTag,),
        ).fetchone()["n"]
        nfacts = conn.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE container_tag = ? AND valid_to IS NULL",
            (containerTag,),
        ).fetchone()["n"]
        return {
            "containerTag": containerTag,
            "facts": [f"{f['subject']} {f['predicate']} {f['object']}" for f in facts],
            "stats": {"documents": docs, "chunks": chunks, "facts": nfacts},
        }

    @app.post("/v4/keys", status_code=201)
    def issue_key(body: KeyIn, conn: DbConn, authorization: str | None = Header(default=None)):
        if not settings.auth_enabled:
            return {"key": db.create_api_key(conn, container_tag=body.containerTag)}
        if _is_admin(authorization, settings):
            return {"key": db.create_api_key(conn, container_tag=body.containerTag)}
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
            if not may_access(authorization, conn, fact["container_tag"]):
                return _error("NOT_FOUND", "fact not found", 404)
        fact_store.delete_fact(conn, fact_id)
        return {"deleted": fact_id}

    @app.delete("/v4/tags/{tag}")
    def purge(tag: str, conn: DbConn, authorization: str | None = Header(default=None)):
        if settings.auth_enabled:
            if not credential_ok(authorization, conn):
                return _error("UNAUTHORIZED", "authentication required", 401)
            if not may_access(authorization, conn, tag):
                return _error("NOT_FOUND", "tag not found", 404)
        return db.purge_tag(conn, tag)

    @app.post("/v4/facts", status_code=201)
    def create_fact(body: FactIn, conn: DbConn, authorization: str | None = Header(default=None)):
        authorize(authorization, conn, body.containerTag)
        fact = fact_store.add_fact(
            conn,
            container_tag=body.containerTag,
            subject=body.subject,
            predicate=body.predicate,
            object=body.object,
            document_id=None,
            metadata=body.metadata,
            supersede=body.supersede,
        )
        return {
            "id": fact["id"],
            "subject": fact["subject"],
            "predicate": fact["predicate"],
            "object": fact["object"],
            "containerTag": fact["container_tag"],
        }

    @app.get("/v4/facts")
    def list_facts_ep(
        conn: DbConn,
        containerTag: str,
        include_superseded: bool = False,
        limit: int = 100,
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization, conn, containerTag)
        facts = list_facts(conn, container_tag=containerTag, include_superseded=include_superseded)[
            : max(limit, 0)
        ]
        return {
            "facts": [
                {
                    "id": f["id"],
                    "subject": f["subject"],
                    "predicate": f["predicate"],
                    "object": f["object"],
                    "document_id": f["document_id"],
                    "metadata": f["metadata"],
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
        authorize(authorization, conn, f"graphify:{slug}")
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

        return sync_records(conn, graph, slug)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True}

    return app
