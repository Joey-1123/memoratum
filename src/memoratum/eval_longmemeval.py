# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LongMemEval-S retrieval harness (developer tooling, not part of the server).

Ingests each question's haystack sessions as documents, runs search per mode,
and reports session-level partial-R@k / full-R@k / MRR. The runner accepts the
common JSON/JSONL exports, records a reproducible manifest, and can evaluate a
complete dataset with ``--full``.

Usage:
    uv run python -m memoratum.eval_longmemeval --data data/longmemeval_s_cleaned.json \\
        --n 20 --seed 42 --k 5,10 --out-md eval/RESULTS.md

Embeddings default to the configured provider. Set ``MEMORATUM_EVAL_API=1`` to
force the OpenAI-compatible API adapter for a benchmark run.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from memoratum import db
from memoratum.embeddings import ApiEmbedder, Embedder
from memoratum.embeddings import build_embedder as build_provider_embedder
from memoratum.eval_datasets import (
    file_sha256,
    load_records,
    normalize_longmemeval,
    records_sha256,
    sample_records,
)
from memoratum.eval_metrics import full_recall, mrr, partial_recall
from memoratum.ingest import process_all
from memoratum.search import search
from memoratum.vectorstore import SQLiteVectorStore, VectorStore


def format_session(turns: list[dict[str, Any]]) -> str:
    lines = []
    for turn in turns:
        role = str(turn.get("role", turn.get("speaker", "?")))
        content = turn.get("content", turn.get("text", ""))
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def build_embedder() -> Embedder:
    from memoratum.config import Settings

    settings = Settings.load()
    if os.environ.get("MEMORATUM_EVAL_API") == "1":
        return ApiEmbedder(
            endpoint=settings.embeddings_endpoint,
            model=settings.embeddings_model,
            api_key=os.environ.get("MEMORATUM_EMBEDDINGS_KEY", ""),
            dims=settings.embeddings_dims,
        )
    return build_provider_embedder(
        settings.embeddings_provider,
        endpoint=settings.embeddings_endpoint,
        model=settings.embeddings_model,
        api_key=os.environ.get("MEMORATUM_EMBEDDINGS_KEY", ""),
        dims=settings.embeddings_dims,
        timeout=30.0,
    )


def hit_session(conn: sqlite3.Connection, hit: dict[str, Any]) -> str | None:
    """Map a search hit back to its haystack session id (chunks only)."""
    hit_id = str(hit.get("id", ""))
    if not hit_id.startswith("chunk_"):
        return None
    try:
        chunk_id = int(hit_id.split("_", 1)[1])
    except (TypeError, ValueError):
        return None
    row = conn.execute(
        "SELECT d.custom_id FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id = ?",
        (chunk_id,),
    ).fetchone()
    return str(row["custom_id"]) if row and row["custom_id"] is not None else None


def _embedder_label(embedder: Embedder) -> str:
    return f"{type(embedder).__name__}:{getattr(embedder, 'dims', 0)}"


def _aggregate(result: dict[str, Any]) -> dict[str, dict[str, float]]:
    n = result["n"]
    if n == 0:
        return {mode: {} for mode in result["modes"]}
    aggregate: dict[str, dict[str, float]] = {}
    for mode in result["modes"]:
        metrics: dict[str, float] = {}
        for k in result["ks"]:
            for prefix in ("partial-R", "full-R"):
                key = f"{prefix}@{k}"
                metrics[key] = sum(q["modes"][mode][key] for q in result["questions"]) / n
        metrics["MRR"] = sum(q["modes"][mode]["MRR"] for q in result["questions"]) / n
        aggregate[mode] = metrics
    return aggregate


