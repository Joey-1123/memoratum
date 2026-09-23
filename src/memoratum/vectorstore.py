# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Vector-store contracts and local reference implementations."""

from __future__ import annotations

import json
import math
import sqlite3
import struct
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from memoratum.llm import ProviderUnavailable


@dataclass(frozen=True)
class VectorRecord:
    id: str
    vector: list[float]
    text: str
    kind: str
    container_tag: str
    org_id: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("vector record id is required")
        if not self.text:
            raise ValueError("vector record text is required")
        if not self.vector:
            raise ValueError("vector record must contain at least one dimension")
        object.__setattr__(self, "vector", [float(value) for value in self.vector])
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


@dataclass(frozen=True)
class VectorHit:
    id: str
    score: float
    text: str
    kind: str
    container_tag: str
    org_id: str | None
    metadata: dict[str, Any]
    created_at: float


class VectorStore(Protocol):
    name: str

    def upsert(self, records: Sequence[VectorRecord]) -> None: ...

    def query(
        self,
        vector: Sequence[float],
        *,
        container_tag: str,
        org_id: str | None = None,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorHit]: ...

    def delete(
        self,
        *,
        ids: Sequence[str] | None = None,
        container_tag: str | None = None,
        org_id: str | None = None,
    ) -> int: ...

    def close(self) -> None: ...


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left)) or 1.0
    right_norm = math.sqrt(sum(value * value for value in right)) or 1.0
    return dot / (left_norm * right_norm)


