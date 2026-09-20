# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LongMemEval-S retrieval harness (developer tooling, not part of the server).

Ingests each question's haystack sessions as documents, runs search per mode,
and reports session-level partial-R@k / full-R@k / MRR.

Usage:
    uv run python -m memoratum.eval_longmemeval --data data/longmemeval_s_cleaned.json \\
        --n 20 --seed 42 --k 5,10 --out-md eval/RESULTS.md

Embeddings: HashEmbedder by default (pipeline-correctness signal only — it has
no semantics, so absolute numbers understate a real embedder). Set
MEMORATUM_EVAL_API=1 with MEMORATUM_EMBEDDINGS_* for API embeddings.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import tempfile
import time
from typing import Any

from memoratum import db
from memoratum.embeddings import ApiEmbedder, Embedder, HashEmbedder
from memoratum.eval_metrics import full_recall, mrr, partial_recall
from memoratum.ingest import process_all
from memoratum.search import search


def format_session(turns: list[dict[str, Any]]) -> str:
    return "\n".join(f"{t.get('role', '?')}: {t.get('content', '')}" for t in turns)


def build_embedder() -> Embedder:
    if os.environ.get("MEMORATUM_EVAL_API") == "1":
        from memoratum.config import Settings

        s = Settings.load()
        return ApiEmbedder(
            endpoint=s.embeddings_endpoint,
            model=s.embeddings_model,
            api_key=os.environ.get("MEMORATUM_EMBEDDINGS_KEY", ""),
        )
    return HashEmbedder(dims=64)


def hit_session(conn: sqlite3.Connection, hit: dict[str, Any]) -> str | None:
    """Map a search hit back to its haystack session id (chunks only)."""
    hid = hit.get("id", "")
    if not hid.startswith("chunk_"):
        return None
    row = conn.execute(
        "SELECT d.custom_id FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id = ?",
        (int(hid.split("_", 1)[1]),),
    ).fetchone()
    return row["custom_id"] if row else None


def evaluate(
    data: list[dict[str, Any]], *, n: int, seed: int, ks: list[int], modes: list[str]
) -> dict[str, Any]:
    rng = random.Random(seed)
    picked = rng.sample(data, min(n, len(data)))
    embedder = build_embedder()
    out: dict[str, Any] = {"questions": [], "modes": modes, "ks": ks, "n": len(picked)}
    for q in picked:
        conn = db.connect(os.path.join(tempfile.mkdtemp(), "eval.db"))
        for sid, session in zip(q["haystack_session_ids"], q["haystack_sessions"], strict=True):
            db.create_document(
                conn, container_tag="bench", content=format_session(session), custom_id=sid
            )
        process_all(conn, embedder)
        gold = set(q["answer_session_ids"])
        row: dict[str, Any] = {"id": q["question_id"], "type": q["question_type"], "modes": {}}
        for mode in modes:
            hits = search(
                conn,
                embedder,
                q["question"],
                container_tag="bench",
                limit=max(ks) * 2,
                search_mode=mode,
            )
            ranked = [s for h in hits if (s := hit_session(conn, h)) is not None]
            row["modes"][mode] = (
                {f"partial-R@{k}": partial_recall(ranked, gold, k=k) for k in ks}
                | {f"full-R@{k}": full_recall(ranked, gold, k=k) for k in ks}
                | {"MRR": mrr(ranked, gold)}
            )
        out["questions"].append(row)
        conn.close()
    return out


def summarize(result: dict[str, Any]) -> str:
    lines = [f"# LongMemEval-S retrieval — n={result['n']}", ""]
    for mode in result["modes"]:
        lines.append(f"## {mode}")
        for k in result["ks"]:
            p = sum(q["modes"][mode][f"partial-R@{k}"] for q in result["questions"]) / result["n"]
            f = sum(q["modes"][mode][f"full-R@{k}"] for q in result["questions"]) / result["n"]
            lines.append(f"- partial-R@{k}: {p:.3f} | full-R@{k}: {f:.3f}")
        m = sum(q["modes"][mode]["MRR"] for q in result["questions"]) / result["n"]
        lines.append(f"- MRR: {m:.3f}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--k", default="5,10")
    ap.add_argument("--modes", default="hybrid,documents")
    ap.add_argument("--out-md", default="")
    args = ap.parse_args()
    with open(args.data) as f:
        data = json.load(f)
    ks = [int(x) for x in args.k.split(",")]
    started = time.time()
    result = evaluate(data, n=args.n, seed=args.seed, ks=ks, modes=args.modes.split(","))
    result["seconds"] = round(time.time() - started, 1)
    report = summarize(result) + f"\ntook {result['seconds']}s\n"
    print(report)
    if args.out_md:
        os.makedirs(os.path.dirname(args.out_md) or ".", exist_ok=True)
        with open(args.out_md, "w") as f:
            f.write(report)


if __name__ == "__main__":
    main()
