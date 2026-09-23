# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LoCoMo-MC10 retrieval and multiple-choice evaluation runner."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from memoratum import db
from memoratum.embeddings import Embedder
from memoratum.eval_datasets import (
    _session_turns,
    file_sha256,
    load_records,
    records_sha256,
    sample_records,
)
from memoratum.eval_longmemeval import build_embedder, format_session, hit_session
from memoratum.ingest import process_all
from memoratum.search import search
from memoratum.vectorstore import SQLiteVectorStore, VectorStore


class ChoiceAnswerer(Protocol):
    def choose(self, question: str, choices: list[str], context: str) -> int: ...


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


class HeuristicChoiceAnswerer:
    """Deterministic no-model baseline for MC10 smoke and regression runs."""

    def choose(self, question: str, choices: list[str], context: str) -> int:
        query_tokens = _tokens(question)
        context_tokens = _tokens(context)
        best_index = 0
        best_score = -1.0
        for index, choice in enumerate(choices):
            choice_tokens = _tokens(str(choice))
            score = 3.0 * len(choice_tokens & context_tokens)
            score += len(choice_tokens & query_tokens)
            if str(choice).strip().lower() in context.lower():
                score += 5.0
            if score > best_score:
                best_index, best_score = index, score
        return best_index


class ChatChoiceAnswerer:
    """Use the configured chat provider, with a deterministic fallback on parse errors."""

    def __init__(self, llm: Any, fallback: ChoiceAnswerer | None = None) -> None:
        self.llm = llm
        self.fallback = fallback or HeuristicChoiceAnswerer()

    def choose(self, question: str, choices: list[str], context: str) -> int:
        prompt = (
            "Answer the multiple-choice question using only the context. "
            'Return JSON {"choice_index": <zero-based index>}.\n\n'
            f"Question: {question}\nChoices: {json.dumps(choices)}\nContext:\n{context[:12000]}"
        )
        try:
            raw = self.llm.complete("You are a precise evaluation answerer.", prompt)
            match = re.search(r"choice_index[^0-9]*(\d+)", raw, flags=re.IGNORECASE)
            if match and 0 <= int(match.group(1)) < len(choices):
                return int(match.group(1))
        except Exception:  # noqa: BLE001 — evaluation must remain runnable offline
            return self.fallback.choose(question, choices, context)
        return self.fallback.choose(question, choices, context)


def load_mc10_records(path: str | Path) -> list[dict[str, Any]]:
    return load_records(path)


def _session_texts(record: dict[str, Any], source: str) -> list[dict[str, Any]]:
    ids = record.get("haystack_session_ids", record.get("session_ids", []))
    if not isinstance(ids, list):
        ids = [ids]
    if source == "summaries":
        raw_sessions = record.get("haystack_session_summaries", record.get("session_summaries", []))
    else:
        raw_sessions = record.get("haystack_sessions", record.get("sessions", []))
    if not isinstance(raw_sessions, list):
        raw_sessions = []
    sessions = []
    for index, raw in enumerate(raw_sessions):
        session_id = str(ids[index]) if index < len(ids) else f"session_{index}"
        turns = _session_turns(raw)
        if not turns:
            continue
        sessions.append({"session_id": session_id, "turns": turns})
    return sessions


def normalize_mc10(
    records: list[dict[str, Any]], *, source: str = "dialogs"
) -> list[dict[str, Any]]:
    """Validate and normalize flat MC10 records for the local runner."""
    if source not in {"dialogs", "summaries"}:
        raise ValueError(f"unknown MC10 source: {source}")
    normalized = []
    for index, record in enumerate(records):
        question = record.get("question", record.get("query"))
        choices = record.get("choices")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"MC10 record {index} has no question")
        if not isinstance(choices, list) or len(choices) < 2:
            raise ValueError(f"MC10 record {index} must contain at least two choices")
        choices = [str(choice) for choice in choices]
        correct_index = record.get("correct_choice_index")
        if correct_index is None:
            answer = str(record.get("answer", ""))
            try:
                correct_index = choices.index(answer)
            except ValueError as exc:
                raise ValueError(f"MC10 record {index} has no valid answer choice") from exc
        correct_index = int(correct_index)
        if not 0 <= correct_index < len(choices):
            raise ValueError(f"MC10 record {index} has an out-of-range correct choice")
        sessions = _session_texts(record, source)
        if not sessions:
            raise ValueError(f"MC10 record {index} has no haystack sessions")
        normalized.append(
            {
                "question_id": str(record.get("question_id", record.get("id", index))),
                "question": question,
                "question_type": str(record.get("question_type", record.get("type", "unknown"))),
                "sessions": sessions,
                "choices": choices,
                "answer": str(record.get("answer", choices[correct_index])),
                "correct_choice_index": correct_index,
            }
        )
    return normalized


