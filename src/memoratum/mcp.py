# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""MCP server over stdio (newline-delimited JSON-RPC): remember + recall tools."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from memoratum import db, ingest
from memoratum.config import Settings
from memoratum.embeddings import HashEmbedder
from memoratum.search import search

TOOLS = [
    {
        "name": "remember",
        "description": "Store content in memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "containerTag": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "recall",
        "description": "Search memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string"},
                "containerTag": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["q"],
        },
    },
]


def _text(content: list[dict[str, Any]]) -> str:
    return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))


def main() -> None:
    settings = Settings.load()
    os.makedirs(settings.data_dir, exist_ok=True)
    conn = db.connect(settings.db_path)
    embedder = HashEmbedder()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params", {}) or {}

        def reply(result: Any = None, error: Any = None, *, mid: Any = mid) -> None:
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": mid}
            if error is not None:
                payload["error"] = error
            else:
                payload["result"] = result
            out.write(json.dumps(payload) + "\n")
            out.flush()

        try:
            if method == "initialize":
                reply(
                    {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "memoratum", "version": "0.4.0"},
                    }
                )
            elif method == "tools/list":
                reply({"tools": TOOLS})
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments", {}) or {}
                if name == "remember":
                    doc = db.create_document(
                        conn,
                        container_tag=args.get("containerTag", "default"),
                        content=args["content"],
                        metadata=args.get("metadata"),
                    )
                    ingest.process_one(conn, embedder)
                    status = db.get_document(conn, doc["id"])["status"]
                    reply(
                        {
                            "content": [
                                {"type": "text", "text": "stored" if status == "done" else status}
                            ]
                        }
                    )
                elif name == "recall":
                    hits = search(
                        conn,
                        embedder,
                        args["q"],
                        container_tag=args.get("containerTag", "default"),
                        limit=int(args.get("limit", 5)),
                    )
                    reply(
                        {
                            "content": [
                                {"type": "text", "text": h.get("memory", h.get("chunk", ""))}
                                for h in hits
                            ]
                        }
                    )
                else:
                    reply(error={"code": -32601, "message": f"unknown tool: {name}"})
            else:
                reply(error={"code": -32601, "message": f"unknown method: {method}"})
        except Exception as exc:  # noqa: BLE001 — JSON-RPC must always answer
            reply(error={"code": -32603, "message": str(exc)})


if __name__ == "__main__":
    main()
