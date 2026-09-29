"""Rebuild provider-neutral vector records from authoritative SQLite data.

This is an operator command for upgrades from databases created before project
scoping existed. It upserts the same deterministic record IDs, so Qdrant,
Chroma, and pgvector payloads gain the current org/project scope without
creating duplicates. Deleted and expired records are removed by ID.
"""

from __future__ import annotations

import argparse
import json
import struct
import time

from memoratum import db
from memoratum.config import Settings
from memoratum.embeddings import build_embedder
from memoratum.vectorstore import VectorRecord, build_vector_store


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def rebuild(*, batch_size: int = 128) -> dict[str, int]:
    settings = Settings.load()
    conn = db.connect(settings.db_path)
    embedder = build_embedder(
        settings.embeddings_provider,
        endpoint=settings.embeddings_endpoint,
        model=settings.embeddings_model,
        api_key="",
        dims=settings.embeddings_dims,
    )
    store = build_vector_store(
        settings.vector_store,
        conn=conn,
        endpoint=settings.vector_store_endpoint,
        path=settings.vector_store_path,
        api_key="",
        collection_name=settings.vector_store_collection,
        dims=settings.vector_store_dims or settings.embeddings_dims,
    )
    records: list[VectorRecord] = []
    deleted_ids: list[str] = []
    try:
        now = time.time()
        memories = db.list_all_memories(conn, include_deleted=True, show_expired=True)
        active_memories = []
        for memory in memories:
            if memory["state"] == "deleted" or (
                memory.get("expires_at") is not None and float(memory["expires_at"]) <= now
            ):
                deleted_ids.append(str(memory["id"]))
            else:
                active_memories.append(memory)
        if active_memories:
            vectors = embedder.embed([memory["text"] for memory in active_memories])
            records.extend(
                VectorRecord(
                    id=str(memory["id"]),
                    vector=vector,
                    text=memory["text"],
                    kind="memory",
                    container_tag=memory["container_tag"],
                    org_id=memory.get("org_id"),
                    project_id=memory.get("project_id"),
                    metadata=memory.get("metadata") or {},
                    created_at=memory.get("created_at") or now,
                )
                for memory, vector in zip(active_memories, vectors, strict=True)
            )
        chunks = [
            dict(row)
            for row in conn.execute(
                "SELECT c.id, c.text, c.embedding, c.created_at, c.document_id, d.container_tag, d.org_id,"
                " d.project_id, d.metadata, d.expires_at, d.status"
                " FROM chunks c JOIN documents d ON d.id = c.document_id"
            ).fetchall()
        ]
        active_chunks = []
        for chunk in chunks:
            if chunk["status"] == "failed" or (
                chunk["expires_at"] is not None and float(chunk["expires_at"]) <= now
            ):
                deleted_ids.append(str(chunk["id"]))
            else:
                active_chunks.append(chunk)
        if active_chunks:
            missing = [chunk for chunk in active_chunks if chunk["embedding"] is None]
            if missing:
                vectors = embedder.embed([chunk["text"] for chunk in missing])
                for chunk, vector in zip(missing, vectors, strict=True):
                    chunk["embedding"] = struct.pack(f"{len(vector)}f", *vector)
            for chunk in active_chunks:
                vector = _unpack(bytes(chunk["embedding"]))
                metadata = json.loads(chunk["metadata"] or "{}")
                metadata["document_id"] = str(chunk["document_id"])
                records.append(
                    VectorRecord(
                        id=str(chunk["id"]),
                        vector=vector,
                        text=chunk["text"],
                        kind="chunk",
                        container_tag=chunk["container_tag"],
                        org_id=chunk["org_id"],
                        project_id=chunk["project_id"],
                        metadata=metadata,
                        created_at=chunk["created_at"] or now,
                    )
                )
        for start in range(0, len(records), batch_size):
            store.upsert(records[start : start + batch_size])
        if deleted_ids:
            store.delete(ids=deleted_ids)
        return {"upserted": len(records), "deleted": len(deleted_ids)}
    finally:
        store.close()
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=128)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 2000:
        parser.error("--batch-size must be between 1 and 2000")
    result = rebuild(batch_size=args.batch_size)
    print(f"reindexed {result['upserted']} records; removed {result['deleted']} stale records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
