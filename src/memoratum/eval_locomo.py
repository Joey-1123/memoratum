# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local LoCoMo retrieval adapter and command-line runner.

The adapter accepts the released conversation JSON, turns each annotated QA
item into a retrieval example, and maps evidence dialog ids back to their
sessions. It supports dialog, observation, and session-summary databases.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from memoratum.eval_datasets import _session_turns, file_sha256, load_records
from memoratum.eval_longmemeval import evaluate


def load_locomo_records(path: str | Path) -> list[dict[str, Any]]:
    return load_records(path)


def _session_sort_key(value: str) -> tuple[int, str]:
    match = re.search(r"(\d+)$", value)
    return (int(match.group(1)) if match else 0, value)


def _conversation_sessions(conversation: Any) -> list[tuple[str, Any]]:
    if isinstance(conversation, dict):
        keys = [
            key
            for key in conversation
            if re.fullmatch(r"session[_-]?\d+", str(key), flags=re.IGNORECASE)
        ]
        if not keys:
            keys = [
                key
                for key in conversation
                if not str(key).endswith(("_date_time", "_observation", "_summary"))
                and isinstance(conversation[key], (list, dict))
            ]
        return [(str(key), conversation[key]) for key in sorted(keys, key=_session_sort_key)]
    if isinstance(conversation, list):
        return [
            (str(item.get("session_id", f"session_{index}")), item)
            for index, item in enumerate(conversation)
            if isinstance(item, dict)
        ]
    return []


def _source_text(session_id: str, raw: Any, source: str, sample: dict[str, Any]) -> str:
    if source == "dialogs":
        turns = _session_turns(raw)
        return "\n".join(
            f"{turn.get('role', turn.get('speaker', '?'))}: {turn.get('content', turn.get('text', ''))}"
            for turn in turns
        )
    if source == "observations":
        observations = sample.get("observation", {})
        if isinstance(observations, dict):
            value = observations.get(f"{session_id}_observation", observations.get(session_id, ""))
            return str(value or "")
    if source == "summaries":
        summaries = sample.get("session_summary", sample.get("session_summaries", {}))
        if isinstance(summaries, dict):
            value = summaries.get(f"{session_id}_summary", summaries.get(session_id, ""))
            return str(value or "")
    raise ValueError(f"unknown LoCoMo source: {source}")


def normalize_locomo(
    records: list[dict[str, Any]], *, source: str = "dialogs"
) -> list[dict[str, Any]]:
    """Convert LoCoMo conversations and QA annotations to retrieval records."""
    source = source.strip().lower()
    if source not in {"dialogs", "observations", "summaries"}:
        raise ValueError(f"unknown LoCoMo source: {source}")
    normalized: list[dict[str, Any]] = []
    for sample_index, sample in enumerate(records):
        if "sessions" in sample and "question" in sample:
            normalized.append(sample)
            continue
        sample_id = str(sample.get("sample_id", sample.get("id", sample_index)))
        sessions = _conversation_sessions(sample.get("conversation", {}))
        dialog_to_session: dict[str, str] = {}
        for session_id, raw in sessions:
            for turn in _session_turns(raw):
                dialog_id = turn.get("dia_id")
                if dialog_id is not None:
                    dialog_to_session[str(dialog_id)] = session_id
        indexed_sessions = []
        for session_id, raw in sessions:
            if source == "dialogs":
                turns = _session_turns(raw)
                if not turns:
                    continue
                indexed_sessions.append({"session_id": session_id, "turns": turns})
                continue
            text = _source_text(session_id, raw, source, sample)
            if not text:
                continue
            indexed_sessions.append(
                {"session_id": session_id, "turns": [{"role": source, "content": text}]}
            )
        if not indexed_sessions:
            continue
        qa_items = sample.get("qa", sample.get("questions", []))
        if isinstance(qa_items, dict):
            qa_items = list(qa_items.values())
        if not isinstance(qa_items, list):
            continue
        for qa_index, qa in enumerate(qa_items):
            if not isinstance(qa, dict):
                continue
            question = qa.get("question", qa.get("query"))
            if not isinstance(question, str) or not question.strip():
                continue
            evidence = qa.get("evidence", qa.get("evidence_ids", []))
            if isinstance(evidence, str):
                evidence = [evidence]
            evidence = [str(value) for value in evidence] if isinstance(evidence, list) else []
            gold = []
            for value in evidence:
                mapped = dialog_to_session.get(
                    value, value if value.startswith("session_") else None
                )
                if mapped and mapped not in gold:
                    gold.append(mapped)
            normalized.append(
                {
                    "question_id": f"{sample_id}:{qa_index}",
                    "question": question,
                    "question_type": str(qa.get("category", qa.get("question_type", "qa"))),
                    "sessions": indexed_sessions,
                    "answer_session_ids": gold,
                    "answer": qa.get("answer"),
                    "evidence": evidence,
                }
            )
    return normalized


def evaluate_locomo(
    records: list[dict[str, Any]],
    *,
    n: int,
    seed: int,
    ks: list[int],
    modes: list[str],
    source: str = "dialogs",
    **kwargs: Any,
) -> dict[str, Any]:
    normalized = normalize_locomo(records, source=source)
    return evaluate(
        normalized,
        n=n,
        seed=seed,
        ks=ks,
        modes=modes,
        dataset_name=f"locomo-{source}",
        **kwargs,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--k", default="5,10")
    parser.add_argument("--modes", default="hybrid,documents")
    parser.add_argument(
        "--source", choices=("dialogs", "observations", "summaries"), default="dialogs"
    )
    parser.add_argument("--out-md", default="")
    parser.add_argument("--out-json", default="")
    args = parser.parse_args()
    records = load_locomo_records(args.data)
    result = evaluate_locomo(
        records,
        n=0 if args.full else args.n,
        seed=args.seed,
        ks=[int(value) for value in args.k.split(",") if value.strip()],
        modes=[value.strip() for value in args.modes.split(",") if value.strip()],
        source=args.source,
        dataset_hash=file_sha256(args.data),
    )
    report = "\n".join(
        [
            f"# LoCoMo retrieval — source={args.source} n={result['n']}",
            "",
            result["aggregate"].__repr__(),
        ]
    )
    print(report)
    if args.out_md:
        output = Path(args.out_md)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report)
    if args.out_json:
        output = Path(args.out_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
