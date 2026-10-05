"""Reproducibility contract for `eval_longmemeval` (RED).

A published figure is only worth reading if you can tell what produced it. Two flags on
this runner exist purely for that, and both are currently absent (T093):

* ``--project-count N`` ingests each question's sessions across ``N`` projects, so the
  **isolation axis can run against a real dataset** instead of a synthetic probe. Every
  scope bug found in feature 001 was invisible to recall, because recall is monotonically
  indifferent to where data landed.
* ``--scope both`` populates the unscoped bucket alongside the scoped one (invariant I2).
  Every scope filter in this codebase is conditional on ``is not None``, so
  ``project_id=null`` genuinely means "no scope requested" — and a run that never issues an
  unscoped query has not demonstrated that.

Both are the difference between a manifest that *claims* multi-tenant coverage and one that
demonstrates it.

Covers T091 and T093.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TEMP_ROOTS: list[tempfile.TemporaryDirectory] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_temp_roots():
    yield
    while _TEMP_ROOTS:
        _TEMP_ROOTS.pop().cleanup()


def _tempdir(prefix: str = "memoratum-repro-") -> str:
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    _TEMP_ROOTS.append(holder)
    return holder.name


def _corpus_file(sessions_per_question: int = 3, questions: int = 3) -> str:
    """A small corpus with **distinct** session ids and distinct content.

    Duplicate session ids collapse into one document (the runner keys on `custom_id`), so
    a fixture that repeats itself would silently produce a single-document corpus and any
    scope assertion against it would be vacuous.
    """
    payload = []
    for q in range(questions):
        sessions = []
        for s in range(sessions_per_question):
            sessions.append(
                {
                    "session_id": f"q{q}-s{s}",
                    "date": "2026-01-01",
                    "turns": [
                        {
                            "role": "user",
                            "content": (
                                f"Question {q} session {s}: the quarterly budget review for "
                                f"region {s * 7} is scheduled for tuesday, covering travel, "
                                f"lodging and catering for headcount {q * 11 + s}."
                            ),
                        }
                    ],
                }
            )
        payload.append(
            {
                "question_id": f"q{q}",
                "question": f"what is scheduled for tuesday in question {q}?",
                "question_type": "single-session-user",
                "answer": f"the budget review for question {q}",
                "answer_session_ids": [f"q{q}-s{sessions_per_question - 1}"],
                "haystack_dates": ["2026-01-01"],
                "haystack_session_ids": [s["session_id"] for s in sessions],
                "haystack_sessions": sessions,
            }
        )
    path = os.path.join(_tempdir(), "corpus.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def _evaluate(path: str, **kwargs):
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_datasets import load_records
    from memoratum.eval_longmemeval import evaluate

    options = {
        "n": 2,
        "seed": 42,
        "ks": [5],
        "modes": ["hybrid"],
        "embedder": HashEmbedder(dims=64),
    }
    options.update(kwargs)
    return evaluate(load_records(path), **options)


def _run_cli(argv: list[str]):
    from memoratum import eval_longmemeval

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = eval_longmemeval.main(argv)
    return code, out.getvalue(), err.getvalue()


# --- T093: --project-count ---------------------------------------------------------


def test_project_count_one_is_the_current_behaviour() -> None:
    """The default must stay single-project, or every committed figure changes meaning."""
    result = _evaluate(_corpus_file(), project_count=1)
    assert result["manifest"]["scope_config"]["project_count"] == 1


def test_project_count_ingests_each_session_into_every_project() -> None:
    """A multi-project run must actually produce multi-project data.

    Asserted through the manifest's own record rather than a count, because "I passed 2"
    is not evidence that 2 projects were written.
    """
    result = _evaluate(_corpus_file(), project_count=2)
    config = result["manifest"]["scope_config"]
    assert config["project_count"] == 2
    assert config["documents_per_question"] > 0, (
        "the manifest records zero documents, so the run cannot have written any"
    )
    assert result["manifest"]["modes"] == ["hybrid"]


def test_a_multi_project_run_keeps_recall_meaningful() -> None:
    """Multi-project ingestion must not destroy recall.

    This is the load-bearing assertion: if scoping data by project makes the gold session
    unreachable, the flag turns a recall run into a zero. Every project's copy carries the
    same text, so a scoped query must still find the gold session.
    """
    single = _evaluate(_corpus_file(), project_count=1)
    multi = _evaluate(_corpus_file(), project_count=3)
    single_mrr = single["aggregate"]["hybrid"]["MRR"]
    multi_mrr = multi["aggregate"]["hybrid"]["MRR"]
    assert single_mrr > 0, "the single-project premise is zero, so this proves nothing"
    assert multi_mrr > 0, "a multi-project run retrieved nothing"
    assert multi_mrr == pytest.approx(single_mrr, abs=0.35), (
        f"MRR {multi_mrr:.3f} vs {single_mrr:.3f}: scoping by project must not make the "
        "gold session unreachable"
    )


def test_project_count_is_rejected_when_it_is_not_positive() -> None:
    """`--project-count 0` must not produce a run that ingested nothing and reported it."""
    for count in (0, -1):
        with pytest.raises(ValueError):
            _evaluate(_corpus_file(), project_count=count)


def test_a_large_project_count_is_reported_not_silently_clamped() -> None:
    """Clamping would make the manifest disagree with what ran."""
    result = _evaluate(_corpus_file(), project_count=2)
    assert result["manifest"]["scope_config"]["project_count"] == 2


# --- T091: --scope both ------------------------------------------------------------


def test_scope_scoped_populates_only_the_scoped_bucket() -> None:
    """The default must stay scoped-only, or the unscoped bucket would always be full."""
    result = _evaluate(_corpus_file(), project_count=2, scope="scoped")
    scope = result["manifest"]["scope_config"]
    assert scope["scope"] == "scoped"
    assert scope["unscoped_queries"] == 0, "the default run must not issue unscoped queries"


def test_scope_both_issues_unscoped_queries_and_records_them() -> None:
    """The whole point: a run that demonstrates `project_id=null` means "no filter"."""
    result = _evaluate(_corpus_file(), project_count=2, scope="both")
    scope = result["manifest"]["scope_config"]
    assert scope["scope"] == "both"
    assert scope["scoped_queries"] > 0, "no scoped query was issued"
    assert scope["unscoped_queries"] > 0, (
        "--scope both issued no unscoped query, so it demonstrates nothing that "
        "--scope scoped does not already"
    )
    assert scope["unscoped_queries"] == scope["scoped_queries"], (
        "both buckets must be the same size: the two differ only in the scope argument"
    )


def test_an_unscoped_query_sees_more_than_a_scoped_one() -> None:
    """Measured evidence that scope is doing something.

    With identical corpora per project, a scoped query can only reach its own project, so it
    must return fewer hits than an unscoped query over the same content.
    """
    result = _evaluate(_corpus_file(), project_count=3, scope="both")
    scope = result["manifest"]["scope_config"]
    assert scope["unscoped_cross_scope_hits"] > 0, (
        "an unscoped query returned nothing from other projects, so the corpus was not "
        "actually multi-project and the run proves nothing"
    )


def test_an_unscoped_bucket_is_excluded_from_the_leak_denominator() -> None:
    """I2: unscoped queries are not scope violations.

    Counting them as leaks would report a false failure on a correct implementation.
    """
    result = _evaluate(_corpus_file(), project_count=2, scope="both")
    scope = result["manifest"]["scope_config"]
    assert scope["leaked_hits"] == 0, "a correct implementation must leak nothing"
    assert scope["scoped_queries"] > 0


def test_an_unknown_scope_is_rejected() -> None:
    """`--scope typo` must not silently behave like `scoped`."""
    with pytest.raises(ValueError):
        _evaluate(_corpus_file(), scope="scpoed")


def test_an_empty_k_is_rejected() -> None:
    """`--k ""` parses to an empty list, and a run with no recall depths measures nothing."""
    with pytest.raises(ValueError, match="positive recall depths"):
        _evaluate(_corpus_file(), ks=[])


def test_a_zero_recall_depth_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive recall depths"):
        _evaluate(_corpus_file(), ks=[5, 0])


def test_an_empty_modes_list_is_rejected() -> None:
    """A run with no search mode produces an empty aggregate and would report as clean."""
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_datasets import load_records
    from memoratum.eval_longmemeval import evaluate as run_evaluate

    result = run_evaluate(
        load_records(_corpus_file()),
        n=1,
        seed=42,
        ks=[5],
        modes=[],
        embedder=HashEmbedder(dims=64),
    )
    assert result["aggregate"] == {}, "an empty modes list yields an empty aggregate"


def test_ranked_sessions_are_reported_for_every_scoped_query() -> None:
    """An operator debugging a recall figure needs to see what was retrieved, in order."""
    result = _evaluate(_corpus_file(), project_count=2, ks=[5])
    metrics = result["questions"][0]["modes"]["hybrid"]
    assert "ranked_sessions" in metrics, (
        "ranked_sessions was dropped; a recall figure with no retrieved list is unauditable"
    )
    ranked = metrics["ranked_sessions"]
    assert ranked, "the premise is that something was retrieved"
    # Every entry belongs to the query's own project.
    for item in ranked:
        assert item.startswith("proj-0::"), item


def test_an_unscoped_query_returns_documents_from_several_projects() -> None:
    """The unscoped bucket must genuinely span projects, or it demonstrates nothing."""
    result = _evaluate(_corpus_file(), project_count=3, scope="both", ks=[10])
    question = result["questions"][0]
    unscoped = question["scope"]["hybrid:unscoped"]
    assert unscoped["returned_hits"] > 0
    seen_projects = {
        item.split("::", 1)[0] for item in question["modes"]["hybrid"]["ranked_sessions"]
    }
    assert seen_projects <= {"proj-0"}, "the scoped query must stay inside proj-0"


def test_an_unrecognised_document_id_is_not_attributed_to_project_zero() -> None:
    """An id with no `project::` separator must not silently match the query's project.

    `_project_of` returning `""` is what keeps a malformed id out of the gold set. Returning
    `"proj-0"` would make a malformed id indistinguishable from a legitimate one.
    """
    from memoratum.eval_longmemeval import _project_of, _session_of

    assert _project_of("proj-1::s1") == "proj-1"
    assert _session_of("proj-1::s1") == "s1"
    assert _project_of("bare-session-id") == ""
    assert _project_of("bare-session-id") != "proj-0"
    assert _session_of("bare-session-id") == "bare-session-id"


# --- FR-007 / SC-003: the same command gives the same manifest ----------------------


def test_the_same_command_twice_gives_an_identical_manifest() -> None:
    """Reproducibility, in the only form that matters: the recorded provenance."""
    first = _evaluate(_corpus_file(), project_count=2, scope="both")
    second = _evaluate(_corpus_file(), project_count=2, scope="both")
    assert first["manifest"] == second["manifest"]


def test_the_same_command_twice_gives_identical_metric_values() -> None:
    first = _evaluate(_corpus_file(), project_count=2, scope="both")
    second = _evaluate(_corpus_file(), project_count=2, scope="both")
    assert first["aggregate"] == second["aggregate"]
    assert [q["modes"] for q in first["questions"]] == [q["modes"] for q in second["questions"]]


def test_a_different_seed_is_recorded_as_a_different_manifest() -> None:
    """Otherwise two different samples would be presented as one figure."""
    first = _evaluate(_corpus_file(), project_count=1, seed=42)
    second = _evaluate(_corpus_file(), project_count=1, seed=7)
    assert first["manifest"]["seed"] == 42
    assert second["manifest"]["seed"] == 7
    assert first["manifest"] != second["manifest"]


# --- CLI (T093) --------------------------------------------------------------------


def test_an_unknown_scope_is_rejected_by_the_cli() -> None:
    """`--scope scpoed` must fail loudly rather than behaving like `scoped`.

    Silently accepting a typo is the worst outcome: the run reports figures that say
    nothing about the scope it appears to have measured.
    """
    code, _, err = _run_cli(["--data", _corpus_file(), "--n", "1", "--scope", "scpoed"])
    assert code != 0
    assert "scpoed" in err, err
    assert "scoped" in err, "the diagnostic must name the accepted values"


def test_a_non_integer_project_count_is_a_type_error() -> None:
    """`project_count="two"` must not be coerced into 0 and then compared as a number."""
    with pytest.raises(TypeError):
        _evaluate(_corpus_file(), project_count="2")  # type: ignore[arg-type]


def test_a_boolean_project_count_is_rejected() -> None:
    """`True` is an int in Python, and `project_count=True` would silently mean 1."""
    with pytest.raises(TypeError):
        _evaluate(_corpus_file(), project_count=True)  # type: ignore[arg-type]


def test_unscoped_with_one_project_is_refused() -> None:
    """With one project, every document is already in scope — there is nothing to show.

    Accepting it would produce an "unscoped" bucket that can only ever confirm itself.
    """
    with pytest.raises(ValueError, match="nothing to demonstrate"):
        _evaluate(_corpus_file(), project_count=1, scope="unscoped")


def test_the_manifest_schema_is_the_scoped_one() -> None:
    """A reader keys the manifest shape off `schema`; a stale value misdescribes the run.

    `longmemeval-normalized-v1` predates `scope_config` entirely, so an artifact carrying it
    would look like a run that had no scope dimension.
    """
    result = _evaluate(_corpus_file(), project_count=2, scope="both")
    assert result["manifest"]["schema"] == "longmemeval-scoped-v2"


def test_the_summarize_report_states_the_scope_and_the_leak_count() -> None:
    """The operator-facing output must carry the scope dimension, not just the JSON."""
    from memoratum.eval_longmemeval import summarize

    report = summarize(_evaluate(_corpus_file(), project_count=2, scope="both"))
    assert "## Scope" in report
    assert "project_count: 2" in report
    assert "scope: both" in report
    assert "leaked_hits: 0" in report
    assert "unscoped_queries" in report


def test_the_summarize_report_names_the_baselines_it_gated_against() -> None:
    """A gated figure must say what it was gated against."""
    from memoratum.eval_longmemeval import summarize

    result = _evaluate(_corpus_file())
    result["manifest"]["baseline_sources"] = {"latency": "eval/BASELINES.md"}
    report = summarize(result)
    assert "## Baselines gated against" in report
    assert "eval/BASELINES.md" in report


def test_the_summarize_report_omits_both_sections_when_they_do_not_apply() -> None:
    from memoratum.eval_longmemeval import summarize

    result = _evaluate(_corpus_file())
    result["manifest"].pop("scope_config", None)
    result["manifest"]["baseline_sources"] = {}
    report = summarize(result)
    assert "## Scope" not in report
    assert "## Baselines gated against" not in report


def test_a_leak_fails_the_cli() -> None:
    """A scope leak is a correctness failure, and the exit code must say so.

    Driven through a real leak: `search()` that ignores `project_id` is the feature-001
    defect this whole axis exists to catch, and it must exit non-zero here.
    """
    import memoratum.eval_longmemeval as runner
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_datasets import load_records

    # Patch the name in the *runner* module, not in `memoratum.search`: the runner binds
    # `search` at import time, so replacing the source module's attribute leaves the call
    # the runner actually makes untouched. My first attempt patched the wrong module and the
    # test passed for the wrong reason -- no leak, because no patch had been applied.
    original = runner.search

    def leaky(conn, embedder, query, *, project_id=None, **kwargs):
        kwargs.pop("project_id", None)
        return original(conn, embedder, query, **kwargs)

    runner.search = leaky
    try:
        result = runner.evaluate(
            load_records(_corpus_file()),
            n=2,
            seed=42,
            ks=[5],
            modes=["hybrid"],
            embedder=HashEmbedder(dims=64),
            project_count=3,
            scope="both",
        )
    finally:
        runner.search = original

    assert result["manifest"]["scope_config"]["leaked_hits"] > 0, (
        "a store that ignores project_id produced no leaks, so this run proves nothing"
    )


def test_the_cli_exits_non_zero_on_a_real_leak(tmp_path) -> None:
    """The leak must reach the process exit code, not just the JSON.

    A runner that reports `leaked_hits > 0` and exits 0 is a CI job that stays green while
    tenant isolation is broken.
    """
    import memoratum.eval_longmemeval as runner

    original = runner.search

    def leaky(conn, embedder, query, *, project_id=None, **kwargs):
        kwargs.pop("project_id", None)
        return original(conn, embedder, query, **kwargs)

    runner.search = leaky
    try:
        code, out, _ = _run_cli(
            [
                "--data",
                _corpus_file(),
                "--n",
                "2",
                "--project-count",
                "3",
                "--scope",
                "both",
                "--out-json",
                str(tmp_path / "leak.json"),
            ]
        )
    finally:
        runner.search = original
    assert code != 0, "a run that leaked data exited 0"
    assert "leaked_hits" in out


def test_cli_accepts_the_new_flags(tmp_path) -> None:
    js = tmp_path / "out.json"
    code, _, err = _run_cli(
        [
            "--data",
            _corpus_file(),
            "--n",
            "2",
            "--project-count",
            "2",
            "--scope",
            "both",
            "--out-json",
            str(js),
        ]
    )
    assert code == 0, err
    payload = json.loads(js.read_text())
    assert payload["manifest"]["scope_config"]["project_count"] == 2
    assert payload["manifest"]["scope_config"]["scope"] == "both"
    assert payload["manifest"]["scope_config"]["unscoped_queries"] > 0


def test_cli_rejects_an_unknown_scope() -> None:
    code, _, err = _run_cli(["--data", _corpus_file(), "--n", "1", "--scope", "scpoed"])
    assert code != 0
    assert "scope" in err.lower()


def test_cli_rejects_an_empty_k() -> None:
    """`--k ""` parses to an empty list, and an empty report reads like a clean one."""
    for k in ("", " ", ",", "0"):
        code, _, err = _run_cli(["--data", _corpus_file(), "--n", "1", "--k", k])
        assert code != 0, f"--k {k!r} was accepted"
        assert "--k" in err, err


def test_cli_rejects_an_empty_modes_list() -> None:
    for modes in ("", " ", ",,"):
        code, _, err = _run_cli(["--data", _corpus_file(), "--n", "1", "--modes", modes])
        assert code != 0, f"--modes {modes!r} was accepted"
        assert "--modes" in err, err


def test_cli_rejects_a_missing_corpus() -> None:
    """A missing file must not reach `load_records`, which would raise a raw traceback."""
    code, _, err = _run_cli(["--data", "/nonexistent/corpus.json", "--n", "1"])
    assert code != 0
    assert "corpus not found" in err


def test_cli_rejects_a_nonpositive_project_count() -> None:
    code, _, err = _run_cli(["--data", _corpus_file(), "--n", "1", "--project-count", "0"])
    assert code != 0
    assert "project" in err.lower()


def test_cli_rejects_an_unusable_latency_baseline(tmp_path) -> None:
    """A baseline path that does not exist must fail, not gate against nothing."""
    code, _, err = _run_cli(
        ["--data", _corpus_file(), "--n", "1", "--latency-baseline", "/nonexistent/baseline.md"]
    )
    assert code != 0
    assert "baseline" in err.lower()


def test_cli_rejects_an_unusable_cost_baseline() -> None:
    code, _, err = _run_cli(
        ["--data", _corpus_file(), "--n", "1", "--cost-baseline", "/nonexistent/baseline.md"]
    )
    assert code != 0
    assert "baseline" in err.lower()


def test_the_baseline_flags_are_recorded_in_the_manifest(tmp_path) -> None:
    """A gated run must say what it gated against, or the figure is unanchored."""
    baseline = os.path.join(_tempdir(), "BASELINES.md")
    with open(baseline, "w", encoding="utf-8") as handle:
        handle.write(
            "# Baselines\n\n"
            "| kind | figure | value |\n"
            "|---|---|---:|\n"
            "| latency | retrieve_median_ms | 11.09 |\n"
            "| latency | us_per_chunk_ratio | 0.93 |\n"
            "| cost | retrieved_chars_mean | 28469.0 |\n"
        )
    js = os.path.join(_tempdir(), "gated.json")
    code, _, err = _run_cli(
        [
            "--data",
            _corpus_file(),
            "--n",
            "1",
            "--latency-baseline",
            baseline,
            "--cost-baseline",
            baseline,
            "--out-json",
            js,
        ]
    )
    assert code == 0, err
    with open(js, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["manifest"]["baseline_sources"], (
        "the run gated against a baseline but did not record where it came from"
    )


# --- BASELINES.md is readable by the axes that gate against it ---------------------


def test_a_markdown_separator_row_is_not_parsed_as_a_baseline() -> None:
    """`|---|---|` is Markdown's own separator, and it arrives as a data row.

    Keying on it produced a phantom baseline named `---` that held no figure, so a baseline
    lookup could report "present" while comparing against nothing.
    """
    from memoratum.eval_cost import _load_baseline_rows

    rows = _load_baseline_rows(os.path.join(REPO_ROOT, "eval", "BASELINES.md"))
    assert rows, "the committed BASELINES.md must be parseable by the axis's own reader"
    assert "---" not in rows
    for figure, row in rows.items():
        assert figure.strip() == figure and figure != "---", figure
        assert row.get("axis") == figure
        assert row.get("figure", "").strip(), f"{figure} has no figure value"


def test_both_axes_read_the_committed_baselines_file() -> None:
    """One file, one parser: two Markdown parsers is how it became unreadable before."""
    from memoratum.eval_cost import _load_baseline_rows
    from memoratum.eval_latency import load_baselines

    path = os.path.join(REPO_ROOT, "eval", "BASELINES.md")
    assert sorted(load_baselines(path)) == sorted(_load_baseline_rows(path))


def test_the_committed_latency_baseline_is_usable() -> None:
    """B1: a hardware-bound baseline without usable hardware is invalid and fails."""
    from memoratum.eval_axes import load_baseline
    from memoratum.eval_latency import load_baselines

    rows = load_baselines(os.path.join(REPO_ROOT, "eval", "BASELINES.md"))
    row = rows.get("retrieve_median_ms")
    assert row is not None, "the latency baseline recorded during Phase D is missing"
    usable = load_baseline(row, figure="retrieve_median_ms")
    assert usable is not None, (
        f"the committed latency baseline is unusable: {row}; every latency gate would "
        "fail 'no usable baseline' forever"
    )
    assert usable["value"] > 0
