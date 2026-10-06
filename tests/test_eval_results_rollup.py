"""SC-002: one file showing all four axes alongside recall and MRR (RED).

`eval_compare` compares two files. SC-002 asks for something different: a single
`eval/RESULTS.md` in which the four axis gates, recall and MRR can be read together, for the
*same* run.

"Together" is the load-bearing word, and it is what makes this hard. The four axes emit
manifests with different `modes`, different sample sizes and different `data_sha256` values,
because they measure different things — the cost axis retrieves 20 hits per question while
grounding retrieves 10. A rollup that printed all six figures in one table without saying so
would imply they came from one configuration, which is the exact confusion ``eval_compare``
was rebuilt to prevent in the previous phase.

So the rollup has two obligations:

* **show every axis and the retrieval metrics**, with its own gate status; and
* **state the provenance of each**, so a reader can see which rows are even comparable.

Covers T095.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL = os.path.join(REPO_ROOT, "eval")

#: Every axis the rollup must include. Asserted as a literal list, not imported from the
#: module, so emptying the module's constant cannot make this test pass.
AXES = ("isolation", "cost", "latency", "grounding")

_TEMP_ROOTS: list[tempfile.TemporaryDirectory] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_temp_roots():
    yield
    while _TEMP_ROOTS:
        _TEMP_ROOTS.pop().cleanup()


def _tempdir() -> str:
    holder = tempfile.TemporaryDirectory(prefix="memoratum-rollup-")
    _TEMP_ROOTS.append(holder)
    return holder.name


def _axis_payload(axis: str, **overrides) -> dict:
    payload = {
        "axis": axis,
        "schema": "memoratum-eval-axes-v1",
        "dataset": "longmemeval-s",
        "n": 25,
        "manifest": {
            "schema": "longmemeval-scoped-v2",
            "seed": 42,
            "requested_n": 25,
            "n": 25,
            "ks": [5, 10],
            "modes": ["hybrid"],
            "embedder": "HashEmbedder:64",
            "vector_store": "sqlite",
            "data_sha256": "a" * 64,
            **overrides.pop("manifest", {}),
        },
        "gate": {"status": "pass", "checks": []},
    }
    payload.update(overrides)
    return payload


def _retrieval_payload(**overrides) -> dict:
    payload = {
        "dataset": "longmemeval-s",
        "n": 10,
        "manifest": {
            "schema": "longmemeval-normalized-v1",
            "seed": 42,
            "requested_n": 10,
            "n": 10,
            "ks": [5, 10],
            "modes": ["hybrid"],
            "embedder": "HashEmbedder:64",
            "vector_store": "SQLiteVectorStore",
            "data_sha256": "b" * 64,
        },
        "aggregate": {"hybrid": {"partial-R@5": 0.42, "full-R@5": 0.3, "MRR": 0.583}},
        "modes": ["hybrid"],
        "ks": [5, 10],
    }
    payload.update(overrides)
    return payload


def _rollup(axes: list[dict], retrieval: dict | None = None) -> str:
    from memoratum.eval_results import render_rollup

    return render_rollup(axes, retrieval)


# --- every axis is present ---------------------------------------------------------


def test_all_four_axes_and_retrieval_appear() -> None:
    report = _rollup([_axis_payload(axis) for axis in AXES], _retrieval_payload())
    for axis in AXES:
        assert axis in report, f"the rollup omits the {axis} axis"
    assert "MRR" in report
    assert "partial-R@5" in report


def test_each_axis_reports_its_gate_status() -> None:
    """A gate status is the most load-bearing cell; a reader must not have to open the JSON."""
    failing = _axis_payload("cost", gate={"status": "fail", "checks": []})
    report = _rollup([_axis_payload(axis) for axis in AXES if axis != "cost"] + [failing])
    assert "FAIL" in report, "a failing gate must be shown as FAIL"
    assert "PASS" in report, "a passing gate must be shown as PASS"


def test_a_failing_gate_is_visually_distinguishable() -> None:
    """Not merely present — marked. A bare word in a table column is easy to skim past."""
    failing = _axis_payload("grounding", gate={"status": "fail", "checks": []})
    report = _rollup([_axis_payload("isolation"), failing])
    line = next(line for line in report.splitlines() if "grounding" in line)
    assert "FAIL" in line.upper(), line


def test_the_rollup_names_the_gated_figures_for_each_axis() -> None:
    """A gate status without its figure is not actionable: "pass" at what?"""
    report = _rollup(
        [
            _axis_payload(
                "cost",
                cost={"retrieved_chars_mean": 28469.0},
                gate={
                    "status": "pass",
                    "checks": [
                        {
                            "name": "retrieved_chars_mean",
                            "status": "pass",
                            "value": 28469.0,
                            "bound": 31315.9,
                        }
                    ],
                },
            ),
            _axis_payload(
                "isolation",
                isolation={"leaked_hits": 0},
                gate={
                    "status": "pass",
                    "checks": [{"name": "leaked_hits", "status": "pass", "value": 0, "bound": 0}],
                },
            ),
        ]
    )
    assert "retrieved_chars_mean" in report
    assert "leaked_hits" in report


# --- provenance: the rows are NOT all comparable -----------------------------------


def test_the_rollup_states_that_the_rows_are_not_one_run() -> None:
    """The four axes legitimately have different manifests; saying so is the whole point.

    A table implying one configuration produced six figures is precisely the confusion
    `eval_compare` was rebuilt to prevent, reintroduced one layer up.
    """
    report = _rollup([_axis_payload(axis) for axis in AXES], _retrieval_payload())
    assert "NOT COMPARABLE" in report, (
        "the rollup presents figures from different configurations without saying so"
    )


def test_each_row_carries_its_own_provenance() -> None:
    """Embedder, n and the data hash per row, so a reader can see why they differ."""
    payloads = [_axis_payload(axis, manifest={"n": 25 + i}) for i, axis in enumerate(AXES)]
    payloads.append(_retrieval_payload())
    report = _rollup(payloads)
    # Each axis carries a distinct n (25..28) and the retrieval file carries 10, so every
    # one of these values must appear: a rollup showing one n for all rows would be the
    # failure this test exists to catch.
    for marker in ("HashEmbedder", "data_sha256", "25", "26", "27", "28", "10"):
        assert marker in report, f"the rollup omits provenance marker {marker!r}"


def test_two_axes_that_do_agree_are_marked_comparable() -> None:
    """Not everything is incomparable: provenance should be reported per row, not blanket-labelled."""
    same = {
        "embedder": "HashEmbedder:64",
        "vector_store": "sqlite",
        "seed": 42,
        "data_sha256": "a" * 64,
        "n": 25,
    }
    report = _rollup(
        [_axis_payload("cost", manifest=dict(same)), _axis_payload("latency", manifest=dict(same))]
    )
    assert "comparable" in report.lower()


# --- retrieval metrics --------------------------------------------------------------


def test_recall_and_mrr_are_labelled_with_their_units() -> None:
    """`R@k` is positions and MRR is distinct sessions; conflating them is the v0 bug."""
    report = _rollup([_axis_payload("isolation")], _retrieval_payload())
    lowered = report.lower()
    assert "position" in lowered, "R@k is a retrieval-position metric and must say so"
    assert "session" in lowered, "MRR is a distinct-session metric and must say so"


def test_a_retrieval_file_that_carries_v0_metrics_is_flagged() -> None:
    """M4: a pre-2026-10-04 `R@k` figure must not sit in the table unmarked."""
    payload = _retrieval_payload(manifest={"metric_definition": "v0"})
    report = _rollup([_axis_payload("isolation")], payload)
    assert "v0" in report.lower()
    assert "NOT COMPARABLE" in report


def test_the_rollup_renders_without_a_retrieval_file() -> None:
    """The axes can be run alone; the rollup must not require a file that may not exist."""
    report = _rollup([_axis_payload(axis) for axis in AXES])
    for axis in AXES:
        assert axis in report
    # The retrieval section must exist and must claim no figure. Checking for the absence of
    # the literal "MRR" would fail on the explanatory prose, so the assertion is that no
    # metric table row was emitted.
    metrics_rows = [
        line for line in report.splitlines() if line.startswith(("| hybrid |", "| documents |"))
    ]
    assert not metrics_rows, (
        f"retrieval metrics were reported with no retrieval file: {metrics_rows}"
    )
    assert "Not available" in report


def test_the_rollup_refuses_to_invent_a_missing_axis() -> None:
    """Silently omitting an axis would look like a run in which it passed."""
    from memoratum.eval_results import render_rollup

    report = render_rollup([_axis_payload("cost"), _axis_payload("latency")], None)
    for missing in ("isolation", "grounding"):
        assert missing in report.lower(), (
            f"the rollup omitted the {missing} axis entirely; a reader would not know it "
            "was never run"
        )
    assert "NOT RUN" in report


# --- CLI ---------------------------------------------------------------------------


def _write(payload: dict, name: str) -> str:
    path = os.path.join(_tempdir(), name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def test_cli_writes_the_rollup(tmp_path) -> None:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from memoratum import eval_results

    out = tmp_path / "RESULTS.md"
    paths = [_write(_axis_payload(axis), f"{axis}.json") for axis in AXES]
    paths.append(_write(_retrieval_payload(), "longmemeval.json"))
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = eval_results.main([*paths, "--out-md", str(out)])
    assert code == 0, stderr.getvalue()
    assert out.exists()
    body = out.read_text()
    for axis in AXES:
        assert axis in body


def test_cli_rejects_a_missing_file() -> None:
    from memoratum import eval_results

    assert eval_results.main(["/nonexistent/results.json"]) != 0


def test_cli_rejects_malformed_json() -> None:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from memoratum import eval_results

    bad = os.path.join(_tempdir(), "bad.json")
    with open(bad, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = eval_results.main([bad])
    assert code != 0
    assert "JSON" in stderr.getvalue() or "json" in stderr.getvalue()


def test_the_output_is_byte_identical_for_identical_input(tmp_path) -> None:
    """FR-007: no timestamp, no set iteration order, no dict ordering in the artifact.

    Compared by writing to the SAME path twice, because the embedded command legitimately
    records the output path — comparing two different paths would differ for a real reason
    and prove nothing about determinism.
    """
    from memoratum.eval_results import main

    paths = [_write(_axis_payload(axis), f"{axis}.json") for axis in AXES]
    out = tmp_path / "RESULTS.md"
    main([*paths, "--out-md", str(out)])
    first = out.read_text()
    main([*paths, "--out-md", str(out)])
    assert out.read_text() == first

    # And with the axes supplied in a different order, so a set iteration cannot be the
    # source of the output. Only the report body is compared: the embedded command block
    # legitimately records the argv order that ran, which is itself part of the record.
    first_body = first.split("## Regenerate this figure")[0]
    main([*reversed(paths), "--out-md", str(out)])
    assert out.read_text().split("## Regenerate this figure")[0] == first_body
