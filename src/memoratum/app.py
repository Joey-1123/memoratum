# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FastAPI app: compat routes, Bearer auth with scoped keys, error envelope."""

from __future__ import annotations

import hmac
import json
import math
import os
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from memoratum import db, jobs, webhooks
from memoratum import facts as fact_store
from memoratum.auth import IdentityProvider, token_fingerprint
from memoratum.config import Settings
from memoratum.embeddings import Embedder
from memoratum.embeddings import build_embedder as build_provider_embedder
from memoratum.facts import count_facts, list_facts
from memoratum.llm import build_chat
from memoratum.rerank import build_reranker
from memoratum.search import expand_query, merge_hits, pack_vector, search, search_memory_records
from memoratum.vectorstore import build_vector_store as build_provider_vector_store


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
    project_id: str | None = None


class DocPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=500_000)
    metadata: dict[str, Any] | None = None


class ShareIn(BaseModel):
    document_id: str = Field(min_length=1, max_length=128)
    expires_in: int = Field(default=7 * 24 * 60 * 60, ge=60, le=365 * 24 * 60 * 60)


class Mem0Message(BaseModel):
    role: Literal["user", "assistant", "system"] = "user"
    content: str = Field(min_length=1, max_length=100_000)


class Mem0AddIn(BaseModel):
    messages: list[Mem0Message] = Field(min_length=1, max_length=500)
    user_id: str | None = None
    agent_id: str | None = None
    app_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] | None = None
    infer: bool = True
    containerTag: str | None = None


class Mem0SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    filters: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=10, ge=1, le=100)
    threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    rerank: bool = False
    show_expired: bool = False


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
    normalized = _compat_auth_header(authorization)
    return bool(
        settings.auth_enabled
        and normalized
        and normalized.startswith("Bearer ")
        and hmac.compare_digest(normalized[len("Bearer ") :], settings.api_key)
    )


def _compat_auth_header(authorization: str | None) -> str | None:
    if authorization and authorization.startswith("Token "):
        return "Bearer " + authorization[len("Token ") :]
    return authorization


def _mem0_entity_tag(values: dict[str, Any]) -> str | None:
    for key in ("user_id", "agent_id", "app_id", "run_id"):
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return f"mem0:{key}:{value}"
    for key in ("AND", "OR"):
        nested = values.get(key)
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    found = _mem0_entity_tag(item)
                    if found:
                        return found
    return None


def build_embedder(settings: Settings) -> Embedder:
    return build_provider_embedder(
        settings.embeddings_provider,
        endpoint=settings.embeddings_endpoint,
        model=settings.embeddings_model,
        api_key=os.environ.get("MEMORATUM_EMBEDDINGS_KEY", ""),
        dims=settings.embeddings_dims,
    )


def build_vector_store(settings: Settings, conn: sqlite3.Connection | None = None):
    return build_provider_vector_store(
        settings.vector_store,
        conn=conn,
        endpoint=settings.vector_store_endpoint,
        path=settings.vector_store_path,
        api_key=os.environ.get("MEMORATUM_VECTOR_STORE_KEY", ""),
        collection_name=settings.vector_store_collection,
        dims=settings.vector_store_dims or settings.embeddings_dims,
    )


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


