# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dataset loading and normalization shared by local evaluation runners."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

#: Wrapper keys accepted at the top level of a JSON object, tried in order.
WRAPPER_KEYS = ("data", "questions", "records", "items")

#: Keys that identify a bare single-record object rather than a wrapper.
SINGLE_RECORD_KEYS = ("question", "query", "sessions", "haystack_sessions")


def unwrap_payload(payload: Any) -> list[Any]:
    """Reduce a decoded JSON payload to its record list.

    Shared by :func:`load_records` and :func:`iter_records` so both accept exactly the
    same corpus shapes. When this logic lived in only one of them, a file that worked
    with one loader was rejected by the other with a message calling the file invalid.
    """
    if isinstance(payload, dict):
        for key in WRAPPER_KEYS:
            if isinstance(payload.get(key), list):
                return payload[key]
        if any(key in payload for key in SINGLE_RECORD_KEYS):
            return [payload]
    return payload


def iter_records(path: str | Path, *, limit: int) -> list[dict[str, Any]]:
    """Read at most ``limit`` records without materialising the whole file.

    ``load_records`` reads the corpus as one string and parses it whole, which exhausts a
    small host on the 277 MB LongMemEval file. An axis that only needs a prefix must not
    pay that cost, so it streams instead.

    Wrapped ``{"data": [...]}`` objects cannot be streamed -- the array position is
    unknown until the payload is parsed -- so those fall back to ``load_records``, which
    is correct for the smaller files that use that shape.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    source = Path(path)
    if source.suffix.lower() == ".jsonl":
        records: list[dict[str, Any]] = []
        with source.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    # ValueError, not TypeError: load_records raises ValueError for the
                    # same condition, and the whole point of sharing this reader is that
                    # both accept and reject exactly the same corpora.
                    raise ValueError(  # noqa: TRY004
                        "evaluation data must be a JSON array of objects"
                    )
                records.append(record)
                if len(records) >= limit:
                    break
        return records

    with source.open(encoding="utf-8") as handle:
        head = handle.read(4096).lstrip()
    if head.startswith("{"):
        # A wrapper or a single record: let the canonical loader decide.
        return load_records(source)[:limit]

    decoder = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    with source.open(encoding="utf-8") as handle:
        buffer = handle.read(1 << 20)
        position = 0
        while position < len(buffer) and buffer[position] in " \t\r\n,":
            position += 1
        if position >= len(buffer) or buffer[position] != "[":
            raise ValueError("evaluation data must be a JSON array of objects")
        position += 1
        while len(out) < limit:
            while position < len(buffer) and buffer[position] in " \t\r\n,":
                position += 1
            record_start = position
            if record_start >= len(buffer) or buffer[record_start] == "]":
                # End of the array -- either the closing bracket or a file with fewer
                # records than `limit`. Neither is an error: the caller asked for UP TO
                # `limit` records. Without this, raw_decode is handed `]`, raises, and
                # _is_complete reports "malformed", rejecting a perfectly good short corpus.
                break
            try:
                obj, end = decoder.raw_decode(buffer, position)
            except ValueError:
                # Distinguish "truncated, read more" from "malformed". Retrying a
                # malformed record to EOF would rebuild the whole file in memory, which
                # is the exact failure this reader exists to avoid.
                if _is_complete(buffer, record_start):
                    raise
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                buffer = buffer[position:] + chunk
                position = 0
                continue
            if not isinstance(obj, dict):
                raise ValueError(  # noqa: TRY004 -- matches load_records, see above
                    "evaluation data must be a JSON array of objects"
                )
            out.append(obj)
            position = end
            if position > (1 << 19):
                buffer = buffer[position:]
                position = 0
    return out


def _is_complete(buffer: str, record_start: int) -> bool:
    """True when the record beginning at ``record_start`` is malformed, not truncated.

    Scans from the START OF THE RECORD, not from the failure position: a partial buffer
    holds the tail of a record whose opening bracket was already consumed, so scanning
    from the failure point sees balanced braces and wrongly reports malformation -- which
    would abort every legitimately-truncated record.

    A truncated value ends with unbalanced open brackets or an unterminated string; a
    malformed one has already closed everything it opened.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in buffer[record_start:]:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            if depth == 0:
                return True
            depth -= 1
    return depth == 0 and not in_string


def load_records(path: str | Path) -> list[dict[str, Any]]:
    """Load a JSON array/object wrapper or JSONL file into validated records."""
    source = Path(path)
    text = source.read_text()
    if source.suffix.lower() == ".jsonl":
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        records = unwrap_payload(json.loads(text))
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