def _aggregate(rows: list[dict[str, Any]]) -> tuple[float, float, dict[str, dict[str, Any]]]:
    if not rows:
        return 0.0, 0.0, {}
    by_type: dict[str, list[int]] = {}
    correct = 0
    for row in rows:
        correct += int(row["correct"])
        by_type.setdefault(row["question_type"], []).append(int(row["correct"]))
    type_results = {
        question_type: {
            "count": len(values),
            "correct": sum(values),
            "accuracy": sum(values) / len(values),
        }
        for question_type, values in sorted(by_type.items())
    }
    balanced = sum(value["accuracy"] for value in type_results.values()) / len(type_results)
    return correct / len(rows), balanced, type_results


def evaluate_mc10(
    records: list[dict[str, Any]],
    *,
    n: int,
    seed: int,
    k: int,
    embedder: Embedder | None = None,
    answerer: ChoiceAnswerer | None = None,
    vector_store_factory: Callable[[sqlite3.Connection], VectorStore] | None = None,
    source: str = "dialogs",
    dataset_hash: str = "",
) -> dict[str, Any]:
    if k <= 0:
        raise ValueError("k must be positive")
    normalized = normalize_mc10(records, source=source)
    picked = sample_records(normalized, n=n, seed=seed)
    embedder = embedder or build_embedder()
    answerer = answerer or HeuristicChoiceAnswerer()
    factory = vector_store_factory or (lambda conn: SQLiteVectorStore(conn))
    rows = []
    for item in picked:
        with tempfile.TemporaryDirectory(prefix="memoratum-mc10-") as directory:
            conn = db.connect(f"{directory}/mc10.db")
            store = factory(conn)
            try:
                session_text: dict[str, str] = {}
                for session in item["sessions"]:
                    text = format_session(session["turns"])
                    if not text:
                        continue
                    session_text[session["session_id"]] = text
                    db.create_document(
                        conn,
                        container_tag="bench",
                        content=text,
                        custom_id=session["session_id"],
                    )
                process_all(conn, embedder, vector_store=store)
                hits = search(
                    conn,
                    embedder,
                    item["question"],
                    container_tag="bench",
                    limit=k,
                    search_mode="documents",
                    vector_store=store,
                )
                ranked = []
                for hit in hits:
                    session = hit_session(conn, hit)
                    if session and session not in ranked:
                        ranked.append(session)
                context = "\n".join(
                    session_text[session] for session in ranked if session in session_text
                )
                prediction = answerer.choose(item["question"], item["choices"], context)
                rows.append(
                    {
                        "id": item["question_id"],
                        "question_type": item["question_type"],
                        "prediction": prediction,
                        "correct_choice_index": item["correct_choice_index"],
                        "correct": prediction == item["correct_choice_index"],
                        "ranked_sessions": ranked,
                    }
                )
            finally:
                conn.close()
    accuracy, balanced, by_type = _aggregate(rows)
    return {
        "dataset": f"locomo-mc10-{source}",
        "manifest": {
            "schema": "locomo-mc10-v1",
            "seed": seed,
            "requested_n": n,
            "n": len(picked),
            "k": k,
            "embedder": f"{type(embedder).__name__}:{getattr(embedder, 'dims', 0)}",
            "answerer": type(answerer).__name__,
            "data_sha256": dataset_hash or records_sha256(picked),
        },
        "n": len(picked),
        "accuracy": accuracy,
        "balanced_accuracy": balanced,
        "by_type": by_type,
        "questions": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--source", choices=("dialogs", "summaries"), default="dialogs")
    parser.add_argument("--out-json", default="")
    args = parser.parse_args()
    result = evaluate_mc10(
        load_mc10_records(args.data),
        n=0 if args.full else args.n,
        seed=args.seed,
        k=args.k,
        source=args.source,
        dataset_hash=file_sha256(args.data),
    )
    print(
        json.dumps(
            {key: result[key] for key in ("dataset", "n", "accuracy", "balanced_accuracy")},
            indent=2,
        )
    )
    if args.out_json:
        output = Path(args.out_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
