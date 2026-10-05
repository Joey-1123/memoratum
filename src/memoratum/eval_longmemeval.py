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
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from memoratum import db
from memoratum.embeddings import ApiEmbedder, Embedder
from memoratum.embeddings import build_embedder as build_provider_embedder
from memoratum.eval_axes import (
    EXIT_BAD_INPUT,
    EXIT_GATE_FAILED,
    EXIT_OK,
    retrieval_budget,
)
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


def _project_ids(count: int) -> list[str]:
    """Project ids for a multi-project ingestion, first one being the query target.

    Every id is a real string, never ``None``. That matters: every scope filter in this
    codebase is conditional on ``is not None``, so a ``None`` project is *unscoped*, and
    naming the query's own project ``None`` would make the "scoped" query return every
    project's data and report a leak in a correct implementation.

    Measured while building this: doing exactly that produced ``leaked_hits == 10`` on a
    corpus with zero scope defects, because the scoped query was silently unscoped.
    """
    return [f"proj-{i}" for i in range(count)]


def _scope_from(value: str) -> tuple[bool, bool]:
    """``scoped`` | ``unscoped`` | ``both`` -> (issue scoped queries, issue unscoped)."""
    scopes = {item.strip() for item in value.split(",") if item.strip()}
    if not scopes:
        raise ValueError("scope must be one of: scoped, unscoped, both")
    unknown = scopes - {"scoped", "unscoped", "both"}
    if unknown:
        raise ValueError(
            f"unknown scope {sorted(unknown)}; expected one of: scoped, unscoped, both"
        )
    if "both" in scopes:
        return True, True
    return "scoped" in scopes, "unscoped" in scopes


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
    project_count: int = 1,
    scope: str = "scoped",
) -> dict[str, Any]:
    """Evaluate a normalized or raw LongMemEval-S record collection.

    ``project_count > 1`` ingests each session into every project, so the isolation axis can
    be evaluated against a real dataset rather than a synthetic probe: a single-project
    corpus reports perfect recall even when the store has stopped enforcing scope, because
    recall is monotonically indifferent to where data landed (research.md D1).

    ``scope="both"`` issues each question twice — once scoped to project 0, once with no
    scope at all — and records both buckets. The unscoped queries are excluded from the leak
    gate (invariant I2): ``project_id=null`` genuinely means "no scope requested", so
    counting their cross-project hits as violations would report a false failure.
    """
    if not ks or any(k <= 0 for k in ks):
        raise ValueError("ks must contain positive recall depths")
    if isinstance(project_count, bool) or not isinstance(project_count, int):
        raise TypeError(f"project_count must be an int, got {project_count!r}")
    if project_count < 1:
        raise ValueError(
            f"project_count={project_count} ingests nothing; a run that writes no data must "
            "not report metrics for it"
        )
    run_scoped, run_unscoped = _scope_from(scope)
    if not run_scoped and project_count < 2:
        raise ValueError(
            "scope=unscoped with project_count=1 has nothing to demonstrate: every document "
            "is already in the NULL scope"
        )

    normalized = normalize_longmemeval(data)
    picked = sample_records(normalized, n=n, seed=seed)
    embedder = embedder or build_embedder()
    factory = vector_store_factory or (lambda conn: SQLiteVectorStore(conn))
    store_label = (
        "sqlite"
        if vector_store_factory is None
        else getattr(vector_store_factory, "__name__", type(vector_store_factory).__name__)
    )
    projects = _project_ids(project_count)
    scoped_project = projects[0]

    scope_config: dict[str, Any] = {
        "project_count": project_count,
        "scope": scope,
        "project_ids": projects,
        "scoped_queries": 0,
        "unscoped_queries": 0,
        "unscoped_cross_scope_hits": 0,
        "leaked_hits": 0,
    }

    out: dict[str, Any] = {
        "dataset": dataset_name,
        "manifest": {
            "schema": "longmemeval-scoped-v2",
            "seed": seed,
            "requested_n": n,
            "n": len(picked),
            "ks": ks,
            "modes": modes,
            "embedder": _embedder_label(embedder),
            "vector_store": store_label,
            "data_sha256": dataset_hash or records_sha256(picked),
            "scope_config": scope_config,
            "baseline_sources": {},
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
                documents = 0
                for session in question["sessions"]:
                    for project in projects:
                        db.create_document(
                            conn,
                            container_tag="bench",
                            content=format_session(session["turns"]),
                            custom_id=f"{project}::{session['session_id']}",
                            project_id=project,
                        )
                        documents += 1
                scope_config["documents_per_question"] = documents
                process_all(conn, embedder, vector_store=vector_store)
                gold = {f"{scoped_project}::{sid}" for sid in question["answer_session_ids"]}
                gold_sessions = set(question["answer_session_ids"])

                row: dict[str, Any] = {
                    "id": question["question_id"],
                    "type": question["question_type"],
                    "modes": {},
                    "scope": {},
                }
                queries: list[tuple[str, str | None]] = []
                if run_scoped:
                    queries.append(("scoped", scoped_project))
                if run_unscoped:
                    queries.append(("unscoped", None))

                for mode in modes:
                    row["modes"][mode] = {}
                    for bucket, project in queries:
                        hits = search(
                            conn,
                            embedder,
                            question["question"],
                            container_tag="bench",
                            project_id=project,
                            limit=retrieval_budget(ks),
                            search_mode=mode,
                            vector_store=vector_store,
                        )
                        ranked = [
                            session
                            for hit in hits
                            if (session := hit_session(conn, hit)) is not None
                        ]
                        if bucket == "scoped":
                            scope_config["scoped_queries"] += 1
                            # A leak is a hit from ANOTHER project, not any non-gold hit.
                            # Gold is one session; the other sessions in the same project
                            # are legitimate results, and counting them would report a
                            # perfect implementation as leaking on every question.
                            leaked = sum(
                                1 for item in ranked if _project_of(item) != scoped_project
                            )
                            scope_config["leaked_hits"] += leaked
                            metrics = {
                                **{f"partial-R@{k}": partial_recall(ranked, gold, k=k) for k in ks},
                                **{f"full-R@{k}": full_recall(ranked, gold, k=k) for k in ks},
                                "MRR": mrr(ranked, gold),
                                # Kept: an operator debugging a recall figure needs to see
                                # which sessions were retrieved, in order.
                                "ranked_sessions": ranked,
                            }
                            row["modes"][mode].update(metrics)
                        else:
                            scope_config["unscoped_queries"] += 1
                            scope_config["unscoped_cross_scope_hits"] += sum(
                                1 for item in ranked if _project_of(item) != scoped_project
                            )
                            # Session ids, not the project-prefixed document ids: the gold
                            # answer is a session, and every project's copy of it counts.
                            unscoped_sessions = [_session_of(item) for item in ranked]
                            row["scope"][f"{mode}:unscoped"] = {
                                **{
                                    f"partial-R@{k}": partial_recall(
                                        unscoped_sessions, gold_sessions, k=k
                                    )
                                    for k in ks
                                },
                                "MRR": mrr(unscoped_sessions, gold_sessions),
                                "returned_hits": len(ranked),
                            }
                out["questions"].append(row)
            finally:
                conn.close()
    out["aggregate"] = _aggregate(out)
    return out


def _session_of(custom_id: str) -> str:
    """The session part of a ``project::session`` document id.

    An id with no separator is returned whole, so a malformed id still resolves to
    *something* for the unscoped recall figures rather than collapsing to ``""`` — which
    would make every such hit match every gold session at once.
    """
    _, separator, session = custom_id.partition("::")
    return session if separator else custom_id


def _project_of(custom_id: str) -> str:
    """The project part of a ``project::session`` document id.

    An id with no ``::`` separator yields ``""``, which compares unequal to every real
    project id -- so an unrecognised id is treated as belonging to no project rather than
    silently matching project 0.
    """
    project, separator, _ = custom_id.partition("::")
    return project if separator else ""


def summarize(result: dict[str, Any]) -> str:
    aggregate = result.get("aggregate") or _aggregate(result)
    lines = [
        f"# LongMemEval-S retrieval — n={result['n']}",
        f"dataset: {result.get('dataset', 'longmemeval-s')}",
        "",
    ]
    scope = result.get("manifest", {}).get("scope_config") or {}
    if scope:
        lines += [
            "## Scope",
            f"- project_count: {scope.get('project_count')}",
            f"- scope: {scope.get('scope')}",
            f"- scoped_queries: {scope.get('scoped_queries', 0)}",
            f"- unscoped_queries: {scope.get('unscoped_queries', 0)}",
            (
                f"- unscoped_cross_scope_hits: {scope.get('unscoped_cross_scope_hits', 0)} "
                "(informational: an unscoped query is supposed to see every project)"
            ),
            f"- leaked_hits: {scope.get('leaked_hits', 0)} (the gate)",
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
    baselines = result.get("manifest", {}).get("baseline_sources") or {}
    if baselines:
        lines += ["## Baselines gated against", ""]
        for axis, path in sorted(baselines.items()):
            lines.append(f"- {axis}: `{path}`")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--k", default="5,10")
    parser.add_argument("--modes", default="hybrid,documents")
    parser.add_argument(
        "--project-count",
        type=int,
        default=1,
        help="ingest each session into N projects, enabling the isolation axis on real data",
    )
    parser.add_argument(
        "--scope",
        default="scoped",
        help="scoped | unscoped | both. 'both' populates the unscoped bucket (invariant I2)",
    )
    parser.add_argument(
        "--latency-baseline",
        default="",
        help="BASELINES.md to gate this run's latency figures against",
    )
    parser.add_argument(
        "--cost-baseline",
        default="",
        help="BASELINES.md to gate this run's cost figures against",
    )
    parser.add_argument("--out-md", default="")
    parser.add_argument("--out-json", default="")
    args = parser.parse_args(argv)

    if not os.path.exists(args.data):
        print(f"corpus not found: {args.data}", file=sys.stderr)
        return EXIT_BAD_INPUT

    ks = [int(value) for value in args.k.split(",") if value.strip()]
    modes = [value.strip() for value in args.modes.split(",") if value.strip()]
    # Validated before the run, not inside `evaluate`: an empty `--modes` would otherwise
    # produce an empty aggregate, and an empty report reads exactly like a clean one.
    if not ks or any(k <= 0 for k in ks):
        print("--k must list at least one positive recall depth", file=sys.stderr)
        return EXIT_BAD_INPUT
    if not modes:
        print("--modes must list at least one search mode", file=sys.stderr)
        return EXIT_BAD_INPUT

    # Validate the baselines BEFORE spending minutes on a run: a flag that silently gates
    # against nothing is worse than one that is refused.
    baselines: dict[str, str] = {}
    for flag, value, label in (
        ("--latency-baseline", args.latency_baseline, "latency"),
        ("--cost-baseline", args.cost_baseline, "cost"),
    ):
        if not value:
            continue
        if not os.path.exists(value):
            print(f"{flag} not found: {value}", file=sys.stderr)
            return EXIT_BAD_INPUT
        baselines[label] = value

    try:
        records = load_records(args.data)
        started = time.time()
        result = evaluate(
            records,
            n=0 if args.full else args.n,
            seed=args.seed,
            ks=ks,
            modes=modes,
            dataset_hash=file_sha256(args.data),
            project_count=args.project_count,
            scope=args.scope,
        )
    except (ValueError, TypeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_INPUT
    result["manifest"]["baseline_sources"] = baselines

    result["seconds"] = round(time.time() - started, 1)
    report = summarize(result) + f"\ntook {result['seconds']}s\n"
    print(report)
    try:
        if args.out_md:
            os.makedirs(os.path.dirname(args.out_md) or ".", exist_ok=True)
            Path(args.out_md).write_text(report)
        if args.out_json:
            os.makedirs(os.path.dirname(args.out_json) or ".", exist_ok=True)
            Path(args.out_json).write_text(json.dumps(result, indent=2, ensure_ascii=False))
    except OSError as exc:
        print(f"could not write results: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    # A scope leak is a correctness failure, not a metric to report around.
    scope = result["manifest"]["scope_config"]
    return EXIT_GATE_FAILED if scope["leaked_hits"] else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
