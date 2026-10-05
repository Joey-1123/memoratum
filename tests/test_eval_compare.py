"""Reproducibility contract for `eval_compare` (RED).

The point of this module is that a published figure is only worth reading if you can tell
**what produced it**. Two numbers from two different configurations are not a comparison,
they are two unrelated numbers next to each other — and a Markdown table happily presents
them as one. So a manifest mismatch must **hard-fail**, not warn (``invariant M2``,
research D8).

These tests encode four claims:

* **Re-running the same command gives the same manifest and the same values.** Anything
  else means the figure cannot be regenerated, which makes it a rumour (FR-007, SC-003).
* **A mismatch exits non-zero and names the keys.** "Please note the embedders differ" is
  not a gate.
* **The compared keys are the ones that change meaning**: embedder, vector store, modes,
  seed, ``data_sha256``, and ``n``. The *metric definition* matters as much — comparing a
  v0 figure with a v1 one is the exact mistake ``eval/MIGRATION-metric-v1.md`` exists to
  warn about.
* **A missing key is a mismatch, not a pass.** Absent provenance is not comparable
  provenance.

Covers T088-T090 and T094.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Keys that change what a number means. Anything not here is a detail of the run; these
#: are the ones that make two figures incomparable.
#:
#: Deliberately duplicated from `eval_compare.COMPARED_KEYS` rather than imported: a test
#: that reads the constant it is meant to police would go green the day the constant was
#: emptied.
COMPARED_KEYS = (
    "schema",
    "embedder",
    "vector_store",
    "modes",
    "seed",
    "n",
    "data_sha256",
    "metric_definition",
)

#: Manifest blocks carrying gate configuration, e.g. `min_grounded_fraction`. A gate bound
#: is part of what a figure means. Also duplicated, for the same reason.
COMPARED_BLOCKS = (
    "scope_config",
    "cost_config",
    "latency_config",
    "grounding_config",
)

_TEMP_ROOTS: list[tempfile.TemporaryDirectory] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_temp_roots():
    yield
    while _TEMP_ROOTS:
        _TEMP_ROOTS.pop().cleanup()


def _tempdir(prefix: str = "memoratum-compare-") -> str:
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    _TEMP_ROOTS.append(holder)
    return holder.name


def _manifest(**overrides) -> dict:
    manifest = {
        "schema": "longmemeval-scoped-v2",
        "seed": 42,
        "requested_n": 20,
        "n": 20,
        "ks": [5, 10],
        "modes": ["hybrid"],
        "embedder": "HashEmbedder:64",
        "vector_store": "sqlite",
        "data_sha256": "a" * 64,
    }
    manifest.update(overrides)
    return manifest


def _result(**overrides) -> dict:
    result = {
        "axis": "grounding",
        "schema": "memoratum-eval-axes-v1",
        "dataset": "longmemeval-s",
        "n": 20,
        "manifest": _manifest(),
        "gate": {"status": "pass", "checks": []},
    }
    for key, value in overrides.items():
        if key == "manifest":
            result["manifest"] = {**result["manifest"], **value}
        else:
            result[key] = value
    return result


def _compare(a: dict, b: dict) -> dict:
    from memoratum.eval_compare import compare_manifests

    return compare_manifests(a, b)


# --- T088: determinism ------------------------------------------------------------


def test_identical_manifests_compare_clean() -> None:
    """The happy path must be genuinely clean, or the gate means nothing."""
    outcome = _compare(_result(), _result())
    assert outcome["comparable"] is True
    assert outcome["mismatches"] == {}
    assert outcome["exit_code"] == 0


def test_identical_manifests_with_different_metric_values_are_still_comparable() -> None:
    """A metric difference is the finding; a manifest difference is the blocker.

    Conflating the two would make the gate fire on every improved score.
    """
    a = _result(grounding={"grounded_fraction": 1.0, "hits_checked": 250})
    b = _result(grounding={"grounded_fraction": 0.9, "hits_checked": 250})
    outcome = _compare(a, b)
    assert outcome["comparable"] is True
    assert outcome["exit_code"] == 0
    delta = outcome["value_deltas"]["grounding.grounded_fraction"]
    assert delta["change"] == pytest.approx(-0.1, abs=1e-9)
    assert delta["direction"] == "down"


def test_deltas_report_direction_and_size() -> None:
    """A regression must be visible as a regression, not as "different"."""
    a = _result(aggregate={"hybrid": {"mrr": 0.583}})
    b = _result(aggregate={"hybrid": {"mrr": 0.599}})
    outcome = _compare(a, b)
    delta = outcome["value_deltas"]["hybrid.mrr"]
    assert delta["before"] == pytest.approx(0.583)
    assert delta["after"] == pytest.approx(0.599)
    assert delta["change"] == pytest.approx(0.016, abs=1e-9)
    assert delta["direction"] == "up"


def test_an_unchanged_metric_is_reported_as_same_not_omitted() -> None:
    """A reader needs to distinguish "unchanged" from "not measured"."""
    a = _result(grounding={"grounded_fraction": 1.0})
    b = _result(grounding={"grounded_fraction": 1.0})
    outcome = _compare(a, b)
    assert outcome["value_deltas"]["grounding.grounded_fraction"]["direction"] == "same"
    assert outcome["value_deltas"]["grounding.grounded_fraction"]["change"] == pytest.approx(0.0)


def test_a_metric_present_on_one_side_only_is_reported_as_missing() -> None:
    """A metric that disappears must be visible, not silently absent from the diff."""
    a = _result(grounding={"grounded_fraction": 1.0, "unresolvable_hits": 0})
    b = _result(grounding={"grounded_fraction": 1.0})
    outcome = _compare(a, b)
    entry = outcome["value_deltas"]["grounding.unresolvable_hits"]
    assert entry["direction"] == "missing"
    assert entry["present_on"] == "left"
    assert outcome["comparable"] is True, "a missing metric is not a manifest mismatch"


def test_the_report_lists_a_missing_metric_rather_than_hiding_it() -> None:
    from memoratum.eval_compare import compare_manifests, render_report

    a = _result(grounding={"grounded_fraction": 1.0, "unresolvable_hits": 0})
    b = _result(grounding={"grounded_fraction": 1.0})
    report = render_report(compare_manifests(a, b), [a, b])
    assert "only on the left side" in report
    assert "grounding.unresolvable_hits" in report


# --- T089: embedder mismatch must FAIL --------------------------------------------


def test_an_embedder_mismatch_fails_and_names_the_key() -> None:
    """A confident number across two embedders is worse than no number (D8)."""
    outcome = _compare(_result(), _result(manifest={"embedder": "OpenAI:text-embed-3"}))
    assert outcome["comparable"] is False
    assert outcome["exit_code"] != 0
    assert "embedder" in outcome["mismatches"]
    assert "HashEmbedder:64" in outcome["mismatches"]["embedder"]["left"]
    assert "text-embed-3" in outcome["mismatches"]["embedder"]["right"]


def test_the_report_leads_with_the_mismatch_not_the_table() -> None:
    """If the table is printed first, a reader skims past the block."""
    from memoratum.eval_compare import compare_manifests, render_report

    outcome = compare_manifests(_result(), _result(manifest={"embedder": "Other"}))
    report = render_report(outcome, [_result()])
    assert "NOT COMPARABLE" in report
    assert "embedder" in report
    assert "regenerate" in report.lower(), "the report must say what to do about it"
    # The block must come before the numbers, or a reader skims straight to them.
    if "| Metric" in report:
        assert report.index("NOT COMPARABLE") < report.index("| Metric")


# --- T090: every compared key -----------------------------------------------------


@pytest.mark.parametrize(
    "block",
    ["scope_config", "cost_config", "latency_config", "grounding_config"],
)
def test_every_axis_config_block_fails_on_mismatch(block: str) -> None:
    """A gate bound is part of what a figure means.

    Two runs differing only in `--min-grounded-fraction` would otherwise be printed as one
    comparison, and the looser bound would look like an improvement.
    """
    base = {"normalization": "nfkc+whitespace+casefold", "min_grounded_fraction": 1.0}
    left = _result(manifest={block: base})
    right = _result(manifest={block: {**base, "min_grounded_fraction": 0.5}})
    outcome = _compare(left, right)
    assert outcome["comparable"] is False, f"a {block} mismatch did not fail the comparison"
    assert block in outcome["mismatches"]


@pytest.mark.parametrize("block", COMPARED_BLOCKS)
def test_a_missing_config_block_is_a_mismatch(block: str) -> None:
    """One side recording its gate bounds and the other not is not agreement."""
    outcome = _compare(_result(), _result(manifest={block: {"min_grounded_fraction": 1.0}}))
    assert outcome["comparable"] is False
    assert block in outcome["mismatches"]


@pytest.mark.parametrize("key", COMPARED_KEYS)
def test_every_compared_key_fails_on_mismatch(key: str) -> None:
    """A key in the compared set that does not fail is a promise the gate does not keep."""
    changed = {
        "schema": "longmemeval-scoped-v1",
        "embedder": "Something:128",
        "vector_store": "QdrantVectorStore",
        "modes": ["memories", "documents"],
        "seed": 7,
        "n": 5,
        "data_sha256": "b" * 64,
        "metric_definition": "v0",
    }[key]
    outcome = _compare(_result(), _result(manifest={key: changed}))
    assert outcome["comparable"] is False, f"a {key} mismatch did not fail the comparison"
    assert key in outcome["mismatches"]


def test_a_missing_key_is_a_mismatch_not_a_pass() -> None:
    """Absent provenance is not comparable provenance.

    Dropping `data_sha256` from one side must not read as agreement.
    """
    left = _result()
    right = _result()
    del right["manifest"]["data_sha256"]
    outcome = _compare(left, right)
    assert outcome["comparable"] is False
    assert "data_sha256" in outcome["mismatches"]


def test_requested_n_is_reported_but_does_not_block() -> None:
    """`requested_n` differing is worth showing, and it is not a definition change.

    Two runs that asked for 50 and got 20 are different samples, so the delta is
    informational; blocking on it would make a sampling note a gate failure.
    """
    outcome = _compare(_result(), _result(manifest={"requested_n": 50}))
    assert outcome["comparable"] is True
    assert outcome["notes"]


def test_the_metric_definition_version_is_compared() -> None:
    """Comparing a v0 figure with a v1 one is the mistake the migration note warns about."""
    outcome = _compare(
        _result(manifest={"metric_definition": "v0"}),
        _result(manifest={"metric_definition": "v1"}),
    )
    assert outcome["comparable"] is False
    assert "metric_definition" in outcome["mismatches"]


def test_a_judge_setting_difference_is_reported_but_does_not_block() -> None:
    """`judge` is recorded but non-gating, so a difference must not block.

    Blocking on it would make an operator re-run with the same `--judge` for no reason, and
    the grounding axis explicitly guarantees (G6, FR-006) that an absent judge does not
    change the figure. So the setting is reported as a note.
    """
    outcome = _compare(
        _result(manifest={"grounding_config": {"min_grounded_fraction": 1.0, "judge": None}}),
        _result(manifest={"grounding_config": {"min_grounded_fraction": 1.0, "judge": "external"}}),
    )
    assert outcome["comparable"] is True, outcome["mismatches"]
    assert "judge" in str(outcome["notes"])


def test_an_isolation_leak_count_change_is_a_reported_delta() -> None:
    """`leaked_hits` is the isolation gate, so a change in it is a change in severity.

    The gate is an absolute count, not a rate, so this must be compared as a number rather
    than filtered out as a boolean-ish flag.
    """
    a = _result(isolation={"leaked_hits": 0, "leaked_queries": 0, "returned_hits": 380})
    b = _result(isolation={"leaked_hits": 6, "leaked_queries": 4, "returned_hits": 380})
    outcome = _compare(a, b)
    entry = outcome["value_deltas"]["isolation.leaked_hits"]
    assert entry["change"] == pytest.approx(6.0)
    assert entry["direction"] == "up"


def test_boolean_flags_are_not_treated_as_metric_values() -> None:
    """`True` vs `1` is not a finding, and reporting it would be noise."""
    a = _result(retrieval={"used_rerank": True})
    b = _result(retrieval={"used_rerank": False})
    outcome = _compare(a, b)
    assert "retrieval.used_rerank" not in outcome["value_deltas"]
    assert not [k for k in outcome["value_deltas"] if k.startswith("retrieval.")]


def test_the_grounded_fraction_is_compared_across_axes() -> None:
    """The grounding axis's own figure must reach the delta table."""
    a = _result(grounding={"grounded_fraction": 1.0, "hits_checked": 250})
    b = _result(grounding={"grounded_fraction": 0.96, "hits_checked": 250})
    outcome = _compare(a, b)
    assert outcome["value_deltas"]["grounding.grounded_fraction"]["change"] == pytest.approx(
        -0.04, abs=1e-9
    )


