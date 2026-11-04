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
    EXIT_BASELINE_MISSING,
    EXIT_GATE_FAILED,
    EXIT_OK,
    Gate,
    build_manifest,
    embedder_label,
    evaluate_gate,
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


class UnmeasurableGoldError(ValueError):
    """A record's gold evidence cannot be retrieved from the corpus it ships with.

    A subclass of :class:`ValueError` so every existing refusal path keeps working. The
    ``reason`` attribute is what the manifest records, so an exclusion is auditable
    rather than a silent skip (M3).
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def partition_unmeasurable(
    records: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split raw records into measurable ones and gold-outside-context exclusions.

    In the real ``hotpot_dev_fullwiki_v1.json`` the ``context`` field holds the
    dataset's own fullwiki retriever's output, and that retriever misses gold for a
    large fraction of questions (7,706 gold facts across 7,405 questions sit outside
    the provided context). Those questions are **not dataset errors** — they are the
    retrieval condition itself: no memory layer can retrieve evidence that the corpus
    does not contain. They are excluded and counted, never scored 0.0 (which would
    measure the dataset's retriever, not ours) and never refused wholesale (which
    would make the real corpus unrunnable).

    Malformed records are still hard errors: a missing question or a malformed fact is
    a broken file, not an unmeasurable question.
    """
    measurable: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        try:
            # Validate via normalize, but keep the RAW record: the caller normalizes the
            # measurable subset itself, and feeding a normalized dict back through
            # normalize would fail on its own output shape (no `supporting_facts` key).
            normalize_hotpotqa([record])
            measurable.append(record)
        except UnmeasurableGoldError as exc:
            exclusions.append(
                {
                    "id": str(record.get("_id", record.get("question_id", index))),
                    "reason": exc.reason,
                }
            )
        except (ValueError, TypeError) as exc:
            # normalize re-indexes every single-record call, so its "record 0" is a lie
            # here. Re-raise with the true position in the file.
            raise type(exc)(f"record {index}: {exc}") from exc
    return measurable, exclusions


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
      figure — a dataset bug wearing the costume of a retrieval regression. In the real
      ``fullwiki`` file this condition is the dataset's own retriever missing gold, which
      is common rather than exceptional; :func:`partition_unmeasurable` is the opt-in
      path that excludes and counts instead of refusing.
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
        # By this point `supporting_facts` is known non-empty, so an absent or empty
        # context means the gold cannot be in the corpus — the same unmeasurable
        # condition as gold-outside-context, and common in the real fullwiki file
        # (10 records). A context that is present but the wrong shape stays malformed.
        if raw_context is None or (isinstance(raw_context, list) and not raw_context):
            raise UnmeasurableGoldError(
                f"record {index} has no context, so its gold evidence cannot be "
                "retrieved from this corpus",
                reason="empty_context",
            )
        if not isinstance(raw_context, list):
            raise TypeError(f"record {index} has no context")

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
                raise UnmeasurableGoldError(
                    f"record {index}: supporting fact {title!r} is not in the provided "
                    "context, so it cannot be retrieved from this corpus",
                    reason="gold_title_outside_context",
                )
            if not 0 <= sent_id < len(titles[title]):
                raise UnmeasurableGoldError(
                    f"record {index}: supporting fact {title!r} sentence {sent_id} is not "
                    f"in the provided context (it has {len(titles[title])})",
                    reason="gold_sentence_out_of_range",
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
) -> tuple[list[tuple[str, int]], int]:
    """Map hits to ``(title, sent_id)``, in rank order, de-duplicated.

    A hit that resolves to no document is **dropped and counted**, never assumed to be
    evidence: an unresolvable hit has not been shown to belong to the corpus. The second
    return value is how many hits were dropped, which the gate treats as a failure —
    the same rule the cost axis applies to ``unresolvable_hits`` (A1).
    """
    seen: list[tuple[str, int]] = []
    dropped = 0
    for hit in hits:
        hit_id = str(hit.get("id", ""))
        if not hit_id.startswith("chunk_"):
            dropped += 1
            continue
        try:
            chunk_id = int(hit_id.split("_", 1)[1])
        except ValueError:
            dropped += 1
            continue
        row = conn.execute(
            "SELECT d.custom_id FROM chunks c JOIN documents d ON d.id = c.document_id"
            " WHERE c.id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            dropped += 1
            continue
        try:
            pair = _split_id(str(row["custom_id"]))
        except ValueError:
            dropped += 1
            continue
        if pair not in seen:
            seen.append(pair)
    return seen, dropped


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
    baseline: dict[str, Any] | None = None,
    allow_unmeasurable_gold: bool = False,
) -> dict[str, Any]:
    """Score sentence-level evidence retrieval over the union of provided contexts.

    With ``allow_unmeasurable_gold`` the questions whose gold evidence the provided
    context does not contain are excluded and counted (see
    :func:`partition_unmeasurable`); without it the run refuses, which is the right
    behaviour for a corpus that promises gold-in-context.
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")

    if allow_unmeasurable_gold:
        measurable, exclusions = partition_unmeasurable(records)
        normalized = normalize_hotpotqa(measurable)
    else:
        normalized = normalize_hotpotqa(records)
        exclusions = []
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
            ghost_total = 0
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
                predicted, ghost = _retrieved_evidence(conn, hits)
                ghost_total += ghost
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
    dataset_total = len(records)
    excluded = len(exclusions)
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
        "dataset_records": dataset_total,
        "unmeasurable_gold_excluded": excluded,
        "unmeasurable_gold_rate": (excluded / dataset_total) if dataset_total else None,
        "allow_unmeasurable_gold": allow_unmeasurable_gold,
    }

    return {
        "axis": "evidence",
        "dataset": dataset_name,
        "schema": "memoratum-eval-axes-v1",
        "manifest": manifest,
        "gate": gate_from_run(
            scored=len(scored),
            ghost=ghost_total,
            recall=recall,
            baseline=baseline,
        ),
        "evidence_recall": recall,
        "evidence_precision": precision,
        "evidence_f1": f1,
        "joint_evidence_rate": joint,
        "scored_questions": len(scored),
        "by_type": by_type_summary,
        "questions": rows,
    }


def gate_from_run(
    *,
    scored: int,
    ghost: int,
    recall: float | None,
    baseline: dict[str, Any] | None,
) -> dict[str, Any]:
    """The absolute checks, plus the baseline floor when a baseline is supplied.

    Isolation gates on ``leaked_hits == 0`` and grounding on ``grounded_fraction == 1.0``
    without any baseline row (``BASELINES.md``), because some guarantees are facts about
    the run rather than comparisons with history. The evidence axis gates the same way:
    zero scored questions measured nothing, a ghost hit is an ungrounded retrieval, and a
    null recall is a failure rather than a skip (M3).
    """
    gate = Gate()
    gate.check(
        "scored_questions",
        value=scored or None,
        bound=0,
        comparison="<=",
        reason="no question was scored, so the axis measured nothing",
    )
    gate.check(
        "ghost_hits",
        value=ghost,
        bound=0,
        comparison=">",
        reason="a hit whose id resolves to no document has not been shown to be evidence",
    )
    gate.check(
        "evidence_recall",
        value=recall,
        bound=0.0,
        comparison="<",
        reason="recall is null; a skipped metric must never read as a pass",
    )
    usable = load_baseline(baseline, figure="evidence_recall") if baseline is not None else None
    if usable is not None:
        # A recall baseline is a FLOOR: the delta reports how far below the baseline the
        # run fell, and the failure is on the "<" direction. compare_to_baseline encodes
        # the ceiling direction ("value > bound fails") used by cost, so the floor is
        # spelled here rather than parameterised there.
        bound = float(usable["value"]) * (1.0 - float(usable.get("tolerance", 0.0) or 0.0))
        delta = None if recall is None else float(recall) - float(usable["value"])
        gate.check(
            "evidence_recall_vs_baseline",
            value=recall,
            bound=bound,
            comparison="<",
            delta=delta,
        )
    return evaluate_gate(gate)


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
        "## What this figure is conditional on",
        "",
    ]
    excluded = config.get("unmeasurable_gold_excluded", 0)
    total = config.get("dataset_records", 0)
    rate = config.get("unmeasurable_gold_rate")
    rate_shown = "n/a" if rate is None else f"{rate:.1%}"
    lines += [
        (
            f"- questions excluded (gold outside the provided context): {excluded} of "
            f"{total} ({rate_shown})"
        ),
        "",
        (
            f"The figures above are **conditional**: they hold for the "
            f"{result['scored_questions']} questions where the dataset's own fullwiki "
            "retrieval placed every gold fact inside the provided context. Questions "
            "where it did not are excluded and counted above, never scored 0.0 — "
            "scoring them would measure the dataset's retriever, not the memory layer "
            "under test."
        ),
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
    parser.add_argument(
        "--allow-unmeasurable-gold",
        action="store_true",
        help=(
            "exclude and count questions whose gold evidence the provided context does "
            "not contain, instead of refusing the file (the real fullwiki corpus needs "
            "this: its own retriever misses gold often)"
        ),
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
    baseline_row = _baseline_row(args.baseline, "evidence_recall") if args.baseline else None
    if args.baseline and baseline_row is None:
        print(
            "no usable evidence_recall baseline; an unmeasurable baseline must fail rather "
            "than pass (B3)",
            file=sys.stderr,
        )
        return EXIT_BASELINE_MISSING

    try:
        records = load_records(args.data)
        result = evaluate_evidence(
            records,
            n=args.n,
            seed=args.seed,
            k=args.k,
            dataset_hash=file_sha256(args.data) if os.path.exists(args.data) else "",
            baseline=baseline_row,
            allow_unmeasurable_gold=args.allow_unmeasurable_gold,
        )
    except (ValueError, TypeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BAD_INPUT

    result["manifest"]["evidence_config"]["config"] = args.config.lower()

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

    gate = result.get("gate") or {}
    if str(gate.get("status")) == "fail":
        for check in gate.get("checks", []):
            if check.get("status") != "pass":
                reason = f" — {check['reason']}" if check.get("reason") else ""
                print(
                    f"gate: {check['name']} = {check.get('value')!r} "
                    f"(bound {check.get('bound')!r}){reason}",
                    file=sys.stderr,
                )
        return EXIT_GATE_FAILED
    return EXIT_OK


def _baseline_row(path: str, figure: str) -> dict[str, Any] | None:
    """One row from a ``BASELINES.md`` table, using the shared parser."""
    if not path or not os.path.exists(path):
        return None
    from memoratum.eval_cost import _load_baseline_rows

    return _load_baseline_rows(path).get(figure)


if __name__ == "__main__":
    raise SystemExit(main())
