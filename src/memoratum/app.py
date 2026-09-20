# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FastAPI app: compat routes, Bearer auth with scoped keys, error envelope."""

from __future__ import annotations

import hmac
import os
import sqlite3
import time
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from memoratum import db, ingest
from memoratum.config import Settings
from memoratum.dreaming import ChatLLM, dream_pending
from memoratum.embeddings import ApiEmbedder, Embedder, HashEmbedder
from memoratum.facts import list_facts
from memoratum.search import search


class DocumentIn(BaseModel):
    content: str = Field(min_length=1)
    containerTag: str = "default"
    customId: str | None = None
    dreaming: Literal["dynamic", "instant"] = "dynamic"
    metadata: dict[str, Any] | None = None


class SearchIn(BaseModel):
    q: str = Field(min_length=1)
    containerTag: str = "default"
    limit: int = Field(default=10, ge=1, le=100)
    threshold: float = Field(default=0.0, ge=0.0)
    searchMode: str = "hybrid"
    filters: dict[str, Any] | None = None
    rerank: bool = False


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


class KeyIn(BaseModel):
    containerTag: str | None = None


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


def get_conn(request: Request):
    conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


DbConn = Annotated[sqlite3.Connection, Depends(get_conn)]


def create_app(settings: Settings | None = None):
    settings = settings or Settings.load()
    os.makedirs(settings.data_dir, exist_ok=True)
    app = FastAPI(title="Memoratum")
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

    def authorize(authorization: str | None, conn: sqlite3.Connection, container_tag: str) -> None:
        if not settings.auth_enabled:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="UNAUTHORIZED")
        raw = authorization[len("Bearer ") :]
        if hmac.compare_digest(raw, settings.api_key):
            return
        scope = db.resolve_key(conn, raw)
        if scope is None or scope != container_tag:
            raise HTTPException(status_code=401 if scope is None else 403, detail="FORBIDDEN")

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
            dream_pending(conn, app.state.llm, mode=doc.dreaming)
        return {"id": created["id"], "status": db.get_document(conn, created["id"])["status"]}

    @app.get("/v3/documents/{doc_id}")
    def get_document(doc_id: str, conn: DbConn, authorization: str | None = Header(default=None)):
        try:
            doc = db.get_document(conn, doc_id)
        except KeyError:
            return _error("NOT_FOUND", "document not found", 404)
        authorize(authorization, conn, doc["container_tag"])
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
        return {
            "containerTag": containerTag,
            "facts": [f"{f['subject']} {f['predicate']} {f['object']}" for f in facts],
            "stats": {"documents": docs, "chunks": chunks, "facts": len(facts)},
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
            and db.resolve_key(conn, authorization[len("Bearer ") :]) is not None
        ):
            return _error("FORBIDDEN", "admin key required", 403)
        return _error("UNAUTHORIZED", "authentication required", 401)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True}

    return app