def evaluate(
    data: list[dict[str, Any]],
    *,
    n: int,
    seed: int,
    ks: list[int],
    modes: list[str],
    embedder: Embedder | None = None,
    vector_store_factory: Callable[[sqlite3.Connection], VectorStore] | None = None,
    dataset_name: str = "longmemeval-s",
    dataset_hash: str = "",
) -> dict[str, Any]:
    """Evaluate a normalized or raw LongMemEval-S record collection."""
    if not ks or any(k <= 0 for k in ks):
        raise ValueError("ks must contain positive recall depths")
    normalized = normalize_longmemeval(data)
    picked = sample_records(normalized, n=n, seed=seed)
    embedder = embedder or build_embedder()
    factory = vector_store_factory or (lambda conn: SQLiteVectorStore(conn))
    out: dict[str, Any] = {
        "dataset": dataset_name,
        "manifest": {
            "schema": "longmemeval-normalized-v1",
            "seed": seed,
            "requested_n": n,
            "n": len(picked),
            "ks": ks,
            "modes": modes,
            "embedder": _embedder_label(embedder),
            "data_sha256": dataset_hash or records_sha256(picked),
        },
        "questions": [],
        "modes": modes,
        "ks": ks,
        "n": len(picked),
    }
    for question in picked:
        with tempfile.TemporaryDirectory(prefix="memoratum-eval-") as directory:
            conn = db.connect(os.path.join(directory, "eval.db"))
            vector_store = factory(conn)
            try:
                for session in question["sessions"]:
                    db.create_document(
                        conn,
                        container_tag="bench",
                        content=format_session(session["turns"]),
                        custom_id=session["session_id"],
                    )
                process_all(conn, embedder, vector_store=vector_store)
                gold = set(question["answer_session_ids"])
                row: dict[str, Any] = {
                    "id": question["question_id"],
                    "type": question["question_type"],
                    "modes": {},
                }
                for mode in modes:
                    hits = search(
                        conn,
                        embedder,
                        question["question"],
                        container_tag="bench",
                        limit=max(max(ks) * 2, 10),
                        search_mode=mode,
                        vector_store=vector_store,
                    )
                    ranked = [
                        session for hit in hits if (session := hit_session(conn, hit)) is not None
                    ]
                    row["modes"][mode] = {
                        **{f"partial-R@{k}": partial_recall(ranked, gold, k=k) for k in ks},
                        **{f"full-R@{k}": full_recall(ranked, gold, k=k) for k in ks},
                        "MRR": mrr(ranked, gold),
                        "ranked_sessions": ranked,
                    }
                out["questions"].append(row)
            finally:
                conn.close()
    out["aggregate"] = _aggregate(out)
    return out


def summarize(result: dict[str, Any]) -> str:
    aggregate = result.get("aggregate") or _aggregate(result)
    lines = [
        f"# LongMemEval-S retrieval — n={result['n']}",
        f"dataset: {result.get('dataset', 'longmemeval-s')}",
        "",
    ]
    for mode in result["modes"]:
        lines.append(f"## {mode}")
        metrics = aggregate[mode]
        for k in result["ks"]:
            lines.append(
                f"- partial-R@{k}: {metrics.get(f'partial-R@{k}', 0.0):.3f}"
                f" | full-R@{k}: {metrics.get(f'full-R@{k}', 0.0):.3f}"
            )
        lines.append(f"- MRR: {metrics.get('MRR', 0.0):.3f}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--k", default="5,10")
    parser.add_argument("--modes", default="hybrid,documents")
    parser.add_argument("--out-md", default="")
    parser.add_argument("--out-json", default="")
    args = parser.parse_args()

    records = load_records(args.data)
    ks = [int(value) for value in args.k.split(",") if value.strip()]
    modes = [value.strip() for value in args.modes.split(",") if value.strip()]
    started = time.time()
    result = evaluate(
        records,
        n=0 if args.full else args.n,
        seed=args.seed,
        ks=ks,
        modes=modes,
        dataset_hash=file_sha256(args.data),
    )
    result["seconds"] = round(time.time() - started, 1)
    report = summarize(result) + f"\ntook {result['seconds']}s\n"
    print(report)
    if args.out_md:
        os.makedirs(os.path.dirname(args.out_md) or ".", exist_ok=True)
        Path(args.out_md).write_text(report)
    if args.out_json:
        os.makedirs(os.path.dirname(args.out_json) or ".", exist_ok=True)
        Path(args.out_json).write_text(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