# --- determinism of the artifacts themselves (FR-007, SC-003) ----------------------


def test_writing_a_result_twice_produces_identical_json() -> None:
    """`eval_compare` output must be byte-identical for identical input.

    A timestamp or a dict-ordering artefact would make every published file look like a
    diff.
    """
    from memoratum.eval_compare import render_report

    outcome = _compare(_result(), _result())
    first = render_report(outcome, [_result(), _result()])
    second = render_report(outcome, [_result(), _result()])
    assert first == second


# --- CLI ---------------------------------------------------------------------------


def _run_module(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO_ROOT, "src")
    return subprocess.run(
        [sys.executable, "-m", "memoratum.eval_compare", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        check=False,
    )


def _write(payload: dict, name: str) -> str:
    path = os.path.join(_tempdir(), name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def test_allow_mismatch_reports_but_exits_zero(tmp_path) -> None:
    """A deliberate, documented difference must be recordable.

    Documenting a known difference is legitimate; silently comparing is not. The flag is
    the former, and it still has to print the diagnosis.
    """
    out = tmp_path / "RESULTS.md"
    proc = _run_module(
        [
            _write(_result(), "a.json"),
            _write(_result(manifest={"embedder": "Other"}), "b.json"),
            "--allow-mismatch",
            "--out-md",
            str(out),
            "--out-json",
            str(tmp_path / "results.json"),
        ]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NOT COMPARABLE" in proc.stdout
    assert out.exists()
    with open(tmp_path / "results.json", encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["comparable"] is False, "the recorded JSON must keep the verdict"
    assert "embedder" in payload["mismatches"]


def test_cli_exits_zero_for_comparable_results(tmp_path) -> None:
    out = tmp_path / "RESULTS.md"
    proc = _run_module(
        [_write(_result(), "a.json"), _write(_result(), "b.json"), "--out-md", str(out)]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [("embedder", "Other"), ("vector_store", "Other"), ("seed", 7), ("data_sha256", "c" * 64)],
)
def test_cli_exits_non_zero_on_a_manifest_mismatch(key, value, tmp_path) -> None:
    """The mismatch must reach the exit code, not just the report text."""
    out = tmp_path / "RESULTS.md"
    proc = _run_module(
        [
            _write(_result(), "a.json"),
            _write(_result(manifest={key: value}), "b.json"),
            "--out-md",
            str(out),
        ]
    )
    assert proc.returncode != 0, (
        f"a {key} mismatch exited 0. The report would say NOT COMPARABLE while CI went "
        "green -- worse than not checking at all"
    )
    assert key in proc.stdout + proc.stderr


def test_a_three_file_run_reports_a_mismatch_between_the_last_two(tmp_path) -> None:
    """Every adjacent pair must be compared, not just the first and last.

    A run that reports only the outermost comparison would hide a difference between files
    1 and 2 while still printing all three as one table — and the operator has no way to
    tell which pair disagrees.
    """
    # The difference is between files 2 and 3, so comparing only (1,2) finds nothing.
    a = _result()
    b = _result()
    c = _result(manifest={"embedder": "Last:256"})
    proc = _run_module([_write(a, "a.json"), _write(b, "b.json"), _write(c, "c.json")])
    assert proc.returncode != 0
    assert "Last:256" in proc.stdout + proc.stderr, (
        "the last pair was never compared; a run comparing only the first two files would "
        "print three differing manifests in one table and exit 0"
    )


def test_a_three_file_run_compares_pairs_in_order(tmp_path) -> None:
    """With all three identical, the run is clean — the pairwise walk must not invent drift."""
    proc = _run_module(
        [_write(_result(), "a.json"), _write(_result(), "b.json"), _write(_result(), "c.json")]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_an_absent_key_is_rendered_as_absent_not_as_an_empty_value() -> None:
    """`<absent>` vs `""` must be distinguishable, or the table cannot be read.

    M3 requires a missing figure and a zero figure to be different states; rendering both
    as an empty cell collapses exactly that distinction in the report.
    """
    from memoratum.eval_compare import compare_manifests, render_report

    left = _result()
    right = _result()
    del right["manifest"]["data_sha256"]
    outcome = compare_manifests(left, right)
    assert outcome["mismatches"]["data_sha256"]["right"] == "<absent>"
    assert (
        outcome["mismatches"]["data_sha256"]["left"]
        != outcome["mismatches"]["data_sha256"]["right"]
    )
    report = render_report(outcome, [left, right])
    assert "<absent>" in report
    assert "`data_sha256` |" in report


def test_notes_are_rendered_in_the_report() -> None:
    """A non-blocking difference still has to reach the reader.

    `requested_n` and `ks` differ routinely; if the notes were computed and not printed,
    an operator would have no way to see why two runs sampled differently.
    """
    from memoratum.eval_compare import compare_manifests, render_report

    outcome = compare_manifests(
        _result(manifest={"requested_n": 50}), _result(manifest={"requested_n": 20})
    )
    assert "requested_n" in outcome["notes"]
    report = render_report(outcome, [_result(), _result()])
    assert "Differences that do not block" in report
    assert "requested_n" in report


def test_the_figures_table_lists_every_input() -> None:
    """Three results in, three rows out — a dropped row hides a run."""
    from memoratum.eval_compare import compare_manifests, render_report

    results = [_result(), _result(axis="cost"), _result(axis="latency")]
    outcome = compare_manifests(results[0], results[1])
    report = render_report(outcome, results)
    for axis in ("grounding", "cost", "latency"):
        assert f"| {axis} |" in report


def test_the_gate_column_is_shown_per_result() -> None:
    """A failing run must be visible in the table, not implied by a prose sentence."""
    from memoratum.eval_compare import compare_manifests, render_report

    failing = _result(axis="cost", gate={"status": "fail", "checks": []})
    report = render_report(compare_manifests(failing, failing), [failing])
    assert "| cost |" in report
    assert "fail" in report


def test_a_malformed_aggregate_block_is_skipped_not_fatal() -> None:
    """A runner that emits `aggregate: null` must not crash the comparison."""
    from memoratum.eval_compare import compare_manifests

    outcome = compare_manifests(_result(aggregate=None), _result(aggregate=None))
    assert outcome["comparable"] is True
    outcome = compare_manifests(
        _result(aggregate={"hybrid": "not-a-dict"}), _result(aggregate={"hybrid": "not-a-dict"})
    )
    assert outcome["comparable"] is True


def test_non_numeric_values_inside_an_aggregate_block_are_skipped() -> None:
    """A runner that emits a string alongside numbers must not crash the comparison.

    `float("n/a")` would raise; the value is simply not a measurement, so it is left out.
    """
    from memoratum.eval_compare import compare_manifests

    a = _result(aggregate={"hybrid": {"mrr": 0.58, "label": "n/a"}})
    b = _result(aggregate={"hybrid": {"mrr": 0.58, "label": "n/a"}})
    outcome = compare_manifests(a, b)
    assert outcome["comparable"] is True
    assert "hybrid.mrr" in outcome["value_deltas"]
    assert not [k for k in outcome["value_deltas"] if "label" in k], (
        "a string metric was treated as a measurement"
    )


def test_allow_mismatch_does_not_change_the_printed_verdict(tmp_path) -> None:
    """`--allow-mismatch` changes the exit code only.

    If it also changed the report, the artifact would claim comparability and become a
    reproducibility record for a comparison that was never made.
    """
    a = _write(_result(), "a.json")
    b = _write(_result(manifest={"embedder": "Other"}), "b.json")
    blocked = _run_module([a, b])
    allowed = _run_module([a, b, "--allow-mismatch"])
    assert blocked.returncode != 0
    assert allowed.returncode == 0
    assert "NOT COMPARABLE" in allowed.stdout, (
        "the report claimed comparability while the manifests differ"
    )
    assert "NOT COMPARABLE" in blocked.stdout


def test_allow_mismatch_on_comparable_results_is_a_no_op(tmp_path) -> None:
    """The flag must not change the verdict when there was nothing to override."""
    proc = _run_module(
        [_write(_result(), "a.json"), _write(_result(), "b.json"), "--allow-mismatch"]
    )
    assert proc.returncode == 0
    assert "manifests comparable" in proc.stdout
    assert "NOT COMPARABLE" not in proc.stdout


def test_accuracy_metrics_are_compared_for_the_mc10_runner() -> None:
    """The plain runners report accuracy at the top level, not under an axis block."""
    from memoratum.eval_compare import compare_manifests

    a = _result(axis="mc10", accuracy=0.62, balanced_accuracy=0.60)
    b = _result(axis="mc10", accuracy=0.71, balanced_accuracy=0.68)
    outcome = compare_manifests(a, b)
    assert outcome["comparable"] is True
    assert outcome["value_deltas"]["accuracy"]["change"] == pytest.approx(0.09, abs=1e-9)
    assert outcome["value_deltas"]["balanced_accuracy"]["direction"] == "up"


def test_cli_requires_at_least_one_result() -> None:
    """`nargs="+"` must reject an empty invocation rather than reporting on nothing."""
    proc = _run_module([])
    assert proc.returncode != 0


def test_a_write_failure_is_reported(tmp_path) -> None:
    """An unwritable artifact path must not exit 0 having written nothing."""
    a_file = tmp_path / "a-file"
    a_file.write_text("x")
    for flag, target in (
        ("--out-md", str(a_file / "nested" / "out.md")),
        ("--out-md", str(tmp_path)),
        ("--out-json", str(a_file / "nested" / "out.json")),
        ("--out-json", str(tmp_path)),
    ):
        proc = _run_module([_write(_result(), "a.json"), flag, target])
        assert proc.returncode != 0, f"{flag}={target} was accepted"
        assert "could not write" in proc.stderr


def test_a_non_mapping_config_block_is_a_mismatch_not_a_crash() -> None:
    """A runner emitting `grounding_config: "x"` must fail the gate, not raise AttributeError."""
    outcome = _compare(
        _result(manifest={"grounding_config": "not-a-mapping"}),
        _result(manifest={"grounding_config": "not-a-mapping"}),
    )
    assert outcome["comparable"] is True, "identical non-mappings are still identical"
    assert not outcome["notes"]

    outcome = _compare(
        _result(manifest={"grounding_config": "not-a-mapping"}),
        _result(manifest={"grounding_config": {"min_grounded_fraction": 1.0}}),
    )
    assert outcome["comparable"] is False
    assert "grounding_config" in outcome["mismatches"]


def test_a_non_mapping_config_block_reports_no_notes() -> None:
    """The notes path must tolerate a non-mapping rather than calling `.get` on a string."""
    outcome = _compare(
        _result(manifest={"grounding_config": 7}),
        _result(manifest={"grounding_config": 7}),
    )
    assert outcome["notes"] == {}


def test_cli_rejects_a_missing_file() -> None:
    proc = _run_module(["/nonexistent/results.json"])
    assert proc.returncode != 0


def test_cli_rejects_malformed_json(tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    proc = _run_module([str(bad)])
    assert proc.returncode != 0


def test_cli_writes_the_report_even_when_not_comparable(tmp_path) -> None:
    """An operator needs the diagnosis in a file, not only on a terminal."""
    out = tmp_path / "RESULTS.md"
    proc = _run_module(
        [
            _write(_result(), "a.json"),
            _write(_result(manifest={"embedder": "Other"}), "b.json"),
            "--out-md",
            str(out),
        ]
    )
    assert proc.returncode != 0
    assert out.exists()
    assert "NOT COMPARABLE" in out.read_text()
