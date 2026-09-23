# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dreaming: LLM fact extraction over documents into the temporal graph.

Modes: `instant` dreams each document on its own right away (per-doc call,
facts carry document_id); `dynamic` bundles a tag's undreamed documents into
one call (facts are tag-level, document_id NULL) for coherent multi-doc units.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from typing import Any

from memoratum import db
from memoratum.facts import add_fact

_SYSTEM = (
    "Extract atomic facts from the text. Each fact must be a single complete claim,"
    " self-contained and understandable without context. Return a JSON object with a"
    ' "facts" array; each item has exactly "subject", "predicate", "object" strings.'
    ' Example: {"facts": [{"subject": "user", "predicate": "loves", "object": "Paris"}]}.'
    ' If there are no facts, return {"facts": []}.'
)


class ChatLLM:
    """OpenAI-compatible chat client (works with Ollama, vLLM, proxies)."""

    def __init__(
        self, *, endpoint: str, model: str, api_key: str = "", timeout: float = 60.0
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def complete(self, system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
            }
        ).encode()
        last: Exception | None = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    self.endpoint + "/chat/completions",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as res:
                    data = json.loads(res.read().decode())
                return str(data["choices"][0]["message"]["content"])
            except Exception as exc:  # noqa: BLE001 — retry transient failures, raise after
                last = exc
                time.sleep(2**attempt)
        raise last  # type: ignore[misc]


def extract_facts(llm: ChatLLM, text: str) -> list[dict[str, str]]:
    """Extract validated facts; malformed LLM output yields [] (never raises)."""
    try:
        raw = llm.complete(_SYSTEM, text[:8000])
        data = json.loads(raw)
        items = data.get("facts") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if (
                isinstance(item, dict)
                and isinstance(item.get("subject"), str)
                and isinstance(item.get("predicate"), str)
                and isinstance(item.get("object"), str)
                and item["subject"].strip()
                and item["predicate"].strip()
                and item["object"].strip()
            ):
                out.append(
                    {
                        "subject": item["subject"].strip(),
                        "predicate": item["predicate"].strip(),
                        "object": item["object"].strip(),
                    }
                )
        return out
    except Exception:  # noqa: BLE001 — unparseable output means no facts
        return []


def _mark_dreamed(conn: sqlite3.Connection, doc_ids: list[str]) -> None:
    now = time.time()
    conn.executemany(
        "UPDATE documents SET dreamed_at = ? WHERE id = ?", [(now, d) for d in doc_ids]
    )
    conn.commit()


def dream_document(conn: sqlite3.Connection, llm: ChatLLM, doc_id: str) -> list[dict[str, Any]]:
    """Instant mode: dream one document on its own; facts carry its id."""
    doc = db.get_document(conn, doc_id)
    stored = [
        add_fact(
            conn,
            container_tag=doc["container_tag"],
            document_id=doc_id,
            org_id=doc.get("org_id"),
            **f,
        )
        for f in extract_facts(llm, doc["content"])
    ]
    _mark_dreamed(conn, [doc_id])
    return stored


_BUDGET = 8000


def dream_pending(
    conn: sqlite3.Connection,
    llm: ChatLLM,
    *,
    mode: str = "instant",
    container_tag: str | None = None,
    org_id: str | None = None,
) -> int:
    """Dream undreamed documents, optionally scoped to one tag and organization."""
    query = "SELECT id, container_tag, content, org_id FROM documents WHERE dreamed_at IS NULL"
    params: tuple[Any, ...] = ()
    if container_tag is not None:
        query += " AND container_tag = ?"
        params += (container_tag,)
    if org_id is not None:
        query += " AND org_id = ?"
        params += (org_id,)
    rows = conn.execute(query + " ORDER BY created_at", params).fetchall()
    if not rows:
        return 0
    calls = 0
    if mode == "dynamic":
        by_scope: dict[tuple[str, str | None], list[sqlite3.Row]] = {}
        for row in rows:
            by_scope.setdefault((row["container_tag"], row["org_id"]), []).append(row)
        for (tag, scope_org), docs in by_scope.items():
            windows: list[tuple[str, int]] = []
            for doc in docs:
                text = doc["content"] or " "
                for i in range(0, len(text), _BUDGET):
                    windows.append((text[i : i + _BUDGET], doc["id"]))
            bundle: list[tuple[str, int]] = []
            used = 0
            for text, doc_id in windows:
                if bundle and used + len(text) > _BUDGET:
                    calls += _dream_bundle(conn, llm, tag, scope_org, bundle)
                    bundle, used = [], 0
                bundle.append((text, doc_id))
                used += len(text)
            if bundle:
                calls += _dream_bundle(conn, llm, tag, scope_org, bundle)
    else:
        for row in rows:
            dream_document(conn, llm, row["id"])
            calls += 1
    return calls


def _dream_bundle(
    conn: sqlite3.Connection,
    llm: ChatLLM,
    tag: str,
    org_id: str | None,
    bundle: list[tuple[str, int]],
) -> int:
    text = "\n\n---\n\n".join(t for t, _ in bundle)
    for f in extract_facts(llm, text):
        add_fact(conn, container_tag=tag, document_id=None, org_id=org_id, **f)
    _mark_dreamed(conn, list({doc_id for _, doc_id in bundle}))
    return 1
