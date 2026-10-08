"""HotPotQA evidence-retrieval contract (RED).

Phase C of feature `002-evaluation-harness`, admitted for **evidence retrieval only**
(`spec.md`). The spec rejected HotPotQA for *answering* — that reasoning is correct and is
kept — but it ships `supporting_facts` alongside a pure-stdlib deterministic scorer, so the
gold evidence can be graded without a judge and without a new dependency.

The rules encoded here are the ones that make the metric honest:

* **Sentence-level, never answer-level.** No answerer exists in this module. Answer accuracy
  was the basis for the original rejection and adding it back would reopen a decision the
  spec closed.
* **One sentence, one document, one addressable id.** `(title, sent_id)` is the unit of gold
  evidence, so it must be the unit of retrieval. If a chunk held several sentences, evidence
  would have to be matched by text similarity — the fuzzy step that made containment unusable
  as a grounding signal (``data-model.md`` G3).
* **Exact set arithmetic.** Recall, precision and F1 over `(title, sent_id)` pairs. No
  threshold, no tolerance, no normalisation fudge.
* **The distractor load is the point.** ``distractor`` is not acceptable because its ~1.2K
  tokens do not force out-of-window retrieval; the union of provided contexts is indexed
  instead, so a question competes with paragraphs belonging to other questions.
* **The caveat travels with the figure.** This is multi-hop evidence recall under a ~74K
  paragraph load, *not* full-wiki scale (~5M articles). A manifest that does not say so lets
  a reader believe the stronger claim.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO_ROOT, "eval", "fixtures", "hotpotqa_evidence_smoke.json")

_TEMP_ROOTS: list[tempfile.TemporaryDirectory] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_temp_roots():
    yield
    while _TEMP_ROOTS:
        _TEMP_ROOTS.pop().cleanup()


def _tempdir(prefix: str = "memoratum-hotpotqa-") -> str:
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    _TEMP_ROOTS.append(holder)
    return holder.name


from memoratum.eval_datasets import load_records
from memoratum.eval_hotpotqa import evidence_scores, hops_found


def _records() -> list[dict]:
    return load_records(FIXTURE)


def _evaluate(**kwargs):
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_hotpotqa import evaluate_evidence

    options: dict = {
        "n": 2,
        "seed": 42,
        "k": 5,
        "embedder": HashEmbedder(dims=64),
    }
    options.update(kwargs)
    return evaluate_evidence(_records(), **options)


# --- the corpus is sentence-addressable --------------------------------------------


def test_each_sentence_becomes_one_document() -> None:
    """`(title, sent_id)` must be retrievable as a unit, or evidence needs fuzzy matching."""
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_hotpotqa import build_sentence_corpus
    from memoratum.ingest import process_all

    corpus = build_sentence_corpus(_records())
    conn = db.connect(os.path.join(_tempdir(), "corpus.db"))
    try:
        for title, sent_id, text in corpus:
            db.create_document(
                conn,
                container_tag="bench",
                content=text,
                custom_id=f"{title}::{sent_id}",
            )
        process_all(conn, HashEmbedder(dims=64))
        ids = {row["custom_id"] for row in conn.execute("SELECT custom_id FROM documents")}
    finally:
        conn.close()

    assert "Northern Observatory::0" in ids
    assert "City of Aldmere::2" in ids, "the second hop's gold sentence must be a unit"
    assert len(ids) == len(corpus), (
        f"{len(corpus)} sentences produced {len(ids)} documents; a duplicate id would make "
        "one gold sentence unretrievable and silently cap recall"
    )


def test_the_same_sentence_from_two_questions_is_indexed_once() -> None:
    """A shared paragraph must not become two rows with the same id.

    `create_document` keys on `custom_id`, so a duplicate silently overwrites. If the two
    copies differed, the corpus would hold whichever was written last and the manifest's
    paragraph count would overstate the truth.
    """
    from memoratum.eval_hotpotqa import build_sentence_corpus

    records = [
        {
            "_id": "a",
            "question": "q",
            "supporting_facts": [["T", 0]],
            "context": [["T", ["one", "two"]]],
        },
        {
            "_id": "b",
            "question": "q",
            "supporting_facts": [["T", 1]],
            "context": [["T", ["one", "two"]]],
        },
    ]
    corpus = build_sentence_corpus(records)
    ids = [f"{title}::{sent}" for title, sent, _ in corpus]
    assert len(ids) == len(set(ids)) == 2, ids


def test_a_sentence_shared_across_questions_must_agree() -> None:
    """Identical ids with different text is a contradiction, not a duplicate.

    Silently taking the last write would make evidence retrieval score against text that was
    never indexed under that id in the first place.
    """
    from memoratum.eval_hotpotqa import build_sentence_corpus

    records = [
        {"_id": "a", "question": "q", "supporting_facts": [["T", 0]], "context": [["T", ["one"]]]},
        {
            "_id": "b",
            "question": "q",
            "supporting_facts": [["T", 0]],
            "context": [["T", ["DIFFERENT"]]],
        },
    ]
    with pytest.raises(ValueError, match="two different texts"):
        build_sentence_corpus(records)


# --- exact set arithmetic -----------------------------------------------------------


def test_perfect_retrieval_scores_one() -> None:
    """The premise: a clean run must actually reach 1.0, or the scale is wrong."""
    gold = {("T", 0), ("U", 2)}
    predicted = {("T", 0), ("U", 2)}
    scores = evidence_scores(gold, predicted)
    assert scores["evidence_recall"] == pytest.approx(1.0)
    assert scores["evidence_precision"] == pytest.approx(1.0)
    assert scores["evidence_f1"] == pytest.approx(1.0)


def test_missing_evidence_lowers_recall_and_leaves_precision() -> None:
    gold = {("T", 0), ("U", 2)}
    scores = evidence_scores(gold, {("T", 0)})
    assert scores["evidence_recall"] == pytest.approx(0.5)
    assert scores["evidence_precision"] == pytest.approx(1.0), (
        "returning only correct evidence is precise; recall and precision are different "
        "questions and a single score hides the difference"
    )
    assert scores["evidence_f1"] == pytest.approx(2 / 3)


def test_no_predictions_gives_zero_precision_not_a_division_error() -> None:
    scores = evidence_scores({("T", 0)}, set())
    assert scores["evidence_recall"] == pytest.approx(0.0)
    assert scores["evidence_precision"] == pytest.approx(0.0), (
        "retrieving nothing is maximally imprecise, not undefined"
    )


def test_empty_gold_gives_null_not_a_perfect_score() -> None:
    """No gold evidence means the question was not exercised, and 1.0 would read as a pass."""
    scores = evidence_scores(set(), {("T", 0)})
    assert scores["evidence_recall"] is None
    assert scores["evidence_precision"] == pytest.approx(0.0)
    assert scores["evidence_f1"] is None


def test_extra_wrong_evidence_lowers_precision() -> None:
    gold = {("T", 0)}
    scores = evidence_scores(gold, {("T", 0), ("X", 5), ("Y", 6)})
    assert scores["evidence_recall"] == pytest.approx(1.0)
    assert scores["evidence_precision"] == pytest.approx(1 / 3)
    assert scores["evidence_f1"] == pytest.approx(0.5)


def test_a_duplicate_prediction_does_not_inflate_precision() -> None:
    """Repeating the same gold sentence is still one unit of evidence."""
    scores = evidence_scores({("T", 0)}, {("T", 0), ("T", 0)})
    assert scores["evidence_precision"] == pytest.approx(1.0), (
        "duplicates must be collapsed before counting, or a retriever can pad its score by "
        "repeating one hit"
    )
    assert scores["evidence_recall"] == pytest.approx(1.0)


# --- multi-hop is the actual claim ---------------------------------------------------


def test_finding_one_hop_only_is_not_a_multi_hop_success() -> None:
    """The point of HotPotQA: both hops, or the question is not answered.

    A plain recall average credits half-credit questions, which is exactly the shape that
    makes a bridge-retrieval regression look like a small dip.
    """
    gold = {("A", 0), ("B", 1)}
    assert hops_found(gold, {("A", 0)}) == 1
    assert hops_found(gold, {("A", 0), ("B", 1)}) == 2
    assert hops_found(gold, {("A", 0), ("B", 1), ("C", 2)}) == 2


def test_two_sentences_from_one_title_count_as_one_hop() -> None:
    """A hop is a *document*, not a sentence.

    HotPotQA's two supporting facts usually come from two different titles; finding two
    sentences of the same title is one hop, and calling it two would let a retriever win the
    multi-hop metric with a single paragraph.
    """
    assert hops_found({("A", 0), ("A", 1)}, {("A", 0), ("A", 1)}) == 1


# --- the run ------------------------------------------------------------------------


def test_a_run_reports_the_four_evidence_figures() -> None:
    result = _evaluate()
    for figure in ("evidence_recall", "evidence_precision", "evidence_f1"):
        assert figure in result, sorted(result)
        assert 0.0 <= result[figure] <= 1.0


def test_a_run_reports_the_multi_hop_rate() -> None:
    """Aggregate recall alone cannot distinguish "found both hops" from "found one twice"."""
    result = _evaluate()
    assert "joint_evidence_rate" in result, sorted(result)
    assert 0.0 <= result["joint_evidence_rate"] <= 1.0


def test_every_question_reports_its_gold_and_predicted_sets() -> None:
    """A score without the sets cannot be checked, and an uncheckable score is a rumour."""
    result = _evaluate()
    for row in result["questions"]:
        assert row["gold_evidence"], row
        assert "predicted_evidence" in row
        assert row["hops_found"] >= 1
        for title, sent_id in row["gold_evidence"]:
            assert isinstance(title, str) and isinstance(sent_id, int), row


def test_retrieval_is_a_subset_of_the_indexed_corpus() -> None:
    """A predicted id that is not a document is an ungrounded hit, and must not count."""
    from memoratum.eval_hotpotqa import build_sentence_corpus

    corpus = {f"{title}::{sent}" for title, sent, _ in build_sentence_corpus(_records())}
    result = _evaluate(k=5)
    for row in result["questions"]:
        for predicted in row["predicted_evidence"]:
            assert f"{predicted[0]}::{predicted[1]}" in corpus, predicted


def test_a_larger_k_cannot_reduce_recall() -> None:
    """Retrieval is a prefix operation: raising k only adds candidates.

    If this fails, the scorer or the ranking is order-dependent, and the metric is not
    measuring retrieval.
    """
    small = _evaluate(k=1)
    large = _evaluate(k=20)
    assert large["evidence_recall"] >= small["evidence_recall"] - 1e-9


def test_the_same_run_twice_gives_identical_results() -> None:
    first, second = _evaluate(), _evaluate()
    assert first["questions"] == second["questions"]
    assert {k: v for k, v in first.items() if k != "manifest"} == {
        k: v for k, v in second.items() if k != "manifest"
    }


# --- the manifest must carry the caveat ---------------------------------------------


def test_the_manifest_records_the_distractor_load_not_the_name() -> None:
    """`fullwiki` in a filename is not provenance. The actual scale must be recorded."""
    result = _evaluate()
    config = result["manifest"]["evidence_config"]
    assert config["paragraphs_indexed"] > 0
    assert config["sentences_indexed"] > 0
    assert config["is_full_wiki_scale"] is False, (
        "indexing the union of provided contexts is NOT full-wiki scale; recording it as "
        "such would let a reader believe the stronger claim"
    )
    assert "not" in config["caveat"].lower()


def test_the_manifest_records_the_scorer_and_the_units() -> None:
    """A score with no stated unit is not interpretable."""
    result = _evaluate()
    config = result["manifest"]["evidence_config"]
    assert config["unit"] == "(title, sent_id)"
    assert config["scorer"] == "exact-set"
    assert config["answer_scored"] is False, "answer scoring was rejected during planning"


def test_the_manifest_records_the_dataset_hash() -> None:
    result = _evaluate()
    assert result["manifest"]["data_sha256"], "a figure with no provenance is unanchored"


# --- rejections ----------------------------------------------------------------------


def test_a_record_without_supporting_facts_is_rejected() -> None:
    """A question with no gold evidence cannot be graded, and must not score 1.0."""
    from memoratum.eval_hotpotqa import normalize_hotpotqa

    with pytest.raises(ValueError, match="supporting_facts"):
        normalize_hotpotqa([{"_id": "x", "question": "q", "context": [["T", ["s"]]]}])


def test_a_supporting_fact_pointing_outside_the_context_is_rejected() -> None:
    """Gold that cannot be retrieved is a dataset bug, and silently scoring it 0.0 hides it."""
    from memoratum.eval_hotpotqa import normalize_hotpotqa

    with pytest.raises(ValueError, match="not in the provided context"):
        normalize_hotpotqa(
            [
                {
                    "_id": "x",
                    "question": "q",
                    "supporting_facts": [["Missing", 0]],
                    "context": [["T", ["s"]]],
                }
            ]
        )


def test_an_out_of_range_sentence_index_is_rejected() -> None:
    from memoratum.eval_hotpotqa import normalize_hotpotqa

    with pytest.raises(ValueError, match="not in the provided context"):
        normalize_hotpotqa(
            [
                {
                    "_id": "x",
                    "question": "q",
                    "supporting_facts": [["T", 9]],
                    "context": [["T", ["only one"]]],
                }
            ]
        )


def test_a_nonpositive_k_is_rejected() -> None:
    with pytest.raises(ValueError):
        _evaluate(k=0)


def test_a_nonpositive_n_is_rejected() -> None:
    with pytest.raises(ValueError):
        _evaluate(n=0)


# --- no answerer exists --------------------------------------------------------------


def test_the_module_exposes_no_answer_scoring_api() -> None:
    """The original rejection of HotPotQA was for answering. Adding one would reopen it.

    Checked on the exported names rather than on the source text. An earlier version of this
    test grepped the module source for the word "answerer" and failed on the docstring that
    explains why there is none -- a prose false positive is worse than no check, because it
    invites the fix of deleting the explanation.
    """
    from memoratum import eval_hotpotqa

    forbidden = ("answer", "exact_match", "f1_score", "normalize_answer", "prediction")
    exported = [name for name in dir(eval_hotpotqa) if not name.startswith("_")]
    offenders = [name for name in exported if any(token in name.lower() for token in forbidden)]
    assert not offenders, (
        f"eval_hotpotqa exports {offenders}, which looks like answer scoring; Phase C is "
        "admitted for evidence retrieval only"
    )
    assert "evidence_scores" in exported
    assert "hops_found" in exported


def test_the_answer_field_cannot_influence_any_reported_figure() -> None:
    """Empirical proof that the answer is never read, rather than a promise about it.

    The same two questions are run twice: once carrying HotPotQA's `answer` field, once with
    it replaced by a different string. Every reported figure must be identical. A text grep
    cannot establish this -- it flags the docstring that explains the constraint, and it
    cannot see a comparison added inside a helper.
    """
    import copy

    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_hotpotqa import evaluate_evidence

    original = load_records(FIXTURE)
    poisoned = copy.deepcopy(original)
    for record in poisoned:
        record["answer"] = "a completely different answer"

    options = {"n": 2, "seed": 42, "k": 5, "embedder": HashEmbedder(dims=64)}
    with_answer = evaluate_evidence(original, **options)
    with_other = evaluate_evidence(poisoned, **options)

    assert with_answer["questions"] == with_other["questions"], (
        "changing the answer field changed the evidence results, so it is being read"
    )
    for figure in ("evidence_recall", "evidence_precision", "evidence_f1", "joint_evidence_rate"):
        assert with_answer[figure] == with_other[figure], figure


def test_dropping_the_answer_field_entirely_changes_nothing() -> None:
    """The converse: a record with no answer at all must run identically."""
    import copy

    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_hotpotqa import evaluate_evidence

    stripped = copy.deepcopy(load_records(FIXTURE))
    for record in stripped:
        record.pop("answer", None)

    options = {"n": 2, "seed": 42, "k": 5, "embedder": HashEmbedder(dims=64)}
    assert (
        evaluate_evidence(load_records(FIXTURE), **options)["questions"]
        == (evaluate_evidence(stripped, **options)["questions"])
    )


def test_the_report_states_that_answers_are_not_scored() -> None:
    from memoratum.eval_hotpotqa import summarize

    result = _evaluate()
    report = summarize(result)
    lowered = report.lower()
    assert "evidence" in lowered
    assert "answer" in lowered, "the report must say what it does not measure"
    assert "not" in lowered
    assert "full-wiki" in lowered or "full wiki" in lowered, (
        "the report must carry the scale caveat, not only the manifest"
    )


# --- CLI ------------------------------------------------------------------------------


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from memoratum import eval_hotpotqa

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = eval_hotpotqa.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_cli_runs_and_writes_artifacts(tmp_path) -> None:
    js, md = tmp_path / "evidence.json", tmp_path / "RESULTS-evidence.md"
    code, _, err = _run_cli(
        [
            "--data",
            FIXTURE,
            "--n",
            "2",
            "--seed",
            "42",
            "--k",
            "5",
            "--out-json",
            str(js),
            "--out-md",
            str(md),
        ]
    )
    assert code == 0, err
    payload = json.loads(js.read_text())
    assert payload["dataset"] == "hotpotqa-evidence"
    assert payload["evidence_recall"] is not None
    assert md.exists()
    assert "Regenerate this figure" in md.read_text(), "SC-003: the command must be recorded"


def test_cli_rejects_a_missing_corpus() -> None:
    code, _, err = _run_cli(["--data", "/nonexistent/hotpotqa.json", "--n", "1"])
    assert code != 0
    assert "corpus not found" in err


def test_cli_rejects_the_distractor_config() -> None:
    """The spec rejects `distractor`: its token count does not force out-of-window retrieval.

    Refused by flag rather than by convention, so the rejection cannot be quietly dropped.
    """
    code, _, err = _run_cli(["--data", FIXTURE, "--n", "1", "--config", "distractor"])
    assert code != 0
    assert "distractor" in err.lower()


def test_cli_rejects_a_nonpositive_k() -> None:
    code, _, err = _run_cli(["--data", FIXTURE, "--n", "1", "--k", "0"])
    assert code != 0
    assert "--k" in err


# --- gaps found by mutation testing -------------------------------------------------
# Each test below was added because a mutant survived. The mutant that motivated it is named
# in its docstring, so a future reader can reproduce the gap rather than re-deriving it.


def test_the_joint_rate_counts_only_complete_evidence() -> None:
    """Mutant: `evidence_recall > 0` instead of `== 1.0`.

    The joint rate is the multi-hop claim, and "found something" is not "found both hops".
    With this fixture neither question reaches recall 1.0 at k=5, so the rate must be 0.0 —
    a mutant crediting partial successes would report a non-zero multi-hop rate for a run
    that found no complete question.
    """
    result = _evaluate(k=5)
    complete = [row for row in result["questions"] if row["evidence_recall"] == 1.0]
    assert complete == [], "the fixture's premise is that no question is fully answered at k=5"
    assert result["joint_evidence_rate"] == pytest.approx(0.0), (
        "a question missing any gold evidence must not count toward the joint rate"
    )


def test_the_aggregates_match_the_per_question_rows() -> None:
    """Mutant: the aggregate recall hard-coded to 1.0.

    A hard-coded aggregate is indistinguishable from a real one whenever the true value
    happens to be 1.0, so the aggregate is recomputed from the rows here.
    """
    result = _evaluate(k=5)
    scored = [row for row in result["questions"] if row["evidence_recall"] is not None]
    assert scored, "the premise is that something was scored"
    expected_recall = sum(row["evidence_recall"] for row in scored) / len(scored)
    expected_precision = sum(row["evidence_precision"] for row in scored) / len(scored)
    expected_joint = sum(1 for row in scored if row["evidence_recall"] == 1.0) / len(scored)
    assert result["evidence_recall"] == pytest.approx(expected_recall)
    assert result["evidence_precision"] == pytest.approx(expected_precision)
    assert result["joint_evidence_rate"] == pytest.approx(expected_joint)


def test_the_retrieval_limit_bounds_the_number_of_predictions() -> None:
    """Mutant: `limit=k` replaced with a hard-coded 10.

    Monotonicity alone cannot catch this — a fixed limit is still monotone in k. What catches
    it is that the number of predictions is bounded by the limit actually requested.
    """
    for k in (1, 2, 3):
        result = _evaluate(k=k)
        for row in result["questions"]:
            assert len(row["predicted_evidence"]) <= k, (
                f"k={k} returned {len(row['predicted_evidence'])} predictions; the flag is "
                "not reaching the search"
            )
    small = _evaluate(k=1)
    large = _evaluate(k=6)
    assert any(
        len(large["questions"][i]["predicted_evidence"])
        > len(small["questions"][i]["predicted_evidence"])
        for i in range(len(small["questions"]))
    ), "raising k added no candidates at all"


def test_an_unresolvable_hit_is_dropped_rather_than_counted_as_evidence() -> None:
    """Mutant: the `row is None` guard removed.

    A hit whose chunk id resolves to no document has not been shown to belong to the corpus.
    Scoring it as evidence would credit a retriever with sentences that were never indexed.
    """
    from memoratum.eval_hotpotqa import _retrieved_evidence

    conn = _connect()
    try:
        assert _retrieved_evidence(conn, [{"id": "chunk_999999", "chunk": "ghost"}]) == []
        assert _retrieved_evidence(conn, [{"id": "not-a-chunk", "chunk": "x"}]) == []
        assert _retrieved_evidence(conn, [{"id": "chunk_nan", "chunk": "x"}]) == []
        real = _first_chunk_id(conn)
        assert _retrieved_evidence(conn, [{"id": f"chunk_{real}", "chunk": "x"}]) != []
    finally:
        conn.close()


def test_every_requested_question_is_evaluated() -> None:
    """Mutant: the evaluation loop truncated to `picked[:1]`.

    The fixture has two questions. Evaluating one and reporting `n = 2` would be a figure
    describing work that was not done.
    """
    result = _evaluate()
    assert len(result["questions"]) == 2, (
        f"{len(result['questions'])} of 2 questions were evaluated while the manifest claims "
        f"n={result['manifest']['n']}"
    )
    assert {row["id"] for row in result["questions"]} == {"fixture-q1", "fixture-q2"}


def test_the_report_says_answers_are_not_scored_by_that_phrase() -> None:
    """Mutant: the "Answers are not scored" paragraph deleted.

    The previous assertion checked for the words "answer" and "not" anywhere in the report,
    which the scale caveat also supplies — so deleting the actual claim still passed. Now
    the claim itself is asserted, by its own phrase.
    """
    from memoratum.eval_hotpotqa import summarize

    report = summarize(_evaluate())
    assert "Answers are not scored" in report, (
        "the report must state, in its own words, that answers are not scored"
    )
    assert "evidence retrieval only" in report


def _connect():
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_hotpotqa import build_sentence_corpus
    from memoratum.ingest import process_all

    conn = db.connect(os.path.join(_tempdir(), "unresolvable.db"))
    for title, sent_id, text in build_sentence_corpus(_records()):
        db.create_document(
            conn, container_tag="bench", content=text, custom_id=f"{title}::{sent_id}"
        )
    process_all(conn, HashEmbedder(dims=64))
    return conn


def _first_chunk_id(conn) -> int:
    return conn.execute("SELECT MIN(id) AS id FROM chunks").fetchone()["id"]
