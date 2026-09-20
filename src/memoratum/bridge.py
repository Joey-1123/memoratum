# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""graphify-out/ → Memoratum bridge: links become facts, report becomes a document."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from typing import Any, Protocol

from memoratum import db
from memoratum.facts import add_fact, delete_fact, list_facts


class GraphAPI(Protocol):
    def post(self, path: str, body: dict[str, Any]) -> Any: ...
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...
    def delete(self, path: str) -> Any: ...


def qualified(node: dict[str, Any]) -> str:
    label = str(node.get("label") or node.get("id", "?"))
    source_file = node.get("source_file")
    return f"{label} @ {source_file}" if source_file else label


def fact_records(graph: dict[str, Any], *, slug: str, commit: str) -> list[dict[str, Any]]:
    tag = f"graphify:{slug}"
    nodes = {n.get("id"): n for n in graph.get("nodes", []) if isinstance(n, dict)}
    records = []
    for link in graph.get("links", []):
        if not isinstance(link, dict):
            continue
        src, tgt = nodes.get(link.get("source")), nodes.get(link.get("target"))
        if src is None or tgt is None:
            continue
        records.append(
            {
                "subject": qualified(src),
                "predicate": str(link.get("relation", "relates")),
                "object": qualified(tgt),
                "containerTag": tag,
                "metadata": {
                    "graphify": True,
                    "slug": slug,
                    "commit": commit,
                    "weight": link.get("weight"),
                    "confidence": link.get("confidence_score"),
                    "community": src.get("community_name"),
                    "location": link.get("source_location"),
                },
            }
        )
    return records


def fact_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (record["subject"], record["predicate"], record["object"])


def sync_records(conn: sqlite3.Connection, graph: dict[str, Any], slug: str) -> dict[str, int]:
    """Direct-DB sync (server-side import endpoint). Same semantics as sync_graph."""
    tag = f"graphify:{slug}"
    commit = str(graph.get("built_at_commit", ""))
    report = graph.get("report") or f"graphify snapshot {slug}@{commit}"
    db.create_document(conn, container_tag=tag, content=report, custom_id=f"graphify:{slug}:report")
    records = fact_records(graph, slug=slug, commit=commit)
    for record in records:
        add_fact(
            conn,
            container_tag=record["containerTag"],
            subject=record["subject"],
            predicate=record["predicate"],
            object=record["object"],
            document_id=None,
            metadata=record["metadata"],
            supersede=False,
        )
    wanted = {fact_key(r) for r in records}
    deleted = 0
    for fact in list_facts(conn, tag, include_superseded=True):
        key = (fact["subject"], fact["predicate"], fact["object"])
        is_graph = bool((fact.get("metadata") or {}).get("graphify"))
        if is_graph and key not in wanted and delete_fact(conn, fact["id"]):
            deleted += 1
    return {"facts_upserted": len(records), "facts_deleted": deleted}


def sync_graph(api: GraphAPI, graph: dict[str, Any], *, slug: str) -> dict[str, int]:
    """Upsert report doc + link facts; delete vanished graph facts. Returns counts."""
    commit = str(graph.get("built_at_commit", ""))
    tag = f"graphify:{slug}"
    report = graph.get("report") or ""
    api.post(
        "/v3/documents",
        {
            "content": report or f"graphify snapshot {slug}@{commit}",
            "containerTag": tag,
            "customId": f"graphify:{slug}:report",
        },
    )
    records = fact_records(graph, slug=slug, commit=commit)
    for record in records:
        api.post("/v4/facts", {**record, "supersede": False})
    wanted = {fact_key(r) for r in records}
    deleted = 0
    listed = api.get("/v4/facts", {"containerTag": tag, "limit": 10000})
    for fact in listed.get("facts", []):
        meta = fact.get("metadata") or {}
        if not meta.get("graphify"):
            continue
        if (fact.get("subject"), fact.get("predicate"), fact.get("object")) not in wanted:
            api.delete(f"/v4/memories/{fact['id']}")
            deleted += 1
    return {"facts_upserted": len(records), "facts_deleted": deleted}


def main() -> None:
    from memoratum.client import Client

    ap = argparse.ArgumentParser(description="Import a graphify-out/ snapshot into Memoratum.")
    ap.add_argument("graph_dir", help="directory containing graph.json (and GRAPH_REPORT.md)")
    ap.add_argument("--tag", default="", help="override container tag (default graphify:<slug>)")
    ap.add_argument("--url", default=os.environ.get("MEMORATUM_URL", "http://localhost:6767"))
    ap.add_argument("--api-key", default=os.environ.get("MEMORATUM_API_KEY", ""))
    args = ap.parse_args()
    slug = (
        args.tag.removeprefix("graphify:")
        if args.tag
        else os.path.basename(os.path.abspath(os.path.join(args.graph_dir, "..")))
    )
    with open(os.path.join(args.graph_dir, "graph.json")) as f:
        graph = json.load(f)
    report_path = os.path.join(args.graph_dir, "GRAPH_REPORT.md")
    if os.path.exists(report_path):
        with open(report_path) as f:
            graph["report"] = f.read()
    summary = sync_graph(
        Client(base_url=args.url, api_key=args.api_key, timeout=300.0), graph, slug=slug
    )
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
