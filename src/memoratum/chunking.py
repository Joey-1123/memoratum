# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Recursive + markdown-aware chunking with heading metadata."""

from __future__ import annotations

import re
from typing import Any

_SPLIT_RE = re.compile(r"(\n\n|\n|\. | )")


def split_text(text: str, *, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    """Split on separators, packing delimiter-attached units so chunks stay verbatim substrings."""
    parts = [p for p in _SPLIT_RE.split(text) if p != ""]
    units: list[str] = []
    i = 0
    while i < len(parts):
        unit = parts[i]
        if i + 1 < len(parts) and _SPLIT_RE.fullmatch(parts[i + 1]):
            unit += parts[i + 1]
            i += 2
        else:
            i += 1
        units.append(unit)
    chunks: list[str] = []
    buf = ""
    for unit in units:
        if len(buf) + len(unit) > chunk_size and buf.strip():
            chunks.append(buf.strip())
            tail = buf[max(0, len(buf) - overlap) :]
            buf = tail if tail.strip() else ""
        buf += unit
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c]


def split_markdown(text: str, *, chunk_size: int = 1500) -> list[dict[str, Any]]:
    """Split on headings; keep fenced code blocks atomic; prefix heading."""
    lines = text.split("\n")
    sections: list[tuple[str | None, list[str]]] = []
    heading: str | None = None
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        if any(line.strip() for line in buf):
            sections.append((heading, list(buf)))
        buf.clear()

    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m and not in_fence:
            flush()
            heading = m.group(2).strip()
        else:
            buf.append(line)
    flush()

    chunks: list[dict[str, Any]] = []
    for h, section_lines in sections:
        prefix = f"# {h}\n\n" if h else ""
        body = "\n".join(section_lines).strip()
        if not body:
            continue
        if len(prefix) + len(body) <= chunk_size:
            chunks.append({"text": prefix + body, "heading": h})
        else:
            for piece in split_text(body, chunk_size=chunk_size - len(prefix), overlap=100):
                chunks.append({"text": prefix + piece, "heading": h})
    return chunks
