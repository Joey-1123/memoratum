# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Hybrid retrieval: cosine vector leg + FTS5 keyword leg, fused with RRF.

Phase 1 ceiling: brute-force cosine over stored chunk embeddings (fine at small
scale; upgrade path is sqlite-vec vec0). container_tag filter is mandatory.
"""

from __future__ import annotations

import math
import sqlite3
import struct
from typing import Any

from memoratum import db
from memoratum.embeddings import Embedder

_RRF_K = 60


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def search(
    conn: sqlite3.Connection,
    embedder: Embedder,
    query: str,
    *,
    container_tag: str,
    limit: int = 10,
    threshold: float = 0.0,
    keyword_limit: int = 50,
) -> list[dict[str, Any]]:
    qvec = embedder.embed([query])[0]

    rows = conn.execute(
        "SELECT c.id, c.text, c.embedding FROM chunks c JOIN documents d ON d.id = c.document_id"
        " WHERE d.container_tag = ?",
        (container_tag,),
    ).fetchall()
    vec_ranked = sorted(
        (
            (r["id"], r["text"], _cosine(qvec, _unpack(r["embedding"])))
            for r in rows
            if r["embedding"] is not None
        ),
        key=lambda t: t[2],
        reverse=True,
    )

    kw_ranked = [
        (r["id"], r["text"])
        for r in db.keyword_search(conn, query, container_tag=container_tag, limit=keyword_limit)
    ]

    scores: dict[int, float] = {}
    texts: dict[int, str] = {}
    for rank, (cid, text, _sim) in enumerate(vec_ranked, start=1):
        scores[cid] = scores.get(cid, 0.0) + 0.6 / (_RRF_K + rank)
        texts[cid] = text
    for rank, (cid, text) in enumerate(kw_ranked, start=1):
        scores[cid] = scores.get(cid, 0.0) + 0.4 / (_RRF_K + rank)
        texts[cid] = text

    hits = [
        {"id": f"chunk_{cid}", "chunk": texts[cid], "similarity": round(score, 6)}
        for cid, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        if score >= threshold
    ]
    return hits[:limit]