def create_app(
    settings: Settings | None = None,
    *,
    rate_limit_per_minute: int | None = 120,
    identity_provider: IdentityProvider | None = None,
):
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
        endpoint=os.environ.get("MEMORATUM_RERANKER_ENDPOINT", ""),
        api_key=os.environ.get("MEMORATUM_RERANKER_KEY", ""),
    )
    app.state.llm = (
        build_chat(
            settings.llm_provider,
            endpoint=settings.llm_endpoint,
            model=settings.llm_model,
            api_key=os.environ.get("MEMORATUM_LLM_KEY", ""),
        )
        if settings.llm_model
        else None
    )
    vector_provider = settings.vector_store.strip().lower()
    app.state.vector_store = (
        None if vector_provider in {"", "sqlite", "local"} else build_vector_store(settings)
    )

    def vector_store_for(conn: sqlite3.Connection):
        if app.state.vector_store is not None:
            return app.state.vector_store
        if vector_provider in {"", "sqlite", "local"}:
            return build_vector_store(settings, conn)
        return None

    _ensure_boot_key(settings)
    dashboard_index = os.path.join(settings.dashboard_dir, "index.html")
    if os.path.exists(dashboard_index):
        from starlette.staticfiles import StaticFiles

        app.mount(
            "/dashboard", StaticFiles(directory=settings.dashboard_dir, html=True), name="dashboard"
        )

    def scope_of(authorization: str | None, conn: sqlite3.Connection) -> dict | bool:
        """Resolve admin, local scoped key, or injected external identity."""
        normalized = _compat_auth_header(authorization)
        if not normalized or not normalized.startswith("Bearer "):
            return False
        raw = normalized[len("Bearer ") :]
        if hmac.compare_digest(raw, settings.api_key):
            return True
        if identity_provider is not None:
            try:
                identity = identity_provider.verify(raw)
            except Exception:  # noqa: BLE001 — invalid external identity is unauthorized
                identity = None
            if identity is not None:
                return {
                    "key_hash": token_fingerprint(raw),
                    "container_tag": identity.container_tag,
                    "org_id": identity.org_id,
                    "project_id": None,
                    "identity_subject": identity.subject,
                }
        row = db.lookup_key(conn, raw)
        if row is None:
            return False
        return {
            "key_hash": row["key_hash"],
            "container_tag": row["container_tag"],
            "org_id": row["org_id"],
            "project_id": row["project_id"],
        }

    def actor_from_scope(authorization: str | None, scope: dict | bool) -> tuple[str, str | None]:
        normalized = _compat_auth_header(authorization)
        if not normalized or not normalized.startswith("Bearer "):
            return "anonymous", None
        raw = normalized[len("Bearer ") :]
        if scope is True:
            return "admin", db.hash_key(raw)
        if isinstance(scope, dict):
            actor_kind = "oidc" if scope.get("identity_subject") else "key"
            return actor_kind, scope.get("key_hash")
        return "anonymous", None

    def actor(authorization: str | None, conn: sqlite3.Connection) -> tuple[str, str | None]:
        return actor_from_scope(authorization, scope_of(authorization, conn))

    def _idempotency_scope(authorization: str | None, conn: sqlite3.Connection) -> str:
        kind, key_hash = actor(authorization, conn)
        return f"{kind}:{key_hash or 'anonymous'}"

    def _begin_idempotency(
        authorization: str | None,
        conn: sqlite3.Connection,
        key: str | None,
        *,
        operation: str,
        payload: Any,
    ) -> tuple[db.IdempotencyClaim | None, JSONResponse | None]:
        if key is None:
            return None, None
        if not key.strip() or len(key) > 256:
            return None, _error("VALIDATION_ERROR", "Idempotency-Key must be 1-256 characters", 422)
        request_hash = db.canonical_request_hash({"operation": operation, "payload": payload})
        claim = db.claim_idempotency(
            conn,
            scope=_idempotency_scope(authorization, conn),
            key=key,
            request_hash=request_hash,
        )
        if claim.status == "conflict":
            return None, _error(
                "IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was already used with a different request",
                409,
            )
        if claim.status == "replay":
            return claim, None
        if claim.status != "claimed":
            return None, _error(
                "IDEMPOTENCY_IN_FLIGHT",
                "a request with this Idempotency-Key is still in progress",
                409,
                {"Retry-After": "1"},
            )
        return claim, None

    def _finish_idempotency(
        conn: sqlite3.Connection,
        claim: db.IdempotencyClaim | None,
        *,
        response: Any,
        job_id: str | None = None,
    ) -> None:
        if claim is None or claim.status != "claimed":
            return
        db.complete_idempotency(
            conn,
            scope=claim.scope,
            key=claim.key,
            request_hash=claim.request_hash,
            response=response,
            job_id=job_id,
        )

    def meter(
        authorization: str | None,
        conn: sqlite3.Connection,
        *,
        container_tag: str | None,
        org_id: str | None,
        operation: str = "request",
        input_chars: int = 0,
        actor_override: tuple[str, str | None] | None = None,
    ) -> None:
        _, key_hash = actor_override or actor(authorization, conn)
        db.record_usage(
            conn,
            key_hash=key_hash,
            container_tag=container_tag,
            org_id=org_id,
            operation=operation,
            input_chars=input_chars,
        )

    def audit(
        authorization: str | None,
        conn: sqlite3.Connection,
        *,
        container_tag: str | None,
        org_id: str | None,
        action: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        outcome: str = "succeeded",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        actor_kind, key_hash = actor(authorization, conn)
        db.append_audit_event(
            conn,
            actor_kind=actor_kind,
            actor_key_hash=key_hash,
            container_tag=container_tag,
            org_id=org_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            metadata=metadata,
        )

    def admin_error(authorization: str | None, conn: sqlite3.Connection) -> JSONResponse | None:
        if _is_admin(authorization, settings):
            return None
        if (
            authorization
            and authorization.startswith("Bearer ")
            and db.lookup_key(conn, authorization[len("Bearer ") :]) is not None
        ):
            return _error("FORBIDDEN", "admin key required", 403)
        return _error("UNAUTHORIZED", "authentication required", 401)

    def credential_ok(authorization: str | None, conn: sqlite3.Connection) -> bool:
        return scope_of(authorization, conn) is not False

    def scope_allows(
        scope: dict | bool,
        container_tag: str,
        org_id: str | None = None,
        project_id: str | None = None,
    ) -> bool:
        if scope is True or scope is None:
            return True
        if scope is False:
            return False
        scope_tag = scope.get("container_tag")
        if scope_tag is not None and scope_tag != container_tag:
            return False
        scope_org = scope.get("org_id")
        if scope_org is not None and org_id != scope_org:
            return False
        scope_project = scope.get("project_id")
        return scope_project is None or scope_project == project_id

    def may_access(
        authorization: str | None,
        conn: sqlite3.Connection,
        container_tag: str,
        org_id: str | None = None,
        project_id: str | None = None,
    ) -> bool:
        return scope_allows(scope_of(authorization, conn), container_tag, org_id, project_id)

    def authorize(
        authorization: str | None,
        conn: sqlite3.Connection,
        container_tag: str,
        org_id: str | None = None,
        *,
        project_id: str | None = None,
        operation: str = "request",
        input_chars: int = 0,
    ) -> str | None:
        if not settings.auth_enabled:
            meter(
                authorization,
                conn,
                container_tag=container_tag,
                org_id=org_id,
                operation=operation,
                input_chars=input_chars,
            )
            return org_id
        scope = scope_of(authorization, conn)
        if scope is False:
            raise HTTPException(status_code=401, detail="UNAUTHORIZED")
        effective_org = org_id
        if isinstance(scope, dict) and scope.get("org_id") is not None and org_id is None:
            effective_org = scope["org_id"]
        scope = scope_of(authorization, conn)
        effective_project = project_id
        if effective_project is None and isinstance(scope, dict):
            effective_project = scope.get("project_id")
        if not scope_allows(scope, container_tag, effective_org, effective_project):
            raise HTTPException(status_code=403, detail="FORBIDDEN")
        meter(
            authorization,
            conn,
            container_tag=container_tag,
            org_id=effective_org,
            operation=operation,
            input_chars=input_chars,
            actor_override=actor_from_scope(authorization, scope),
        )
        return effective_org

    def _authorized(authorization: str | None, conn: sqlite3.Connection) -> bool:
        return credential_ok(authorization, conn)

    def project_scope(
        authorization: str | None, conn: sqlite3.Connection
    ) -> tuple[str | None, str | None]:
        scope = scope_of(authorization, conn)
        if isinstance(scope, dict) and scope.get("project_id"):
            project_id = str(scope["project_id"])
            row = conn.execute("SELECT org_id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=403, detail="FORBIDDEN")
            return str(row["org_id"]), project_id
        return None, None

    def _may_access(
        authorization: str | None,
        conn: sqlite3.Connection,
        container_tag: str,
        org_id: str | None = None,
        project_id: str | None = None,
    ) -> bool:
        if not may_access(authorization, conn, container_tag, org_id, project_id):
            return False
        scope = scope_of(authorization, conn)
        if isinstance(scope, dict) and scope.get("project_id") is not None:
            return scope["project_id"] == project_id
        return True

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
        org_id = authorize(
            authorization,
            conn,
            doc.containerTag,
            doc.org_id,
            operation="document",
            input_chars=len(doc.content),
        )
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
        audit(
            authorization,
            conn,
            container_tag=doc.containerTag,
            org_id=org_id,
            action="document.created",
            resource_type="document",
            resource_id=created["id"],
            metadata={"job_id": job_id},
        )
        return {"id": created["id"], "status": "queued", "job_id": job_id}

    @app.post("/v3/memories/add/")
    @app.post("/v1/memories/")
    def mem0_add(
        body: Mem0AddIn,
        conn: DbConn,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        values = body.model_dump()
        if body.containerTag is not None and any(
            values.get(key) is not None for key in ("user_id", "agent_id", "app_id", "run_id")
        ):
            return _error(
                "VALIDATION_ERROR",
                "containerTag cannot be combined with an entity id",
                422,
            )
        if body.containerTag is not None and (
            not body.containerTag.startswith("mem0:") or body.containerTag.count(":") < 2
        ):
            return _error(
                "VALIDATION_ERROR",
                "containerTag must identify a mem0 entity",
                422,
            )
        tag = body.containerTag or _mem0_entity_tag(values)
        if tag is None:
            return _error(
                "VALIDATION_ERROR",
                "one of user_id, agent_id, app_id, or run_id is required",
                422,
            )
        auth = _compat_auth_header(authorization)
        scope = scope_of(auth, conn)
        project_id = scope.get("project_id") if isinstance(scope, dict) else None
        if project_id is not None:
            project = conn.execute(
                "SELECT org_id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                return _error("NOT_FOUND", "project not found", 404)
            org_id = str(project["org_id"])
        else:
            org_id = None
        org_id = authorize(
            auth,
            conn,
            tag,
            org_id,
            project_id=project_id,
            operation="document",
            input_chars=sum(len(m.content) for m in body.messages),
        )
        claim, claim_error = _begin_idempotency(
            auth,
            conn,
            idempotency_key,
            operation="mem0.add",
            payload=values,
        )
        if claim_error is not None:
            return claim_error
        if claim is not None and claim.status == "replay":
            return claim.response
        try:
            content = "\n".join(f"{message.role}: {message.content}" for message in body.messages)
            document = db.create_document(
                conn,
                container_tag=tag,
                content=content,
                metadata=body.metadata,
                org_id=org_id,
                project_id=project_id,
            )
            if not body.infer or app.state.llm is None:
                for message in body.messages:
                    if message.role != "system":
                        db.ensure_input_memory(
                            conn,
                            text=message.content,
                            container_tag=tag,
                            metadata=body.metadata,
                            org_id=org_id,
                            document_id=document["id"],
                            project_id=project_id,
                        )
            job_id = jobs.enqueue(
                conn,
                kind="ingest",
                payload={
                    "document_id": document["id"],
                    "dreaming": "dynamic",
                    "container_tag": tag,
                    "org_id": org_id,
                    "project_id": project_id,
                },
            )
            audit(
                auth,
                conn,
                container_tag=tag,
                org_id=org_id,
                action="mem0.add",
                resource_type="document",
                resource_id=document["id"],
                metadata={"job_id": job_id, "infer": body.infer},
            )
            response = {"event_id": job_id, "status": "PENDING"}
            _finish_idempotency(conn, claim, response=response, job_id=job_id)
            return response
        except Exception:
            if claim is not None and claim.status == "claimed":
                db.release_idempotency(conn, scope=claim.scope, key=claim.key)
            raise

    @app.get("/v1/event/{event_id}/")
    def mem0_event(event_id: str, conn: DbConn, authorization: str | None = Header(default=None)):
        job = jobs.get(conn, event_id)
        if job is None:
            return _error("NOT_FOUND", "event not found", 404)
        auth = _compat_auth_header(authorization)
        if settings.auth_enabled and not credential_ok(auth, conn):
            return _error("UNAUTHORIZED", "authentication required", 401)
        tag = _job_tag(conn, job)
        org_id = _job_org(conn, job)
        project_id = (job.get("payload") or {}).get("project_id")
        if settings.auth_enabled and (
            tag is None
            or not may_access(
                auth, conn, tag, org_id, project_id if isinstance(project_id, str) else None
            )
        ):
            return _error("NOT_FOUND", "event not found", 404)
        status = {
            "queued": "PENDING",
            "running": "PENDING",
            "done": "SUCCEEDED",
            "failed": "FAILED",
        }.get(job["status"], "PENDING")
        return {"event_id": event_id, "status": status, "results": job.get("result", {})}

    @app.post("/v3/memories/search/")
    @app.post("/v1/memories/search/")
    def mem0_search(
        body: Mem0SearchIn, conn: DbConn, authorization: str | None = Header(default=None)
    ):
        tag = _mem0_entity_tag(body.filters)
        if tag is None:
            return _error(
                "VALIDATION_ERROR",
                "filters must include user_id, agent_id, app_id, or run_id",
                422,
            )
        auth = _compat_auth_header(authorization)
        scope = scope_of(auth, conn)
        project_id = scope.get("project_id") if isinstance(scope, dict) else None
        org_id = authorize(
            auth,
            conn,
            tag,
            project_id=project_id,
            operation="search",
            input_chars=len(body.query),
        )
        hits = search_memory_records(
            conn,
            app.state.embedder,
            body.query,
            container_tag=tag,
            org_id=org_id,
            project_id=project_id,
            limit=body.top_k,
            threshold=body.threshold,
            filters=body.filters,
            show_expired=body.show_expired,
        )
        return {
            "results": [
                {
                    "id": hit["id"],
                    "memory": hit["memory"],
                    "score": hit["similarity"],
                    "metadata": hit["metadata"],
                }
                for hit in hits
            ]
        }

    def _memory_response(memory: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": memory["id"],
            "memory": memory["text"],
            "metadata": memory["metadata"],
            "created_at": memory["created_at"],
            "updated_at": memory["updated_at"],
            "version": memory["version"],
            "expiration_date": (
                time.strftime("%Y-%m-%d", time.gmtime(memory["expires_at"]))
                if memory.get("expires_at") is not None
                else None
            ),
        }

    def _memory_history_response(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "memory_id": row["memory_id"],
            "old_memory": row["old_text"],
            "new_memory": row["new_text"],
            "event": row["event"],
            "metadata": row["metadata"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row.get("updated_at") or row["created_at"],
            **(
                {"content_hash": row["content_hash"]} if row.get("content_hash") is not None else {}
            ),
        }

    def _validate_batch_items(items: Any, *, update: bool) -> JSONResponse | None:
        if not isinstance(items, list) or not items or len(items) > 1000:
            return _error("VALIDATION_ERROR", "memories must contain 1-1000 items", 422)
        allowed = {"memory_id", "text", "metadata"} if update else {"memory_id"}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                return _error("VALIDATION_ERROR", f"memories[{index}] must be an object", 422)
            unknown = set(item) - allowed
            if unknown:
                return _error(
                    "VALIDATION_ERROR",
                    f"memories[{index}] contains unsupported fields",
                    422,
                )
            memory_id = item.get("memory_id")
            if not isinstance(memory_id, str) or not memory_id.strip():
                return _error("VALIDATION_ERROR", f"memories[{index}].memory_id is required", 422)
            if update:
                if "text" not in item and "metadata" not in item:
                    return _error(
                        "VALIDATION_ERROR",
                        f"memories[{index}] must include text or metadata",
                        422,
                    )
                if "text" in item and (
                    not isinstance(item["text"], str) or not item["text"].strip()
                ):
                    return _error(
                        "VALIDATION_ERROR", f"memories[{index}].text must be non-empty", 422
                    )
                if "metadata" in item and not isinstance(item["metadata"], dict):
                    return _error(
                        "VALIDATION_ERROR", f"memories[{index}].metadata must be an object", 422
                    )
        return None

    def _expiration_timestamp(value: Any) -> float | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("expiration_date must be YYYY-MM-DD")
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() + 86399
        except ValueError as exc:
            raise ValueError("expiration_date must be YYYY-MM-DD") from exc

    def _event_timestamp(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                try:
                    return datetime.fromisoformat(value).timestamp()
                except ValueError as exc:
                    raise ValueError(
                        "timestamp must be a Unix timestamp or ISO-8601 string"
                    ) from exc
        raise ValueError("timestamp must be a Unix timestamp or ISO-8601 string")

    def _get_memory_or_error(
        memory_id: str,
        conn: sqlite3.Connection,
        authorization: str | None,
        *,
        include_deleted: bool = False,
    ) -> tuple[dict[str, Any] | None, JSONResponse | None]:
        try:
            memory = db.get_memory(conn, memory_id)
        except KeyError:
            return None, _error("NOT_FOUND", "memory not found", 404)
        if memory["state"] == "deleted" and not include_deleted:
            return None, _error("NOT_FOUND", "memory not found", 404)
        if settings.auth_enabled:
            auth = _compat_auth_header(authorization)
            if not credential_ok(auth, conn):
                return None, _error("UNAUTHORIZED", "authentication required", 401)
            if not may_access(
                auth,
                conn,
                memory["container_tag"],
                memory.get("org_id"),
                memory.get("project_id"),
            ):
                return None, _error("NOT_FOUND", "memory not found", 404)
        return memory, None

    @app.put("/v1/batch/")
    def mem0_batch_update(
        body: dict[str, Any],
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        items = body.get("memories") if isinstance(body, dict) else None
        validation = _validate_batch_items(items, update=True)
        if validation is not None:
            return validation
        assert isinstance(items, list)
        auth = _compat_auth_header(authorization)
        resolved: list[tuple[dict[str, Any], dict[str, Any]]] = []
        seen: set[str] = set()
        for item in items:
            memory_id = str(item["memory_id"])
            if memory_id in seen:
                return _error("VALIDATION_ERROR", "duplicate memory_id in batch", 422)
            seen.add(memory_id)
            memory, error = _get_memory_or_error(memory_id, conn, authorization)
            if error is not None:
                return error
            assert memory is not None
            resolved.append((item, memory))
        try:
            conn.execute("BEGIN IMMEDIATE")
            for item, _ in resolved:
                db.update_memory(
                    conn,
                    str(item["memory_id"]),
                    text=item.get("text"),
                    metadata=item.get("metadata"),
                    actor_key_hash=actor(auth, conn)[1],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        for item, memory in resolved:
            job_id = jobs.enqueue(
                conn,
                kind="reindex_memory",
                payload={
                    "memory_id": str(item["memory_id"]),
                    "container_tag": memory["container_tag"],
                    "org_id": memory.get("org_id"),
                },
            )
            audit(
                auth,
                conn,
                container_tag=memory["container_tag"],
                org_id=memory.get("org_id"),
                action="memory.batch_updated",
                resource_type="memory",
                resource_id=str(item["memory_id"]),
                metadata={"job_id": job_id},
            )
        meter(auth, conn, container_tag=None, org_id=None, operation="request")
        return {"message": f"Successfully updated {len(resolved)} memories"}

    @app.delete("/v1/batch/")
    def mem0_batch_delete(
        body: dict[str, Any],
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        items = body.get("memories") if isinstance(body, dict) else None
        validation = _validate_batch_items(items, update=False)
        if validation is not None:
            return validation
        assert isinstance(items, list)
        auth = _compat_auth_header(authorization)
        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            memory_id = str(item["memory_id"])
            if memory_id in seen:
                return _error("VALIDATION_ERROR", "duplicate memory_id in batch", 422)
            seen.add(memory_id)
            memory, error = _get_memory_or_error(memory_id, conn, authorization)
            if error is not None:
                return error
            assert memory is not None
            resolved.append(memory)
        try:
            conn.execute("BEGIN IMMEDIATE")
            deleted_ids: list[str] = []
            for memory in resolved:
                result = db.soft_delete_memory(
                    conn, memory["id"], actor_key_hash=actor(auth, conn)[1]
                )
                if result["deleted"]:
                    deleted_ids.append(memory["id"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if deleted_ids:
            vector_store_for(conn).delete(ids=deleted_ids)
        for memory in resolved:
            audit(
                auth,
                conn,
                container_tag=memory["container_tag"],
                org_id=memory.get("org_id"),
                action="memory.batch_deleted",
                resource_type="memory",
                resource_id=memory["id"],
            )
        meter(auth, conn, container_tag=None, org_id=None, operation="request")
        return {"message": f"Successfully deleted {len(deleted_ids)} memories"}

    @app.delete("/v1/memories/")
    def mem0_delete_all(
        conn: DbConn,
        user_id: str | None = None,
        agent_id: str | None = None,
        app_id: str | None = None,
        run_id: str | None = None,
        authorization: str | None = Header(default=None),
    ):
        filters = {
            key: value
            for key, value in (
                ("user_id", user_id),
                ("agent_id", agent_id),
                ("app_id", app_id),
                ("run_id", run_id),
            )
            if value is not None
        }
        if not filters:
            return _error(
                "VALIDATION_ERROR",
                "at least one user_id, agent_id, app_id, or run_id filter is required",
                422,
            )
        wildcard = any(value == "*" for value in filters.values())
        auth = _compat_auth_header(authorization)
        scope = scope_of(auth, conn)
        if wildcard and settings.auth_enabled and not _is_admin(auth, settings):
            return _error("FORBIDDEN", "an administrative key is required for wildcard delete", 403)
        exact_tag = _mem0_entity_tag(filters) if not wildcard else None
        org_id: str | None = None
        if exact_tag is not None:
            org_id = authorize(auth, conn, exact_tag, operation="request")
        elif settings.auth_enabled:
            scope = scope_of(auth, conn)
            if scope is False:
                return _error("UNAUTHORIZED", "authentication required", 401)
            if isinstance(scope, dict):
                org_id = scope.get("org_id")
        payload: dict[str, Any] = {"filters": filters, "org_id": org_id, "project_id": None}
        if isinstance(scope, dict):
            payload["project_id"] = scope.get("project_id")
        if exact_tag is not None:
            payload["container_tag"] = exact_tag
        event_id = jobs.enqueue(conn, kind="delete_all_memories", payload=payload)
        audit(
            auth,
            conn,
            container_tag=exact_tag,
            org_id=org_id,
            action="memory.delete_all_queued",
            resource_type="memory_scope",
            resource_id=None,
            metadata={"event_id": event_id, "filters": filters},
        )
        return {
            "message": "Delete in progress. This may take some time.",
            "event_id": event_id,
        }

    @app.get("/v1/ping/")
    def mem0_ping(conn: DbConn, authorization: str | None = Header(default=None)):
        if settings.auth_enabled and not credential_ok(authorization, conn):
            return _error("UNAUTHORIZED", "authentication required", 401)
        scope = scope_of(authorization, conn)
        if isinstance(scope, dict) and scope.get("project_id"):
            project_id = str(scope["project_id"])
            row = conn.execute(
                "SELECT id, org_id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                return _error("NOT_FOUND", "project not found", 404)
            org_id = str(row["org_id"])
        else:
            org_id, project_id = "local-org", "local-project"
        meter(authorization, conn, container_tag=None, org_id=org_id)
        return {
            "status": "ok",
            "user_email": None,
            "org_id": org_id,
            "project_id": project_id,
        }

    def _project_admin(authorization: str | None, conn: sqlite3.Connection) -> bool:
        if _is_admin(authorization, settings):
            return True
        scope = scope_of(authorization, conn)
        if not isinstance(scope, dict) or not scope.get("project_id"):
            return False
        row = conn.execute(
            "SELECT role FROM project_members WHERE project_id = ? AND email = ?",
            (scope["project_id"], scope.get("identity_subject", scope.get("key_hash"))),
        ).fetchone()
        return row is not None and row["role"] == "OWNER"

    def _project_response(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "org_id": row["org_id"],
            "name": row["name"],
            "description": row["description"],
            "custom_instructions": row["custom_instructions"],
            "custom_categories": json.loads(row["custom_categories"] or "[]"),
            "agent_custom_instructions": row["agent_custom_instructions"],
            "multilingual": bool(row["multilingual"]),
            "decay": bool(row["decay"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _webhook_project(authorization: str | None, conn: sqlite3.Connection, project_id: str):
        if _is_admin(authorization, settings):
            return None
        scope = scope_of(_compat_auth_header(authorization), conn)
        if not isinstance(scope, dict) or scope.get("project_id") != project_id:
            return _error("FORBIDDEN", "project webhook administrator required", 403)
        return None

    @app.post("/api/v1/webhooks/projects/{project_id}/", status_code=201)
    def create_webhook_route(
        project_id: str,
        body: dict[str, Any],
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        denied = _webhook_project(authorization, conn, project_id)
        if denied is not None:
            return denied
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            return _error("NOT_FOUND", "project not found", 404)
        try:
            created = webhooks.create_webhook(
                conn,
                project_id=project_id,
                name=body.get("name", ""),
                url=body.get("url", ""),
                event_types=body.get("event_types", []),
                allow_private=settings.webhook_allow_private_targets,
            )
        except (ValueError, webhooks.UnsafeWebhookTarget) as exc:
            return _error("VALIDATION_ERROR", str(exc), 422)
        audit(
            authorization,
            conn,
            container_tag=None,
            org_id=None,
            action="webhook.created",
            resource_type="webhook",
            resource_id=created["id"],
            metadata={"project_id": project_id, "event_types": created["event_types"]},
        )
        return created

    @app.get("/api/v1/webhooks/projects/{project_id}/")
    def list_webhooks_route(
        project_id: str,
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        denied = _webhook_project(authorization, conn, project_id)
        if denied is not None:
            return denied
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            return _error("NOT_FOUND", "project not found", 404)
        return webhooks.list_webhooks(conn, project_id=project_id)

    @app.get("/api/v1/webhooks/{webhook_id}/")
    def get_webhook_route(
        webhook_id: str,
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        row = conn.execute("SELECT project_id FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
        if row is None:
            return _error("NOT_FOUND", "webhook not found", 404)
        denied = _webhook_project(authorization, conn, str(row["project_id"]))
        if denied is not None:
            return denied
        result = webhooks.get_webhook(conn, webhook_id)
        assert result is not None
        return result

    @app.put("/api/v1/webhooks/{webhook_id}/")
    def update_webhook_route(
        webhook_id: str,
        body: dict[str, Any],
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        row = conn.execute("SELECT project_id FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
        if row is None:
            return _error("NOT_FOUND", "webhook not found", 404)
        project_id = str(row["project_id"])
        denied = _webhook_project(authorization, conn, project_id)
        if denied is not None:
            return denied
        try:
            result = webhooks.update_webhook(
                conn,
                webhook_id=webhook_id,
                project_id=project_id,
                name=body.get("name"),
                url=body.get("url"),
                event_types=body.get("event_types"),
                allow_private=settings.webhook_allow_private_targets,
            )
        except (ValueError, webhooks.UnsafeWebhookTarget) as exc:
            return _error("VALIDATION_ERROR", str(exc), 422)
        if result is None:
            return _error("NOT_FOUND", "webhook not found", 404)
        audit(
            authorization,
            conn,
            container_tag=None,
            org_id=None,
            action="webhook.updated",
            resource_type="webhook",
            resource_id=webhook_id,
            metadata={"project_id": project_id},
        )
        return {"message": "Webhook updated successfully", "webhook": result}

    @app.delete("/api/v1/webhooks/{webhook_id}/")
    def delete_webhook_route(
        webhook_id: str,
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        row = conn.execute("SELECT project_id FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
        if row is None:
            return _error("NOT_FOUND", "webhook not found", 404)
        project_id = str(row["project_id"])
        denied = _webhook_project(authorization, conn, project_id)
        if denied is not None:
            return denied
        deleted = webhooks.delete_webhook(conn, webhook_id=webhook_id, project_id=project_id)
        audit(
            authorization,
            conn,
            container_tag=None,
            org_id=None,
            action="webhook.deleted",
            resource_type="webhook",
            resource_id=webhook_id,
            metadata={"project_id": project_id},
        )
        return {"message": "Webhook deleted successfully", "deleted": deleted}

    @app.post("/v4/webhooks/{webhook_id}/rotate-secret")
    def rotate_webhook_secret_route(
        webhook_id: str,
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        row = conn.execute("SELECT project_id FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
        if row is None:
            return _error("NOT_FOUND", "webhook not found", 404)
        project_id = str(row["project_id"])
        denied = _webhook_project(authorization, conn, project_id)
        if denied is not None:
            return denied
        secret = webhooks.rotate_secret(conn, webhook_id=webhook_id, project_id=project_id)
        if secret is None:
            return _error("NOT_FOUND", "webhook not found", 404)
        audit(
            authorization,
            conn,
            container_tag=None,
            org_id=None,
            action="webhook.secret_rotated",
            resource_type="webhook",
            resource_id=webhook_id,
            metadata={"project_id": project_id},
        )
        return {"id": webhook_id, "secret": secret}

    @app.get("/v4/projects/{project_id}/webhooks/deliveries")
    def list_webhook_deliveries_route(
        project_id: str,
        conn: DbConn,
        authorization: str | None = Header(default=None),
        limit: int = 100,
        offset: int = 0,
    ):
        denied = _webhook_project(authorization, conn, project_id)
        if denied is not None:
            return denied
        return {
            "deliveries": webhooks.list_deliveries(
                conn,
                project_id=project_id,
                limit=max(1, min(limit, 500)),
                offset=max(0, offset),
            )
        }

    @app.post("/v4/webhooks/deliveries/{delivery_id}/replay")
    def replay_webhook_delivery_route(
        delivery_id: str,
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        row = conn.execute(
            "SELECT project_id FROM webhook_deliveries WHERE id = ?", (delivery_id,)
        ).fetchone()
        if row is None:
            return _error("NOT_FOUND", "delivery not found", 404)
        denied = _webhook_project(authorization, conn, str(row["project_id"]))
        if denied is not None:
            return denied
        result = webhooks.replay_delivery(conn, delivery_id)
        if result is None:
            return _error("CONFLICT", "delivery is not replayable", 409)
        return result

    @app.get("/api/v1/orgs/organizations/{org_id}/projects/", status_code=200)
    def list_projects_route(
        org_id: str,
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        denied = admin_error(authorization, conn)
        if denied is not None:
            return denied
        rows = conn.execute(
            "SELECT * FROM projects WHERE org_id = ? ORDER BY updated_at DESC", (org_id,)
        ).fetchall()
        return [_project_response(row) for row in rows]

    @app.post("/api/v1/orgs/organizations/{org_id}/projects/", status_code=201)
    def create_project(
        org_id: str,
        body: dict[str, Any],
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        denied = admin_error(authorization, conn)
        if denied is not None:
            return denied
        if not isinstance(body.get("name"), str) or not body["name"].strip():
            return _error("VALIDATION_ERROR", "name is required", 422)
        now = time.time()
        project_id = "project-" + secrets.token_urlsafe(12)
        conn.execute(
            "INSERT INTO projects(id, org_id, name, description, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, org_id, body["name"].strip(), body.get("description"), now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        assert row is not None
        audit(
            authorization,
            conn,
            container_tag=None,
            org_id=org_id,
            action="project.created",
            resource_id=project_id,
        )
        return _project_response(row)

    @app.get("/api/v1/orgs/organizations/{org_id}/projects/{project_id}/")
    def get_project(
        org_id: str,
        project_id: str,
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        denied = admin_error(authorization, conn)
        if denied is not None:
            return denied
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND org_id = ?", (project_id, org_id)
        ).fetchone()
        if row is None:
            return _error("NOT_FOUND", "project not found", 404)
        return _project_response(row)

    @app.patch("/api/v1/orgs/organizations/{org_id}/projects/{project_id}/")
    def update_project(
        org_id: str,
        project_id: str,
        body: dict[str, Any],
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        denied = admin_error(authorization, conn)
        if denied is not None:
            return denied
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND org_id = ?", (project_id, org_id)
        ).fetchone()
        if row is None:
            return _error("NOT_FOUND", "project not found", 404)
        allowed = {
            "name",
            "description",
            "custom_instructions",
            "custom_categories",
            "agent_custom_instructions",
            "multilingual",
            "decay",
        }
        if not body or set(body) - allowed:
            return _error("VALIDATION_ERROR", "unsupported project settings", 422)
        values = dict(row)
        for key in ("name", "description", "custom_instructions", "agent_custom_instructions"):
            if key in body:
                values[key] = body[key]
        if "custom_categories" in body:
            if not isinstance(body["custom_categories"], list):
                return _error("VALIDATION_ERROR", "custom_categories must be a list", 422)
            values["custom_categories"] = json.dumps(body["custom_categories"])
        for key in ("multilingual", "decay"):
            if key in body:
                values[key] = int(bool(body[key]))
        values["updated_at"] = time.time()
        conn.execute(
            "UPDATE projects SET name = ?, description = ?, custom_instructions = ?,"
            " custom_categories = ?, agent_custom_instructions = ?, multilingual = ?, decay = ?,"
            " updated_at = ? WHERE id = ? AND org_id = ?",
            (
                values["name"],
                values["description"],
                values["custom_instructions"],
                values["custom_categories"],
                values["agent_custom_instructions"],
                values["multilingual"],
                values["decay"],
                values["updated_at"],
                project_id,
                org_id,
            ),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        assert updated is not None
        return _project_response(updated)

    @app.get("/api/v1/orgs/organizations/{org_id}/projects/{project_id}/members/")
    def list_project_members(
        org_id: str,
        project_id: str,
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        denied = admin_error(authorization, conn)
        if denied is not None:
            return denied
        if (
            conn.execute(
                "SELECT 1 FROM projects WHERE id = ? AND org_id = ?", (project_id, org_id)
            ).fetchone()
            is None
        ):
            return _error("NOT_FOUND", "project not found", 404)
        rows = conn.execute(
            "SELECT email, role, created_at, updated_at FROM project_members WHERE project_id = ? ORDER BY email",
            (project_id,),
        ).fetchall()
        return {"members": [dict(row) for row in rows]}

    @app.post("/api/v1/orgs/organizations/{org_id}/projects/{project_id}/members/", status_code=201)
    def add_project_member(
        org_id: str,
        project_id: str,
        body: dict[str, Any],
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        denied = admin_error(authorization, conn)
        if denied is not None:
            return denied
        email = body.get("email")
        role = body.get("role", "READER")
        if not isinstance(email, str) or "@" not in email or role not in {"OWNER", "READER"}:
            return _error("VALIDATION_ERROR", "email and role are required", 422)
        if (
            conn.execute(
                "SELECT 1 FROM projects WHERE id = ? AND org_id = ?", (project_id, org_id)
            ).fetchone()
            is None
        ):
            return _error("NOT_FOUND", "project not found", 404)
        now = time.time()
        conn.execute(
            "INSERT INTO project_members(project_id, email, role, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_id, email) DO UPDATE SET role = excluded.role, updated_at = excluded.updated_at",
            (project_id, email, role, now, now),
        )
        conn.commit()
        return {"email": email, "role": role, "project_id": project_id}

    @app.put("/api/v1/orgs/organizations/{org_id}/projects/{project_id}/members/")
    def update_project_member(
        org_id: str,
        project_id: str,
        body: dict[str, Any],
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        return add_project_member(org_id, project_id, body, conn, authorization)

    @app.delete("/api/v1/orgs/organizations/{org_id}/projects/{project_id}/members/")
    def remove_project_member(
        org_id: str,
        project_id: str,
        email: str,
        conn: DbConn,
        authorization: str | None = Header(default=None),
    ):
        denied = admin_error(authorization, conn)
        if denied is not None:
            return denied
        changed = conn.execute(
            "DELETE FROM project_members WHERE project_id = ? AND email = ?", (project_id, email)
        ).rowcount
        conn.commit()
        return {"removed": bool(changed)}

    @app.get("/v1/memories/{memory_id}/")
    def mem0_get(memory_id: str, conn: DbConn, authorization: str | None = Header(default=None)):
        memory, error = _get_memory_or_error(memory_id, conn, authorization)
        if error is not None:
            return error
        assert memory is not None
        meter(
            authorization, conn, container_tag=memory["container_tag"], org_id=memory.get("org_id")
        )
        return _memory_response(memory)

    @app.put("/v1/memories/{memory_id}/")
    def mem0_update(
        memory_id: str,
        body: dict[str, Any],
        conn: DbConn,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        memory, error = _get_memory_or_error(memory_id, conn, authorization)
        if error is not None:
            return error
        assert memory is not None
        allowed = {"text", "metadata", "timestamp", "expiration_date"}
        unknown = set(body) - allowed
        if unknown or not body:
            return _error(
                "VALIDATION_ERROR",
                "provide at least one of text, metadata, timestamp, or expiration_date",
                422,
            )
        if "text" in body and (not isinstance(body["text"], str) or not body["text"].strip()):
            return _error("VALIDATION_ERROR", "text must be non-empty", 422)
        if "metadata" in body and not isinstance(body["metadata"], dict):
            return _error("VALIDATION_ERROR", "metadata must be an object", 422)
        auth = _compat_auth_header(authorization)
        claim, claim_error = _begin_idempotency(
            auth,
            conn,
            idempotency_key,
            operation="mem0.update",
            payload={"memory_id": memory_id, "body": body},
        )
        if claim_error is not None:
            return claim_error
        if claim is not None and claim.status == "replay":
            return claim.response
        try:
            old_text = memory["text"]
            new_text = body.get("text", old_text)
            expires_at = memory.get("expires_at")
            if "expiration_date" in body:
                expires_at = _expiration_timestamp(body["expiration_date"])
            timestamp = _event_timestamp(body.get("timestamp")) if "timestamp" in body else None
            db.update_memory(
                conn,
                memory_id,
                text=new_text if "text" in body else None,
                metadata=body.get("metadata"),
                timestamp=timestamp,
                expires_at=expires_at if "expiration_date" in body else db._UNSET,
                actor_key_hash=actor(auth, conn)[1],
            )
            conn.commit()
        except (TypeError, ValueError) as exc:
            conn.rollback()
            if claim is not None and claim.status == "claimed":
                db.release_idempotency(conn, scope=claim.scope, key=claim.key)
            return _error("VALIDATION_ERROR", str(exc), 422)
        except Exception:
            conn.rollback()
            if claim is not None and claim.status == "claimed":
                db.release_idempotency(conn, scope=claim.scope, key=claim.key)
            raise
        updated = db.get_memory(conn, memory_id)
        job_id = jobs.enqueue(
            conn,
            kind="reindex_memory",
            payload={
                "memory_id": memory_id,
                "container_tag": updated["container_tag"],
                "org_id": updated.get("org_id"),
            },
        )
        audit(
            auth,
            conn,
            container_tag=updated["container_tag"],
            org_id=updated.get("org_id"),
            action="memory.updated",
            resource_type="memory",
            resource_id=memory_id,
            metadata={"job_id": job_id},
        )
        response = _memory_response(updated)
        response["job_id"] = job_id
        _finish_idempotency(conn, claim, response=response, job_id=job_id)
        return response

    @app.delete("/v1/memories/{memory_id}/")
    def mem0_delete(
        memory_id: str,
        conn: DbConn,
        authorization: str | None = Header(default=None),
        delete_linked: bool = False,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        memory, error = _get_memory_or_error(memory_id, conn, authorization)
        if error is not None:
            return error
        assert memory is not None
        auth = _compat_auth_header(authorization)
        claim, claim_error = _begin_idempotency(
            auth,
            conn,
            idempotency_key,
            operation="mem0.delete",
            payload={"memory_id": memory_id, "delete_linked": delete_linked},
        )
        if claim_error is not None:
            return claim_error
        if claim is not None and claim.status == "replay":
            return claim.response
        try:
            result = db.soft_delete_memory(
                conn,
                memory_id,
                actor_key_hash=actor(auth, conn)[1],
                delete_linked=delete_linked,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            if claim is not None and claim.status == "claimed":
                db.release_idempotency(conn, scope=claim.scope, key=claim.key)
            raise
        vector_store_for(conn).delete(ids=result["deleted_ids"])
        audit(
            auth,
            conn,
            container_tag=memory["container_tag"],
            org_id=memory.get("org_id"),
            action="memory.deleted",
            resource_type="memory",
            resource_id=memory_id,
            metadata={"cascade_count": result["cascade_count"]},
        )
        response = {
            "message": "Memory deleted successfully!",
            "cascade_count": result["cascade_count"],
        }
        _finish_idempotency(conn, claim, response=response)
        return response

    @app.get("/v1/memories/{memory_id}/history/")
    def mem0_history(
        memory_id: str, conn: DbConn, authorization: str | None = Header(default=None)
    ):
        memory, error = _get_memory_or_error(memory_id, conn, authorization, include_deleted=True)
        if error is not None:
            return error
        assert memory is not None
        return [
            _memory_history_response(row)
            for row in db.list_memory_history(
                conn, memory_id, container_tag=memory["container_tag"]
            )
        ]

    @app.get("/v1/memories/")
    def mem0_list(
        conn: DbConn,
        user_id: str | None = None,
        agent_id: str | None = None,
        app_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
        authorization: str | None = Header(default=None),
    ):
        tag = _mem0_entity_tag(
            {"user_id": user_id, "agent_id": agent_id, "app_id": app_id, "run_id": run_id}
        )
        if tag is None:
            return _error("VALIDATION_ERROR", "an entity id is required", 422)
        auth = _compat_auth_header(authorization)
        scope = scope_of(auth, conn)
        project_id = scope.get("project_id") if isinstance(scope, dict) else None
        org_id = authorize(auth, conn, tag, project_id=project_id, operation="request")
        memories = db.list_memories(
            conn,
            tag,
            org_id=org_id,
            project_id=project_id,
            show_expired=False,
            limit=max(1, min(limit, 100)),
        )
        return {
            "results": [_memory_response(memory) for memory in memories],
            "count": len(memories),
            "next": None,
            "previous": None,
        }

    @app.post("/v3/memories/")
    def mem0_get_all(
        body: dict[str, Any],
        conn: DbConn,
        page: int = 1,
        page_size: int = 100,
        authorization: str | None = Header(default=None),
    ):
        filters = body.get("filters")
        if not isinstance(filters, dict):
            return _error("VALIDATION_ERROR", "filters must be an object", 422)
        tag = _mem0_entity_tag(filters)
        if tag is None:
            return _error("VALIDATION_ERROR", "an entity id is required", 422)
        if page < 1 or not 1 <= page_size <= 200:
            return _error("VALIDATION_ERROR", "invalid pagination", 422)
        auth = _compat_auth_header(authorization)
        scope = scope_of(auth, conn)
        project_id = scope.get("project_id") if isinstance(scope, dict) else None
        org_id = authorize(auth, conn, tag, project_id=project_id, operation="request")
        show_expired = bool(body.get("show_expired", False))
        all_memories = db.list_memories(
            conn, tag, org_id=org_id, project_id=project_id, show_expired=show_expired
        )
        start = (page - 1) * page_size
        page_items = all_memories[start : start + page_size]
        return {
            "count": len(all_memories),
            "next": page + 1 if start + page_size < len(all_memories) else None,
            "previous": page - 1 if page > 1 else None,
            "results": [_memory_response(memory) for memory in page_items],
        }

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
        meter(
            authorization,
            conn,
            container_tag=doc["container_tag"],
            org_id=doc.get("org_id"),
        )
        return {"id": doc["id"], "containerTag": doc["container_tag"], "status": doc["status"]}

    def _active_share_link(conn: sqlite3.Connection, link: dict[str, Any] | None) -> bool:
        return bool(
            link
            and link.get("revoked_at") is None
            and float(link.get("expires_at") or 0) > time.time()
        )

    @app.post("/v4/share-links", status_code=201)
    def create_share_link(
        body: ShareIn, conn: DbConn, authorization: str | None = Header(default=None)
    ):
        try:
            document = db.get_document(conn, body.document_id)
        except KeyError:
            return _error("NOT_FOUND", "document not found", 404)
        if settings.auth_enabled and not _authorized(authorization, conn):
            return _error("UNAUTHORIZED", "authentication required", 401)
        if settings.auth_enabled and not _may_access(
            authorization, conn, document["container_tag"], document.get("org_id")
        ):
            return _error("NOT_FOUND", "document not found", 404)
        org_id = authorize(
            authorization,
            conn,
            document["container_tag"],
            document.get("org_id"),
            operation="request",
        )
        actor_kind, key_hash = actor(authorization, conn)
        token = "share_" + secrets.token_urlsafe(32)
        link = db.create_share_link(
            conn,
            token_hash=db.hash_key(token),
            document_id=document["id"],
            created_by=key_hash,
            expires_at=time.time() + body.expires_in,
        )
        audit(
            authorization,
            conn,
            container_tag=document["container_tag"],
            org_id=org_id,
            action="share.created",
            resource_type="share_link",
            resource_id=link["id"],
            metadata={"actor_kind": actor_kind},
        )
        return {
            "id": link["id"],
            "token": token,
            "url": f"/v1/share/{token}",
            "expiresAt": link["expires_at"],
        }

    @app.delete("/v4/share-links/{link_id}")
    def revoke_share_link(
        link_id: str, conn: DbConn, authorization: str | None = Header(default=None)
    ):
        link = db.get_share_link(conn, link_id)
        if link is None:
            return _error("NOT_FOUND", "share link not found", 404)
        try:
            document = db.get_document(conn, link["document_id"])
        except KeyError:
            return _error("NOT_FOUND", "share link not found", 404)
        if settings.auth_enabled and not _authorized(authorization, conn):
            return _error("UNAUTHORIZED", "authentication required", 401)
        if settings.auth_enabled and not _may_access(
            authorization, conn, document["container_tag"], document.get("org_id")
        ):
            return _error("NOT_FOUND", "share link not found", 404)
        org_id = authorize(
            authorization,
            conn,
            document["container_tag"],
            document.get("org_id"),
            operation="request",
        )
        revoked = db.revoke_share_link(conn, link_id)
        audit(
            authorization,
            conn,
            container_tag=document["container_tag"],
            org_id=org_id,
            action="share.revoked",
            resource_type="share_link",
            resource_id=link_id,
            outcome="succeeded" if revoked else "not_found",
        )
        return {"id": link_id, "revoked": revoked}

    @app.get("/v1/share/{token}")
    def read_share(token: str, conn: DbConn):
        link = db.get_share_link_by_token(conn, db.hash_key(token))
        if not _active_share_link(conn, link):
            return _error("NOT_FOUND", "share link not found", 404)
        assert link is not None
        try:
            document = db.get_document(conn, link["document_id"])
        except KeyError:
            return _error("NOT_FOUND", "share link not found", 404)
        return {
            "id": document["id"],
            "containerTag": document["container_tag"],
            "content": document["content"],
            "createdAt": document["created_at"],
            "expiresAt": link["expires_at"],
        }

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
        meter(
            authorization,
            conn,
            container_tag=doc["container_tag"],
            org_id=doc.get("org_id"),
            operation="document",
            input_chars=len(patch.content or doc["content"]),
        )
        old_chunk_ids = [
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM chunks WHERE document_id = ?", (doc_id,)
            ).fetchall()
        ]
        if old_chunk_ids:
            vector_store_for(conn).delete(ids=old_chunk_ids)
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
        audit(
            authorization,
            conn,
            container_tag=updated["container_tag"],
            org_id=updated.get("org_id"),
            action="document.updated",
            resource_type="document",
            resource_id=updated["id"],
            metadata={"job_id": job_id},
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
        org_id = authorize(
            authorization,
            conn,
            query.containerTag,
            query.org_id,
            operation="search",
            input_chars=len(query.q),
        )
        started = time.time()
        queries = (
            expand_query(app.state.llm, query.q)
            if query.rewriteQuery and app.state.llm is not None
            else [query.q]
        )
        vector_store = vector_store_for(conn)
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
                vector_store=vector_store,
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
        if not settings.auth_enabled or _is_admin(authorization, settings):
            raw = db.create_api_key(
                conn,
                container_tag=body.containerTag,
                org_id=body.org_id,
                project_id=body.project_id,
            )
            meter(
                authorization,
                conn,
                container_tag=body.containerTag,
                org_id=body.org_id,
            )
            audit(
                authorization,
                conn,
                container_tag=body.containerTag,
                org_id=body.org_id,
                action="key.issued",
                resource_type="api_key",
                resource_id=db.hash_key(raw)[:16],
                metadata={
                    "containerTag": body.containerTag,
                    "org_id": body.org_id,
                    "project_id": body.project_id,
                },
            )
            return {"key": raw}
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
        revoked = db.revoke_key(conn, key)
        meter(authorization, conn, container_tag=None, org_id=None)
        audit(
            authorization,
            conn,
            container_tag=None,
            org_id=None,
            action="key.revoked",
            resource_type="api_key",
            resource_id=db.hash_key(key)[:16],
            outcome="succeeded" if revoked else "not_found",
        )
        return {"revoked": revoked}

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
        meter(
            authorization,
            conn,
            container_tag=fact["container_tag"],
            org_id=fact.get("org_id"),
        )
        audit(
            authorization,
            conn,
            container_tag=fact["container_tag"],
            org_id=fact.get("org_id"),
            action="fact.deleted",
            resource_type="fact",
            resource_id=fact_id,
        )
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
        vector_store_for(conn).delete(container_tag=tag, org_id=org_id)
        counts = db.purge_tag(conn, tag, org_id=org_id)
        audit(
            authorization,
            conn,
            container_tag=tag,
            org_id=org_id,
            action="tag.purged",
            resource_type="tag",
            resource_id=tag,
            metadata=counts,
        )
        return counts

    @app.post("/v4/facts", status_code=201)
    def create_fact(body: FactIn, conn: DbConn, authorization: str | None = Header(default=None)):
        org_id = authorize(
            authorization,
            conn,
            body.containerTag,
            body.org_id,
            operation="fact",
            input_chars=len(body.subject) + len(body.predicate) + len(body.object),
        )
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
        audit(
            authorization,
            conn,
            container_tag=body.containerTag,
            org_id=org_id,
            action="fact.created",
            resource_type="fact",
            resource_id=fact["id"],
        )
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
        offset: int = 0,
        memory_type: str | None = None,
        org_id: str | None = None,
        authorization: str | None = Header(default=None),
    ):
        org_id = authorize(authorization, conn, containerTag, org_id)
        page_limit = max(1, min(limit, 500))
        facts = list_facts(
            conn,
            container_tag=containerTag,
            include_superseded=include_superseded,
            memory_type=memory_type,
            org_id=org_id,
            limit=page_limit,
            offset=max(0, offset),
        )
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
            "total": count_facts(
                conn,
                container_tag=containerTag,
                include_superseded=include_superseded,
                memory_type=memory_type,
                org_id=org_id,
            ),
        }

    @app.post("/v4/import")
    def import_graph(
        body: ImportIn, conn: DbConn, authorization: str | None = Header(default=None)
    ):
        # Server-local path by design (single-host tool). Any authenticated caller
        # may import, but only into tags they can write.
        slug = body.tag.removeprefix("graphify:")
        org_id = authorize(
            authorization,
            conn,
            f"graphify:{slug}",
            body.org_id,
            operation="document",
        )
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

        counts = sync_records(conn, graph, slug, org_id=org_id)
        audit(
            authorization,
            conn,
            container_tag=f"graphify:{slug}",
            org_id=org_id,
            action="graph.imported",
            resource_type="graph",
            resource_id=slug,
            metadata=counts,
        )
        return counts

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
            meter(authorization, conn, container_tag=tag, org_id=org_id)
        return {
            "id": job["id"],
            "kind": job["kind"],
            "status": job["status"],
            "attempts": job["attempts"],
            "result": job["result"],
            "error": job["error"],
        }

    @app.get("/v4/audit")
    def list_audit_events_ep(
        conn: DbConn,
        containerTag: str | None = None,
        org_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        authorization: str | None = Header(default=None),
    ):
        denied = admin_error(authorization, conn)
        if denied is not None:
            return denied
        events = db.list_audit_events(
            conn,
            container_tag=containerTag,
            org_id=org_id,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )
        return {
            "events": [
                {
                    "id": event["id"],
                    "createdAt": event["created_at"],
                    "actorKind": event["actor_kind"],
                    "actorKeyFingerprint": event["actor_key_hash"],
                    "containerTag": event["container_tag"],
                    "orgId": event["org_id"],
                    "action": event["action"],
                    "resourceType": event["resource_type"],
                    "resourceId": event["resource_id"],
                    "outcome": event["outcome"],
                    "metadata": event["metadata"],
                }
                for event in events
            ],
            "total": db.count_audit_events(conn, container_tag=containerTag, org_id=org_id),
        }

    @app.get("/v4/usage")
    def list_usage_ep(
        conn: DbConn,
        containerTag: str | None = None,
        org_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        authorization: str | None = Header(default=None),
    ):
        denied = admin_error(authorization, conn)
        if denied is not None:
            return denied
        counters = db.list_usage(
            conn,
            container_tag=containerTag,
            org_id=org_id,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )
        return {
            "usage": [
                {
                    "keyFingerprint": row["key_hash"][:16],
                    "containerTag": row["container_tag"],
                    "orgId": row["org_id"],
                    "requests": row["requests"],
                    "searches": row["searches"],
                    "documentWrites": row["document_writes"],
                    "factWrites": row["fact_writes"],
                    "inputChars": row["input_chars"],
                    "createdAt": row["created_at"],
                    "updatedAt": row["updated_at"],
                }
                for row in counters
            ],
            "total": db.count_usage(conn, container_tag=containerTag, org_id=org_id),
        }

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True}

    return app
