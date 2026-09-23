# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Compare local evaluation result manifests without publishing data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _rows(result: dict[str, Any]) -> list[tuple[str, str, str, int, str, float]]:
    manifest = result.get("manifest", {})
    dataset = str(result.get("dataset", "unknown"))
    backend = str(manifest.get("vector_store", "unknown"))
    embedder = str(manifest.get("embedder", "unknown"))
    n = int(result.get("n", 0))
    rows = []
    if "accuracy" in result:
        rows.append((dataset, backend, embedder, n, "accuracy", float(result["accuracy"])))
        rows.append(
            (
                dataset,
                backend,
                embedder,
                n,
                "balanced_accuracy",
                float(result.get("balanced_accuracy", 0.0)),
            )
        )
    for mode, metrics in (result.get("aggregate") or {}).items():
        for metric, value in metrics.items():
            rows.append((dataset + ":" + mode, backend, embedder, n, metric, float(value)))
    return rows


def compare_results(results: list[dict[str, Any]]) -> str:
    lines = [
        "# Evaluation results",
        "",
        "| Dataset | Backend | Embedder | N | Metric | Value |",
        "|---|---|---|---:|---|---:|",
    ]
    for result in results:
        for dataset, backend, embedder, n, metric, value in _rows(result):
            lines.append(f"| {dataset} | {backend} | {embedder} | {n} | {metric} | {value:.6f} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+")
    parser.add_argument("--out-md", default="")
    args = parser.parse_args()
    loaded = [json.loads(Path(path).read_text()) for path in args.results]
    report = compare_results(loaded)
    print(report)
    if args.out_md:
        output = Path(args.out_md)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report)


if __name__ == "__main__":
    main()
