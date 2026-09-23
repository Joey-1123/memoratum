# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dataset loading and normalization shared by local evaluation runners."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def load_records(path: str | Path) -> list[dict[str, Any]]:
    """Load a JSON array/object wrapper or JSONL file into validated records."""
    source = Path(path)
    text = source.read_text()
    if source.suffix.lower() == ".jsonl":
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        payload = json.loads(text)
        if isinstance(payload, dict):
            wrapped = False
            for key in ("data", "questions", "records", "items"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    wrapped = True
                    break
            if not wrapped and any(
                key in payload for key in ("question", "query", "sessions", "haystack_sessions")
            ):
                payload = [payload]
        records = payload
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValueError("evaluation data must be a JSON array of objects")
    return records


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def records_sha256(records: list[dict[str, Any]]) -> str:
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_turn(turn: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(turn)
    if "role" not in normalized and "speaker" in normalized:
        normalized["role"] = normalized["speaker"]
    if "content" not in normalized and "text" in normalized:
        normalized["content"] = normalized["text"]
    return normalized


def _session_turns(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        for key in ("turns", "conversation", "messages"):
            if isinstance(value.get(key), list):
                return [_normalize_turn(turn) for turn in value[key] if isinstance(turn, dict)]
        return [_normalize_turn(value)]
    if isinstance(value, list):
        return [_normalize_turn(turn) for turn in value if isinstance(turn, dict)]
    return [{"role": "user", "content": str(value)}]


def normalize_longmemeval(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize common LongMemEval-S exports to the runner's stable shape."""
    normalized = []
    for index, record in enumerate(records):
        question = record.get("question", record.get("query"))
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"record {index} has no question")
        raw_sessions = record.get("haystack_sessions", record.get("sessions", []))
        raw_ids = _as_list(record.get("haystack_session_ids", record.get("session_ids")))
        if isinstance(raw_sessions, list) and all(
            isinstance(item, dict) and "session_id" in item and "turns" in item
            for item in raw_sessions
        ):
            sessions = [
                {"session_id": str(item["session_id"]), "turns": _session_turns(item["turns"])}
                for item in raw_sessions
            ]
        else:
            if isinstance(raw_sessions, dict):
                session_items = list(raw_sessions.items())
            elif isinstance(raw_sessions, list):
                session_items = [
                    (
                        item.get("session_id", position)
                        if isinstance(item, dict) and item.get("session_id") is not None
                        else position,
                        item,
                    )
                    for position, item in enumerate(raw_sessions)
                ]
            else:
                session_items = []
            sessions = []
            for position, (session_key, session) in enumerate(session_items):
                if isinstance(session_key, int):
                    session_id = (
                        str(raw_ids[position]) if position < len(raw_ids) else f"session_{position}"
                    )
                else:
                    session_id = str(session_key)
                    if position < len(raw_ids) and raw_ids[position] is not None:
                        session_id = str(raw_ids[position])
                sessions.append({"session_id": session_id, "turns": _session_turns(session)})
        if not sessions and isinstance(record.get("context"), str):
            sessions = [
                {
                    "session_id": "session_0",
                    "turns": [{"role": "user", "content": record["context"]}],
                }
            ]
        if not sessions:
            raise ValueError(f"record {index} has no haystack sessions")
        answer_ids = record.get("answer_session_ids", record.get("answer_session_id"))
        if answer_ids is None:
            answer_ids = []
        answer_ids = [str(value) for value in _as_list(answer_ids)]
        normalized.append(
            {
                "question_id": str(record.get("question_id", record.get("id", index))),
                "question": question,
                "question_type": str(record.get("question_type", record.get("type", "unknown"))),
                "sessions": sessions,
                "answer_session_ids": answer_ids,
                "answer": record.get("answer"),
            }
        )
    return normalized


def sample_records(records: list[dict[str, Any]], *, n: int, seed: int) -> list[dict[str, Any]]:
    """Select a reproducible sample; ``n <= 0`` means the complete dataset."""
    if n <= 0 or n >= len(records):
        return list(records)
    import random

    return random.Random(seed).sample(records, n)
