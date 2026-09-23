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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


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


def build_vector_store(provider: str, *, conn: sqlite3.Connection | None) -> VectorStore:
    """Build a local vector-store implementation without optional dependencies."""
    normalized = provider.strip().lower()
    if normalized in {"", "sqlite", "local"}:
        if conn is None:
            raise ValueError("SQLite vector store requires a database connection")
        return SQLiteVectorStore(conn)
    if normalized in {"memory", "in-memory", "fake"}:
        return InMemoryVectorStore()
    raise ValueError(f"unknown vector store: {provider}")
