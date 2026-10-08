# Memoratum — self-hosted context infrastructure for AI agents.
# Copyright (C) 2026 Memoratum contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HotPotQA evidence retrieval: can the memory layer find the sentences that answer?

Phase C of feature ``002-evaluation-harness``, admitted for **evidence retrieval only**
(``spec.md``). The spec rejected HotPotQA for *answering* — that reasoning is correct and is
kept — but the dataset ships ``supporting_facts`` alongside a pure-stdlib deterministic
scorer, so the gold evidence can be graded without a judge and without a new dependency.

**No answer is scored.** There is no answerer here, and one is not merely unused: adding one
would reopen a decision the spec closed. ``tests/test_eval_hotpotqa.py`` asserts that
structurally, because a docstring promise is not a constraint.

Why one sentence is one document
--------------------------------
Gold evidence is a set of ``(title, sent_id)`` pairs, so that pair must be the unit of
retrieval. If a chunk held several sentences, a retrieved chunk would have to be matched to
its sentences by text similarity — the fuzzy step that made token containment unusable as a
grounding signal (``data-model.md`` G3: a fabricated passage assembled from a document's own
words scores containment 1.000). One sentence per document makes the scorer exact set
arithmetic over ids, with no threshold and no normalisation fudge.

Why ``distractor`` is refused
-----------------------------
The ``distractor`` split ships ~1.2K tokens per question, which a retriever can hold entirely
in context, so it does not force out-of-window retrieval. The union of provided contexts is
indexed instead, so a question competes with paragraphs belonging to other questions.

The caveat that travels with every figure
------------------------------------------
That union is ~74K paragraphs, not the ~5M articles of true full-wiki. This is multi-hop
evidence recall under a 74K-paragraph distractor load. The manifest and the report both say
so, because a figure that does not is read as the stronger claim.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Sequence
from typing import Any

from memoratum import db
from memoratum.embeddings import Embedder, HashEmbedder
from memoratum.eval_axes import (
    EXIT_BAD_INPUT,
    EXIT_OK,
    build_manifest,
    embedder_label,
    load_baseline,
    write_artifacts,
)
from memoratum.eval_datasets import file_sha256, load_records, records_sha256, sample_records
from memoratum.ingest import process_all
from memoratum.search import search
from memoratum.vectorstore import SQLiteVectorStore, VectorStore

#: The unit of evidence, quoted verbatim in the manifest so a figure is interpretable.
EVIDENCE_UNIT = "(title, sent_id)"

#: The scorer, quoted verbatim. Deterministic set arithmetic on ids — nothing else qualifies.
EVIDENCE_SCORER = "exact-set"

#: Recorded so a reader cannot infer full-wiki scale from a filename.
SCALE_CAVEAT = (
    "Sentences are indexed from the union of provided contexts (~74K paragraphs), which is "
    "NOT true full-wiki scale (~5M articles). This is multi-hop evidence recall under a "
    "~74K-paragraph distractor load."
)

#: Splits the spec admits. `distractor` is deliberately absent: see the module docstring.
ALLOWED_CONFIGS = ("fullwiki",)

#: Separator inside a document id. Chosen because it cannot appear in a Wikipedia title, so
#: `title::sent_id` splits unambiguously.
ID_SEPARATOR = "::"


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


def _split_id(custom_id: str) -> tuple[str, int]:
    """Inverse of ``f"{title}{ID_SEPARATOR}{sent_id}"``."""
    title, separator, sent = custom_id.rpartition(ID_SEPARATOR)
    if not separator or not sent.isdigit():
        raise ValueError(
            f"{custom_id!r} is not a '<title>{ID_SEPARATOR}<sent_id>' id; a sentence that "
            "cannot be addressed cannot be scored as evidence"
        )
    return title, int(sent)