def _matches(metadata: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    return not filters or all(metadata.get(key) == value for key, value in filters.items())


def _rank(
    records: Sequence[VectorRecord],
    vector: Sequence[float],
    limit: int,
    filters: dict[str, Any] | None,
) -> list[VectorHit]:
    hits = [
        VectorHit(
            id=record.id,
            score=_cosine(vector, record.vector),
            text=record.text,
            kind=record.kind,
            container_tag=record.container_tag,
            org_id=record.org_id,
            metadata=dict(record.metadata or {}),
            created_at=record.created_at or time.time(),
        )
        for record in records
        if _matches(record.metadata or {}, filters)
    ]
    hits.sort(key=lambda hit: (-hit.score, hit.id))
    return hits[: max(0, limit)]


def _pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


class InMemoryVectorStore:
    """Deterministic reference store useful for tests and ephemeral indexes."""

    name = "memory"

    def __init__(self) -> None:
        self._records: dict[str, VectorRecord] = {}

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        for record in records:
            self._records[record.id] = record

    def query(
        self,
        vector: Sequence[float],
        *,
        container_tag: str,
        org_id: str | None = None,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        records = [
            record
            for record in self._records.values()
            if record.container_tag == container_tag and (org_id is None or record.org_id == org_id)
        ]
        return _rank(records, vector, limit, filters)

    def delete(
        self,
        *,
        ids: Sequence[str] | None = None,
        container_tag: str | None = None,
        org_id: str | None = None,
    ) -> int:
        if ids is not None:
            targets = set(ids)
        elif container_tag is not None:
            targets = {
                record.id
                for record in self._records.values()
                if record.container_tag == container_tag
                and (org_id is None or record.org_id == org_id)
            }
        else:
            raise ValueError("delete requires ids or a container tag")
        removed = 0
        for record_id in targets:
            if self._records.pop(record_id, None) is not None:
                removed += 1
        return removed

    def close(self) -> None:
        return None


class SQLiteVectorStore:
    """SQLite-backed vector points with the same contract as external stores."""

    name = "sqlite"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        self.conn.executemany(
            """
            INSERT INTO vector_points(
              id, kind, text, vector, container_tag, org_id, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              kind = excluded.kind,
              text = excluded.text,
              vector = excluded.vector,
              container_tag = excluded.container_tag,
              org_id = excluded.org_id,
              metadata = excluded.metadata,
              created_at = excluded.created_at
            """,
            [
                (
                    record.id,
                    record.kind,
                    record.text,
                    _pack(record.vector),
                    record.container_tag,
                    record.org_id,
                    json.dumps(record.metadata or {}, separators=(",", ":")),
                    record.created_at or time.time(),
                )
                for record in records
            ],
        )
        self.conn.commit()

    def query(
        self,
        vector: Sequence[float],
        *,
        container_tag: str,
        org_id: str | None = None,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        where = ["container_tag = ?"]
        params: list[Any] = [container_tag]
        if org_id is not None:
            where.append("org_id = ?")
            params.append(org_id)
        rows = self.conn.execute(
            "SELECT id, kind, text, vector, container_tag, org_id, metadata, created_at"
            f" FROM vector_points WHERE {' AND '.join(where)}",
            params,
        ).fetchall()
        records = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"] or "{}")
            item["vector"] = _unpack(bytes(item["vector"]))
            records.append(
                VectorRecord(
                    id=item["id"],
                    vector=item["vector"],
                    text=item["text"],
                    kind=item["kind"],
                    container_tag=item["container_tag"],
                    org_id=item["org_id"],
                    metadata=item["metadata"],
                    created_at=item["created_at"],
                )
            )
        return _rank(records, vector, limit, filters)

    def delete(
        self,
        *,
        ids: Sequence[str] | None = None,
        container_tag: str | None = None,
        org_id: str | None = None,
    ) -> int:
        conditions: list[str] = []
        params: list[Any] = []
        if ids is not None:
            if not ids:
                return 0
            conditions.append(f"id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        elif container_tag is not None:
            conditions.append("container_tag = ?")
            params.append(container_tag)
            if org_id is not None:
                conditions.append("org_id = ?")
                params.append(org_id)
        else:
            raise ValueError("delete requires ids or a container tag")
        deleted = self.conn.execute(
            f"DELETE FROM vector_points WHERE {' AND '.join(conditions)}", params
        ).rowcount
        self.conn.commit()
        return deleted

    def close(self) -> None:
        return None


class QdrantVectorStore:
    """Qdrant adapter using deterministic UUID point IDs and payload scopes."""

    name = "qdrant"

    def __init__(
        self,
        *,
        client: Any | None = None,
        models: Any | None = None,
        url: str = "",
        api_key: str = "",
        collection_name: str = "memoratum",
        dims: int = 0,
    ) -> None:
        self.client = client if client is not None else self._load_client(url, api_key)
        self.models = models
        self.url = url
        self.collection_name = collection_name
        self.dims = dims
        self._collection_ready = False

    @staticmethod
    def _load_client(url: str, api_key: str) -> Any:
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:
            raise ProviderUnavailable(
                "the qdrant-client package is not installed; install the qdrant extra"
            ) from exc
        return QdrantClient(url=url or None, api_key=api_key or None)

    @staticmethod
    def _load_models() -> Any:
        try:
            from qdrant_client import models
        except ImportError as exc:
            raise ProviderUnavailable(
                "the qdrant-client package is not installed; install the qdrant extra"
            ) from exc
        return models

    def _require_models(self) -> Any:
        if self.models is None:
            self.models = self._load_models()
        return self.models

    @staticmethod
    def _point_id(record_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoratum:{record_id}"))

    @staticmethod
    def _field(value: Any, name: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    def _ensure_collection(self, dims: int) -> None:
        if self._collection_ready:
            return
        models = self._require_models()
        params = models.VectorParams(size=dims, distance=models.Distance.COSINE)
        self.client.get_or_create_collection(
            collection_name=self.collection_name,
            vectors_config=params,
        )
        self._collection_ready = True

    def _payload(self, record: VectorRecord) -> dict[str, Any]:
        return {
            "record_id": record.id,
            "text": record.text,
            "kind": record.kind,
            "container_tag": record.container_tag,
            "org_id": record.org_id,
            "metadata_json": json.dumps(record.metadata or {}, separators=(",", ":")),
            "created_at": record.created_at or time.time(),
        }

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        width = len(records[0].vector)
        if any(len(record.vector) != width for record in records):
            raise ValueError("all vector records in a batch must have the same dimension")
        if self.dims and width != self.dims:
            raise ValueError(f"vector dimension mismatch: expected {self.dims}, got {width}")
        self.dims = width
        self._ensure_collection(width)
        models = self._require_models()
        points = [
            models.PointStruct(
                id=self._point_id(record.id), vector=record.vector, payload=self._payload(record)
            )
            for record in records
        ]
        self.client.upsert(collection_name=self.collection_name, points=points)

    def _filter(self, container_tag: str, org_id: str | None) -> Any:
        models = self._require_models()
        must = [
            models.FieldCondition(key="container_tag", match=models.MatchValue(value=container_tag))
        ]
        if org_id is not None:
            must.append(models.FieldCondition(key="org_id", match=models.MatchValue(value=org_id)))
        return models.Filter(must=must)

    def _hit(self, point: Any) -> VectorHit | None:
        payload = self._field(point, "payload") or {}
        record_id = payload.get("record_id") if isinstance(payload, dict) else None
        if not record_id:
            return None
        metadata_json = payload.get("metadata_json") or "{}"
        try:
            metadata = json.loads(metadata_json)
        except (TypeError, ValueError):
            metadata = {}
        return VectorHit(
            id=str(record_id),
            score=float(self._field(point, "score", 0.0) or 0.0),
            text=str(payload.get("text", "")),
            kind=str(payload.get("kind", "")),
            container_tag=str(payload.get("container_tag", "")),
            org_id=payload.get("org_id"),
            metadata=metadata if isinstance(metadata, dict) else {},
            created_at=float(payload.get("created_at", 0.0) or 0.0),
        )

    def query(
        self,
        vector: Sequence[float],
        *,
        container_tag: str,
        org_id: str | None = None,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        if not self._collection_ready:
            return []
        query_filter = self._filter(container_tag, org_id)
        kwargs = {
            "collection_name": self.collection_name,
            "query_vector": list(vector),
            "query_filter": query_filter,
            "limit": max(0, limit),
            "with_payload": True,
        }
        if hasattr(self.client, "query_points"):
            response = self.client.query_points(**kwargs)
        else:
            response = self.client.search(**kwargs)
        points = self._field(response, "points", response)
        if not isinstance(points, list):
            return []
        hits = [hit for point in points if (hit := self._hit(point)) is not None]
        if filters:
            hits = [hit for hit in hits if _matches(hit.metadata, filters)]
        return hits[: max(0, limit)]

    def delete(
        self,
        *,
        ids: Sequence[str] | None = None,
        container_tag: str | None = None,
        org_id: str | None = None,
    ) -> int:
        if not self._collection_ready:
            return 0
        models = self._require_models()
        if ids is not None:
            point_ids = [self._point_id(record_id) for record_id in ids]
            if not point_ids:
                return 0
            selector = (
                models.PointIdsList(points=point_ids)
                if hasattr(models, "PointIdsList")
                else point_ids
            )
        elif container_tag is not None:
            selector = (
                models.FilterSelector(filter=self._filter(container_tag, org_id))
                if hasattr(models, "FilterSelector")
                else self._filter(container_tag, org_id)
            )
        else:
            raise ValueError("delete requires ids or a container tag")
        try:
            self.client.delete(
                collection_name=self.collection_name, points_selector=selector, wait=True
            )
        except TypeError:
            self.client.delete(collection_name=self.collection_name, points=selector, wait=True)
        return len(ids) if ids is not None else 0

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            close()


class ChromaVectorStore:
    """Chroma adapter using explicit embeddings and metadata scopes."""

    name = "chroma"

    def __init__(
        self,
        *,
        client: Any | None = None,
        collection_name: str = "memoratum",
        endpoint: str = "",
        path: str = "",
        api_key: str = "",
    ) -> None:
        self.client = client if client is not None else self._load_client(endpoint, path, api_key)
        self.collection_name = collection_name
        self.collection: Any | None = None

    def _ensure_collection(self) -> Any:
        if self.collection is None:
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=None,
                metadata={"hnsw:space": "cosine"},
            )
        return self.collection

    @staticmethod
    def _load_client(endpoint: str, path: str, api_key: str) -> Any:
        try:
            import chromadb
        except ImportError as exc:
            raise ProviderUnavailable(
                "the chromadb package is not installed; install the chroma extra"
            ) from exc
        if path:
            return chromadb.PersistentClient(path=path)
        if endpoint:
            parsed = urlparse(endpoint)
            return chromadb.HttpClient(
                host=parsed.hostname or "localhost",
                port=parsed.port or 8000,
                ssl=parsed.scheme == "https",
                headers={"X-Chroma-Token": api_key} if api_key else None,
            )
        return chromadb.Client()

    def _metadata(self, record: VectorRecord) -> dict[str, Any]:
        return {
            "record_id": record.id,
            "kind": record.kind,
            "container_tag": record.container_tag,
            "org_id": record.org_id or "",
            "metadata_json": json.dumps(record.metadata or {}, separators=(",", ":")),
        }

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        collection = self._ensure_collection()
        collection.upsert(
            ids=[record.id for record in records],
            embeddings=[record.vector for record in records],
            documents=[record.text for record in records],
            metadatas=[self._metadata(record) for record in records],
        )

    def _where(self, container_tag: str, org_id: str | None) -> dict[str, Any]:
        if org_id is None:
            return {"container_tag": container_tag}
        return {"$and": [{"container_tag": container_tag}, {"org_id": org_id}]}

    @staticmethod
    def _column(result: dict[str, Any], name: str, index: int, default: Any) -> Any:
        values = result.get(name)
        return values[index] if isinstance(values, list) and len(values) > index else default

    def query(
        self,
        vector: Sequence[float],
        *,
        container_tag: str,
        org_id: str | None = None,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        result = self._ensure_collection().query(
            query_embeddings=[list(vector)],
            n_results=max(0, limit),
            where=self._where(container_tag, org_id),
            include=["documents", "metadatas", "distances"],
        )
        ids = self._column(result, "ids", 0, [])
        documents = self._column(result, "documents", 0, [])
        metadatas = self._column(result, "metadatas", 0, [])
        distances = self._column(result, "distances", 0, [])
        hits = []
        for index, record_id in enumerate(ids):
            metadata = metadatas[index] if index < len(metadatas) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            try:
                original_metadata = json.loads(metadata.get("metadata_json") or "{}")
            except (TypeError, ValueError):
                original_metadata = {}
            if not isinstance(original_metadata, dict):
                original_metadata = {}
            distance = float(distances[index]) if index < len(distances) else 1.0
            hits.append(
                VectorHit(
                    id=str(record_id),
                    score=1.0 - distance,
                    text=str(documents[index]) if index < len(documents) else "",
                    kind=str(metadata.get("kind", "")),
                    container_tag=str(metadata.get("container_tag", "")),
                    org_id=metadata.get("org_id") or None,
                    metadata=original_metadata,
                    created_at=0.0,
                )
            )
        if filters:
            hits = [hit for hit in hits if _matches(hit.metadata, filters)]
        return hits[: max(0, limit)]

    def delete(
        self,
        *,
        ids: Sequence[str] | None = None,
        container_tag: str | None = None,
        org_id: str | None = None,
    ) -> int:
        collection = self._ensure_collection()
        if ids is not None:
            if not ids:
                return 0
            collection.delete(ids=list(ids))
            return len(ids)
        if container_tag is None:
            raise ValueError("delete requires ids or a container tag")
        collection.delete(where=self._where(container_tag, org_id))
        return 0

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            close()


def build_vector_store(
    provider: str,
    *,
    conn: sqlite3.Connection | None = None,
    endpoint: str = "",
    path: str = "",
    api_key: str = "",
    collection_name: str = "memoratum",
    dims: int = 0,
    client: Any | None = None,
    models: Any | None = None,
) -> VectorStore:
    """Build a local or optional vector-store implementation."""
    normalized = provider.strip().lower()
    if normalized in {"", "sqlite", "local"}:
        if conn is None:
            raise ValueError("SQLite vector store requires a database connection")
        return SQLiteVectorStore(conn)
    if normalized in {"memory", "in-memory", "fake"}:
        return InMemoryVectorStore()
    if normalized == "qdrant":
        return QdrantVectorStore(
            client=client,
            models=models,
            url=endpoint,
            api_key=api_key,
            collection_name=collection_name,
            dims=dims,
        )
    if normalized == "chroma":
        return ChromaVectorStore(
            client=client,
            collection_name=collection_name,
            endpoint=endpoint,
            path=path,
            api_key=api_key,
        )
    raise ValueError(f"unknown vector store: {provider}")
