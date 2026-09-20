# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Hybrid retrieval: cosine vector leg + keyword leg, fused with RRF.

Candidate types: chunks (documents mode) and facts rendered as
"subject predicate object" (memories mode); hybrid searches both.
Phase 2 ceiling: fact embeddings computed per search (batched, one call);
brute-force cosine (upgrade path is sqlite-vec vec0). container_tag filter
is mandatory.
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
import time
from typing import Any

from memoratum import db
from memoratum.embeddings import Embedder
from memoratum.facts import list_facts

_RRF_K = 60


def pack_vector(vec: list[float]) -> bytes:
    import struct

    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _fact_text(fact: dict[str, Any]) -> str:
    return f"{fact['subject']} {fact['predicate']} {fact['object']}"


def _token_overlap(query: str, text: str) -> float:
    qtokens = {t.lower() for t in query.split()}
    if not qtokens:
        return 0.0
    ttokens = {t.lower() for t in text.split()}
    return len(qtokens & ttokens) / len(qtokens)


def search(
    conn: sqlite3.Connection,
    embedder: Embedder,
    query: str,
    *,
    container_tag: str,
    limit: int = 10,
    threshold: float = 0.0,
    keyword_limit: int = 50,
    search_mode: str = "hybrid",
    filters: dict[str, Any] | None = None,
    rerank: bool = False,
) -> list[dict[str, Any]]:
    want_chunks = search_mode in ("hybrid", "documents")
    want_facts = search_mode in ("hybrid", "memories")

    def _matches(meta: dict[str, Any]) -> bool:
        return not filters or all(meta.get(k) == v for k, v in filters.items())

    texts: dict[str, str] = {}
    kinds: dict[str, str] = {}
    stamped: dict[str, float] = {}
    if want_chunks:
        rows = conn.execute(
            "SELECT c.id, c.text, c.embedding, c.created_at, d.metadata FROM chunks c"
            " JOIN documents d ON d.id = c.document_id WHERE d.container_tag = ?",
            (container_tag,),
        ).fetchall()
        for r in rows:
            if not _matches(json.loads(r["metadata"] or "{}")):
                continue
            key = f"chunk_{r['id']}"
            texts[key] = r["text"]
            kinds[key] = "chunk"
            stamped[key] = r["created_at"]
    fact_list: list[dict[str, Any]] = []
    if want_facts:
        fact_list = [f for f in list_facts(conn, container_tag) if _matches(f["metadata"])]
        for f in fact_list:
            key = f"mem_{f['id']}"
            texts[key] = _fact_text(f)
            kinds[key] = "memory"
            stamped[key] = f["created_at"]

    scores: dict[str, float] = {}
    vec_items: list[tuple[str, list[float]]] = []
    chunk_keys = [k for k in texts if kinds[k] == "chunk"]
    if chunk_keys:
        chunk_embs = embedder.embed([texts[k] for k in chunk_keys])
        vec_items.extend(zip(chunk_keys, chunk_embs, strict=True))
    if want_facts and fact_list:
        missing = [f for f in fact_list if f.get("embedding") is None]
        for i in range(0, len(missing), 512):
            window = missing[i : i + 512]
            vecs = embedder.embed([_fact_text(f) for f in window])
            for f, vec in zip(window, vecs, strict=True):
                blob = pack_vector(vec)
                conn.execute("UPDATE facts SET embedding = ? WHERE id = ?", (blob, f["id"]))
                f["embedding"] = blob
            conn.commit()
        for f in fact_list:
            if f.get("embedding") is not None:
                vec_items.append((f"mem_{f['id']}", _unpack(bytes(f["embedding"]))))
    if texts:
        qvec = embedder.embed([query])[0]
        vec_ranked = sorted(
            ((key, _cosine(qvec, emb)) for key, emb in vec_items),
            key=lambda t: t[1],
            reverse=True,
        )
        for rank, (key, _sim) in enumerate(vec_ranked, start=1):
            scores[key] = scores.get(key, 0.0) + 0.6 / (_RRF_K + rank)

    kw_ranked: list[tuple[str, float]] = []
    if want_chunks:
        for r in db.keyword_search(conn, query, container_tag=container_tag, limit=keyword_limit):
            kw_ranked.append((f"chunk_{r['id']}", 1.0))
    if want_facts:
        scored = sorted(
            ((f"mem_{f['id']}", _token_overlap(query, _fact_text(f))) for f in fact_list),
            key=lambda t: t[1],
            reverse=True,
        )
        kw_ranked.extend([(key, s) for key, s in scored[:keyword_limit] if s > 0])
    for rank, (key, _s) in enumerate(
        sorted(kw_ranked, key=lambda t: t[1], reverse=True)[:keyword_limit], start=1
    ):
        if key in texts:
            scores[key] = scores.get(key, 0.0) + 0.4 / (_RRF_K + rank)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if rerank:
        now = time.time()
        rescored = []
        for key, score in ranked[: max(limit * 3, limit)]:
            age_days = max(0.0, now - stamped.get(key, now)) / 86400.0
            recency = 1.0 / (1.0 + age_days)
            rescored.append((key, 0.7 * score + 0.3 * recency))
        ranked = sorted(rescored, key=lambda kv: kv[1], reverse=True)

    hits = []
    for key, score in ranked:
        if score < threshold:
            continue
        if kinds[key] == "memory":
            hits.append({"id": key, "memory": texts[key], "similarity": round(score, 6)})
        else:
            hits.append({"id": key, "chunk": texts[key], "similarity": round(score, 6)})
    return hits[:limit]