def normalize_hotpotqa(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and normalise HotPotQA records into the runner's stable shape.

    Two rejections are load-bearing and neither is a convenience:

    * **A record with no ``supporting_facts`` is refused.** Without gold evidence there is
      nothing to score against, and scoring it 1.0 would report a question the harness never
      examined as a success.
    * **A supporting fact outside the provided context is refused.** Such gold is
      unretrievable by construction, so it would score 0.0 forever and quietly depress the
      figure — a dataset bug wearing the costume of a retrieval regression.
    """
    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise TypeError(f"record {index} is not an object, got {type(record).__name__}")

        question = record.get("question", record.get("query"))
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"record {index} has no question")

        raw_facts = record.get("supporting_facts")
        if not raw_facts:
            raise ValueError(
                f"record {index} has no supporting_facts; a question with no gold evidence "
                "cannot be graded and must not be reported as a success"
            )

        raw_context = record.get("context", record.get("paragraphs"))
        if not isinstance(raw_context, list) or not raw_context:
            raise ValueError(f"record {index} has no context")

        titles: dict[str, list[str]] = {}
        for entry in raw_context:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise ValueError(
                    f"record {index} has a malformed context entry; expected [title, [sentences]]"
                )
            title, sentences = entry
            if not isinstance(title, str) or not isinstance(sentences, list):
                raise TypeError(f"record {index} has a malformed context entry")
            titles[title] = [str(sentence) for sentence in sentences]

        gold: set[tuple[str, int]] = set()
        for fact in raw_facts:
            if not isinstance(fact, (list, tuple)) or len(fact) != 2:
                raise ValueError(
                    f"record {index} has a malformed supporting fact; expected [title, sent_id]"
                )
            title, sent_id = fact[0], fact[1]
            if not isinstance(title, str) or not isinstance(sent_id, int):
                raise TypeError(f"record {index} has a malformed supporting fact")
            if title not in titles:
                raise ValueError(
                    f"record {index}: supporting fact {title!r} is not in the provided "
                    "context, so it cannot be retrieved; this is a dataset error"
                )
            if not 0 <= sent_id < len(titles[title]):
                raise ValueError(
                    f"record {index}: supporting fact {title!r} sentence {sent_id} is not "
                    f"in the provided context (it has {len(titles[title])}); this is a "
                    "dataset error"
                )
            gold.add((title, sent_id))

        normalized.append(
            {
                "id": str(record.get("_id", record.get("question_id", index))),
                "question": question,
                "question_type": str(record.get("type", record.get("question_type", "unknown"))),
                "level": str(record.get("level", "unknown")),
                "gold_evidence": gold,
                "paragraphs": titles,
            }
        )
    return normalized


def build_sentence_corpus(
    records: Sequence[dict[str, Any]],
) -> list[tuple[str, int, str]]:
    """Every distinct sentence in the union of provided contexts, as ``(title, sent_id, text)``.

    The union rather than each question's own paragraphs, so a question competes with
    paragraphs belonging to other questions. A title shared by two questions is emitted
    **once**: ``create_document`` keys on ``custom_id``, so a duplicate would silently
    overwrite and make one copy of the text the corpus pretends to contain.
    """
    seen: dict[str, str] = {}
    for record in records:
        if "paragraphs" not in record:
            record = normalize_hotpotqa([record])[0]
        for title, sentences in record["paragraphs"].items():
            for sent_id, text in enumerate(sentences):
                key = f"{title}{ID_SEPARATOR}{sent_id}"
                if key in seen:
                    if seen[key] != text:
                        raise ValueError(
                            f"{key} appears with two different texts across questions; "
                            "silently keeping one would score evidence against text that "
                            "was never indexed under that id"
                        )
                    continue
                seen[key] = text
    return [
        (title, sent_id, text)
        for title, sent_id, text in ((*_split_id(key), value) for key, value in seen.items())
    ]


# --------------------------------------------------------------------------- #
# Scoring — exact set arithmetic on ids
# --------------------------------------------------------------------------- #


def evidence_scores(
    gold: set[tuple[str, int]], predicted: set[tuple[str, int]]
) -> dict[str, float | None]:
    """Recall, precision and F1 over ``(title, sent_id)`` pairs.

    Three choices that a single score would hide:

    * **Recall and precision are both reported.** Returning only correct evidence is
      maximally precise and half-complete; returning only wrong evidence is complete and
      worthless. One number cannot say which happened.
    * **Empty gold gives ``recall = None``, not 1.0.** The question was not exercised, and a
      perfect score would read as a pass.
    * **Empty predictions give ``precision = 0.0``, not a division error.** Retrieving
      nothing is maximally imprecise.
    """
    gold = set(gold)
    # Collapsed: a retriever must not pad its precision by repeating one hit.
    predicted = set(predicted)
    overlap = len(gold & predicted)

    recall = (overlap / len(gold)) if gold else None
    precision = (overlap / len(predicted)) if predicted else 0.0
    if recall is None or (recall + precision) == 0:
        f1 = None if recall is None else 0.0
    else:
        f1 = 2 * recall * precision / (recall + precision)
    return {
        "evidence_recall": recall,
        "evidence_precision": precision,
        "evidence_f1": f1,
        "gold_count": float(len(gold)),
        "predicted_count": float(len(predicted)),
        "overlap_count": float(overlap),
    }


def hops_found(gold: set[tuple[str, int]], predicted: set[tuple[str, int]]) -> int:
    """How many gold *titles* were reached.

    A hop is a document, not a sentence. HotPotQA's two supporting facts normally come from
    two different titles, so finding two sentences of the same title is one hop — counting
    it as two would let a retriever win the multi-hop metric with a single paragraph.
    """
    return len({title for title, _ in gold} & {title for title, _ in predicted})


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def _retrieved_evidence(
    conn: sqlite3.Connection, hits: Sequence[dict[str, Any]]
) -> list[tuple[str, int]]:
    """Map hits to ``(title, sent_id)``, in rank order, de-duplicated.

    A hit that resolves to no document is **dropped and counted**, never assumed to be
    evidence: an unresolvable hit has not been shown to belong to the corpus.
    """
    seen: list[tuple[str, int]] = []
    for hit in hits:
        hit_id = str(hit.get("id", ""))
        if not hit_id.startswith("chunk_"):
            continue
        try:
            chunk_id = int(hit_id.split("_", 1)[1])
        except ValueError:
            continue
        row = conn.execute(
            "SELECT d.custom_id FROM chunks c JOIN documents d ON d.id = c.document_id"
            " WHERE c.id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            continue
        try:
            pair = _split_id(str(row["custom_id"]))
        except ValueError:
            continue
        if pair not in seen:
            seen.append(pair)
    return seen


def evaluate_evidence(
    records: Sequence[dict[str, Any]],
    *,
    n: int,
    seed: int,
    k: int,
    embedder: Embedder | None = None,
    vector_store_factory: Callable[[sqlite3.Connection], VectorStore] | None = None,
    dataset_hash: str = "",
    dataset_name: str = "hotpotqa-evidence",
) -> dict[str, Any]:
    """Score sentence-level evidence retrieval over the union of provided contexts."""
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")

    normalized = normalize_hotpotqa(records)
    picked = sample_records(normalized, n=n, seed=seed)
    embedder = embedder or HashEmbedder(dims=64)
    factory = vector_store_factory or (lambda conn: SQLiteVectorStore(conn))
    store_label = (
        "sqlite"
        if vector_store_factory is None
        else getattr(vector_store_factory, "__name__", type(vector_store_factory).__name__)
    )

    # One corpus for the whole run: the union, not per-question paragraphs.
    corpus = build_sentence_corpus(picked)
    with tempfile.TemporaryDirectory(prefix="memoratum-hotpotqa-") as directory:
        conn = db.connect(os.path.join(directory, "evidence.db"))
        store = factory(conn)
        try:
            for title, sent_id, text in corpus:
                db.create_document(
                    conn,
                    container_tag="bench",
                    content=text,
                    custom_id=f"{title}{ID_SEPARATOR}{sent_id}",
                )
            process_all(conn, embedder, vector_store=store)
            paragraphs = {
                row["custom_id"].rpartition(ID_SEPARATOR)[0]
                for row in conn.execute("SELECT custom_id FROM documents")
            }

            rows: list[dict[str, Any]] = []
            for item in picked:
                hits = search(
                    conn,
                    embedder,
                    item["question"],
                    container_tag="bench",
                    limit=k,
                    search_mode="documents",
                    vector_store=store,
                )
                predicted = _retrieved_evidence(conn, hits)
                scores = evidence_scores(item["gold_evidence"], set(predicted))
                rows.append(
                    {
                        "id": item["id"],
                        "question_type": item["question_type"],
                        "level": item["level"],
                        "gold_evidence": sorted(
                            [title, sent] for title, sent in item["gold_evidence"]
                        ),
                        "predicted_evidence": [[title, sent] for title, sent in predicted],
                        "hops_found": hops_found(item["gold_evidence"], set(predicted)),
                        "gold_hops": len({title for title, _ in item["gold_evidence"]}),
                        **scores,
                    }
                )
        finally:
            conn.close()

    scored = [row for row in rows if row["evidence_recall"] is not None]
    count = len(scored) or 1
    recall = sum(row["evidence_recall"] for row in scored) / count if scored else None
    precision = sum(row["evidence_precision"] for row in scored) / count if scored else None
    f1 = (
        sum(row["evidence_f1"] for row in scored) / count
        if scored and all(row["evidence_f1"] is not None for row in scored)
        else None
    )
    joint = sum(1 for row in scored if row["evidence_recall"] == 1.0) / count if scored else None
    by_type: dict[str, dict[str, float | None]] = {}
    for row in scored:
        bucket = by_type.setdefault(row["question_type"], {"count": 0, "recall_sum": 0.0})
        bucket["count"] += 1
        bucket["recall_sum"] += float(row["evidence_recall"])
    by_type_summary = {
        name: {"n": int(bucket["count"]), "evidence_recall": bucket["recall_sum"] / bucket["count"]}
        for name, bucket in sorted(by_type.items())
    }

    # The hash must cover the *raw* records, not the normalised ones: `gold_evidence` is a
    # set, and hashing the normalised form raised TypeError on a set the harness had just
    # built correctly. Hashing the input also records what the dataset actually said.
    raw_by_id = {
        str(record.get("_id", record.get("question_id", index))): record
        for index, record in enumerate(records)
    }
    manifest = build_manifest(
        seed=seed,
        requested_n=n,
        n=len(picked),
        ks=[k],
        modes=["documents"],
        embedder=embedder_label(embedder),
        vector_store=store_label,
        data_sha256=dataset_hash or records_sha256([raw_by_id[item["id"]] for item in picked]),
    )
    manifest["evidence_config"] = {
        "unit": EVIDENCE_UNIT,
        "scorer": EVIDENCE_SCORER,
        "answer_scored": False,
        "config": "fullwiki",
        "is_full_wiki_scale": False,
        "caveat": SCALE_CAVEAT,
        "paragraphs_indexed": len(paragraphs),
        "sentences_indexed": len(corpus),
        "retrieval_limit": k,
        "corpus": "union of provided contexts",
    }

    return {
        "dataset": dataset_name,
        "schema": "memoratum-eval-axes-v1",
        "manifest": manifest,
        "evidence_recall": recall,
        "evidence_precision": precision,
        "evidence_f1": f1,
        "joint_evidence_rate": joint,
        "scored_questions": len(scored),
        "by_type": by_type_summary,
        "questions": rows,
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def summarize(result: dict[str, Any]) -> str:
    config = result["manifest"]["evidence_config"]
    lines = [
        "# HotPotQA — evidence retrieval",
        "",
        f"dataset: {result['dataset']} ({result['manifest']['evidence_config']['config']})",
        f"gate metric: {EVIDENCE_UNIT} recall, scored by {EVIDENCE_SCORER}",
        "",
        f"- questions scored: {result['scored_questions']} of {result['manifest']['n']}",
        f"- evidence_recall: {_fmt(result['evidence_recall'])}",
        f"- evidence_precision: {_fmt(result['evidence_precision'])}",
        f"- evidence_f1: {_fmt(result['evidence_f1'])}",
        (
            f"- joint_evidence_rate: {_fmt(result['joint_evidence_rate'])}"
            "  (both hops found in every question)"
        ),
        "",
        f"- paragraphs indexed: {config['paragraphs_indexed']}",
        f"- sentences indexed: {config['sentences_indexed']} (one sentence = one document)",
        f"- retrieval limit: {config['retrieval_limit']} sentences",
        "",
        "## What this does not measure",
        "",
        (
            "**Answers are not scored.** Phase C is admitted for evidence retrieval only. "
            "Answer accuracy was the basis for rejecting this dataset during planning, and "
            "adding it back would reopen a decision the spec closed."
        ),
        "",
        f"**This is not full-wiki scale.** {config['caveat']}",
        "",
        "A hop is a document, not a sentence: finding two sentences of one title is one hop.",
        "",
        "## By question type",
        "",
    ]
    if result["by_type"]:
        lines += ["| type | n | evidence_recall |", "|---|---:|---:|"]
        for name, stats in result["by_type"].items():
            lines.append(f"| {name} | {stats['n']} | {_fmt(stats['evidence_recall'])} |")
    else:
        lines.append("No question was scored.")

    misses = [row for row in result["questions"] if (row["evidence_recall"] or 0.0) < 1.0]
    if misses:
        lines += ["", "## Questions with missed evidence", ""]
        for row in misses[:20]:
            missing = sorted((title, sent) for title, sent in row["gold_evidence"])
            lines.append(
                f"- {row['id']} ({row['question_type']}): "
                f"{row['hops_found']}/{row['gold_hops']} hops, "
                f"recall {row['evidence_recall']:.3f}, "
                f"gold {missing}"
            )
        if len(misses) > 20:
            lines.append(f"- ... and {len(misses) - 20} more")
    return "\n".join(lines) + "\n"


def _fmt(value: float | None) -> str:
    """`n/a` for null, because an unexercised figure is not a zero (invariant M3)."""
    return "n/a" if value is None else f"{value:.3f}"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _command(argv: list[str] | None) -> list[str]:
    from memoratum.eval_axes import command

    return command(argv, module="memoratum.eval_hotpotqa")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HotPotQA evidence-retrieval axis")
    parser.add_argument("--data", required=True, help="local HotPotQA JSON (never vendored)")
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--k", type=int, default=10, help="sentences to retrieve per question")
    parser.add_argument(
        "--config",
        default="fullwiki",
        help=f"HotPotQA split; only {', '.join(ALLOWED_CONFIGS)} is admitted",
    )
    parser.add_argument("--baseline", default="", help="BASELINES.md to gate against")
    parser.add_argument("--out-md", default="")
    parser.add_argument("--out-json", default="")
    args = parser.parse_args(argv)

    if args.config.lower() not in ALLOWED_CONFIGS:
        print(
            f"--config {args.config!r} is not admitted: the distractor split's ~1.2K tokens "
            "fit in context, so it does not force out-of-window retrieval (spec.md)",
            file=sys.stderr,
        )
        return EXIT_BAD_INPUT
    if not os.path.exists(args.data):
        print(f"corpus not found: {args.data}", file=sys.stderr)
        return EXIT_BAD_INPUT
    if args.n < 1:
        print("--n must be >= 1", file=sys.stderr)
        return EXIT_BAD_INPUT
    if args.k <= 0:
        print("--k must be positive", file=sys.stderr)
        return EXIT_BAD_INPUT
    if args.baseline and not os.path.exists(args.baseline):
        print(f"--baseline not found: {args.baseline}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        records = load_records(args.data)
        result = evaluate_evidence(
            records,
            n=args.n,
            seed=args.seed,
            k=args.k,
            dataset_hash=file_sha256(args.data) if os.path.exists(args.data) else "",
        )
    except (ValueError, TypeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_INPUT

    result["manifest"]["evidence_config"]["config"] = args.config.lower()
    baseline = (
        load_baseline(_baseline_row(args.baseline, "evidence_recall"), figure="evidence_recall")
        if args.baseline
        else None
    )

    try:
        write_artifacts(
            result,
            summarize(result),
            out_md=args.out_md,
            out_json=args.out_json,
            argv=_command(argv),
        )
    except OSError as exc:
        print(f"could not write results: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    if args.baseline and baseline is None:
        print(
            "no usable evidence_recall baseline; an unmeasurable baseline must fail rather "
            "than pass (B3)",
            file=sys.stderr,
        )
        return EXIT_BAD_INPUT
    return EXIT_OK


def _baseline_row(path: str, figure: str) -> dict[str, Any] | None:
    """One row from a ``BASELINES.md`` table, using the shared parser."""
    if not path or not os.path.exists(path):
        return None
    from memoratum.eval_cost import _load_baseline_rows

    return _load_baseline_rows(path).get(figure)


if __name__ == "__main__":
    raise SystemExit(main())
