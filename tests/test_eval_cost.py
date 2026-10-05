"""Token-cost axis contract (RED).

Cost is the metric that makes a memory store comparable against no memory at all. It is
exactly measurable here and no retrieval metric provides it.

Design rules that are load-bearing (see `research.md` D2 and `data-model.md` C1-C6):

* **Characters are canonical; bytes are rejected.** The corpus is pure ASCII so bytes
  carry zero information, yet on non-ASCII they inflate 1.1-3x with nothing visible in
  the report. Characters are also the unit the codebase already spends: `eval_mc10.py`
  truncates with `context[:12000]`.
* **The chars-per-token ratio is MEASURED and recorded, never hardcoded.** A real BPE
  tokenizer gives ~4.0-4.2 chars/token for English prose, so whitespace tokens overcount
  by ~1.5x -- but that figure cannot be verified without a tokenizer dependency the
  project refuses (Principle V). So it is a calibration constant, not a fact.
* **The estimate is defensible only as a RATIO between two runs on the same corpus.**
  "This query costs 1,850 tokens" would be a guess inheriting every tokenizer difference.
* **Tokenizer presence must not change the gated figure**, or the metric becomes
  machine-dependent (C6, FR-007).
* **A regression must report a delta**, or SC-005 is not satisfied.

Covers T037-T044 and T045-T052.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LONGMEMEVAL = os.path.join(REPO_ROOT, "data", "longmemeval_s_cleaned.json")

_TEMP_ROOTS: list[tempfile.TemporaryDirectory] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_temp_roots():
    """Remove every temp dir these tests create.

    Each ``evaluate_cost`` call builds a real SQLite database per question, so this suite
    is the largest temp consumer. Leaking them filled ``/tmp``, which made unrelated tests
    fail with I/O errors and truncated a source file mid-write.
    """
    yield
    while _TEMP_ROOTS:
        _TEMP_ROOTS.pop().cleanup()


def _tempdir(prefix: str = "memoratum-cost-") -> str:
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    _TEMP_ROOTS.append(holder)
    return holder.name


def _dataset() -> str:
    """A small local corpus; no network, no provider, no upload (FR-010)."""
    path = os.path.join(_tempdir(), "corpus.json")
    sessions = []
    for n in range(12):
        # Sections long enough that split_markdown emits several chunks per session, so
        # the retriever genuinely returns multiple hits sharing one source row. Without
        # this, redundancy is structurally impossible and the metric could not be tested.
        sections = "\n\n".join(
            f"## Line item {n}-{i}\n\n"
            "The quarterly budget review covers travel and lodging for the team."
            for i in range(4)
        )
        body = sections
        sessions.append(
            {
                "question_id": f"q{n}",
                "question": "what is scheduled for tuesday?",
                "question_type": "single-session-user",
                "answer": "the quarterly budget review",
                "answer_session_ids": [f"s{n % 6}"],
                "haystack_dates": ["2026-01-01"] * 6,
                "haystack_session_ids": [f"s{i}" for i in range(6)],
                "haystack_sessions": [
                    {
                        "session_id": f"s{i}",
                        "date": "2026-01-01",
                        "turns": [
                            {"role": "user", "content": f"Session {i} note. " + body},
                        ],
                    }
                    for i in range(6)
                ],
            }
        )
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(sessions, handle)
    return path


def _evaluate(path=None, **kwargs):
    from memoratum.eval_cost import evaluate_cost

    defaults = {
        "data": path or _dataset(),
        "n": 6,
        "seed": 42,
        "ks": [5, 10],
        "mode": "hybrid",
    }
    defaults.update(kwargs)
    return evaluate_cost(**defaults)


# --- T037: the reported figures --------------------------------------------------


def test_reports_mean_p95_and_total_characters() -> None:
    """T037/FR-003: mean, p95 and total retrieved characters per query."""
    result = _evaluate()
    cost = result["cost"]
    assert cost["queries"] > 0
    assert cost["hits"] > 0
    assert cost["retrieved_chars_total"] > 0
    assert cost["retrieved_chars_mean"] > 0
    assert cost["retrieved_chars_p95"] > 0
    # The mean is per QUERY (what one retrieval costs in context budget), so it divides
    # by queries, not hits. Dividing by hits would report cost-per-chunk instead.
    assert cost["retrieved_chars_mean"] == pytest.approx(
        cost["retrieved_chars_total"] / cost["queries"], rel=0.01
    )
    # Per-query figures must also be present, so the distribution is reproducible.
    assert len(cost["per_query_chars"]) == cost["queries"]


def test_reports_whitespace_tokens_as_a_companion_unit() -> None:
    """The companion unit, which is a sound proxy for relative comparison (CV 7.5%)."""
    cost = _evaluate()["cost"]
    assert cost["retrieved_ws_tokens_mean"] > 0
    assert cost["unit"] == "chars"


def test_manifest_records_the_measured_ratio_and_retrieval_budget() -> None:
    """FR-007: the manifest must carry enough to reproduce the figure."""
    config = _evaluate()["manifest"]["cost_config"]
    assert config["unit"] == "chars"
    assert config["chars_per_ws_token"] > 0
    assert config["retrieval_limit"] >= 10
    assert config["mean_chunks_per_document"] > 0
    assert _evaluate()["manifest"]["schema"] == "longmemeval-scoped-v2"


def test_manifest_validates() -> None:
    from memoratum.eval_axes import validate_manifest

    validate_manifest(_evaluate()["manifest"])


# --- T038: redundancy explains a cost difference ---------------------------------


def test_redundant_hits_are_counted() -> None:
    """T038: several hits share one source document, and that must be visible.

    Redundancy is what makes a cost difference attributable when recall is unchanged.
    """
    cost = _evaluate()["cost"]
    assert cost["redundant_hits"] > 0, (
        "a 6-session corpus must produce duplicate source rows; 0 would mean redundancy "
        "is not actually being detected"
    )
    assert cost["redundant_hits"] < cost["hits"]


def test_redundancy_is_attributable_to_a_source_row() -> None:
    """A redundant hit must name the row it duplicates, or it is just a number."""
    cost = _evaluate()["cost"]
    assert cost["duplicate_source_rows"] > 0
    assert cost["redundant_hits"] >= cost["duplicate_source_rows"]


# --- T039/C1: bytes is not a unit -------------------------------------------------


def test_bytes_is_not_an_accepted_unit() -> None:
    """T039/C1: bytes must be rejected at the API, not merely discouraged."""
    from memoratum.eval_axes import ManifestError

    with pytest.raises(ManifestError, match="bytes"):
        _evaluate(unit="bytes")


@pytest.mark.parametrize("bad", ["b", "byte", "utf8_bytes", "CHARS", "", None])
def test_unknown_units_are_rejected_too(bad) -> None:
    """C1 is an allowlist. On ASCII, 'b' or 'utf8_bytes' are numerically identical to
    chars, so an unrecognised string would produce a report that looks fine and inflates
    on non-ASCII."""
    from memoratum.eval_axes import ManifestError

    with pytest.raises(ManifestError):
        _evaluate(unit=bad)


@pytest.mark.parametrize("unit", ["chars", "ws_tokens"])
def test_supported_units_are_accepted_and_select_the_gated_figure(unit: str) -> None:
    """Switching unit must move the GATED figure, and must not change what was retrieved.

    Otherwise the unit is a label rather than a choice, and a baseline recorded in one
    unit would be compared against a figure measured in another (C1).
    """
    result = _evaluate(unit=unit)
    assert result["manifest"]["cost_config"]["unit"] == unit
    cost = result["cost"]
    assert cost["unit"] == unit
    expected = (
        cost["retrieved_ws_tokens_mean"] if unit == "ws_tokens" else cost["retrieved_chars_mean"]
    )
    assert cost["retrieved_mean"] == pytest.approx(expected)
    # Whitespace tokens are fewer than characters for the same text.
    assert cost["retrieved_ws_tokens_mean"] < cost["retrieved_chars_mean"]


def test_both_units_are_always_recorded_whatever_the_unit() -> None:
    """So a baseline can be compared in the unit it was recorded in."""
    for unit in ("chars", "ws_tokens"):
        cost = _evaluate(unit=unit)["cost"]
        assert cost["retrieved_chars_mean"] > 0
        assert cost["retrieved_ws_tokens_mean"] > 0
        assert cost["retrieved_chars_total"] > 0
        assert cost["retrieved_ws_tokens_total"] > 0


# --- T040/C2: the ratio is measured, never hardcoded -----------------------------


def test_ratio_is_measured_from_the_corpus_not_hardcoded() -> None:
    """T040/C2: no hardcoded 4.0. The value must come from the corpus in front of us."""
    ratio = _evaluate()["cost"]["chars_per_ws_token"]
    assert 3.0 < ratio < 12.0, (
        f"measured ratio {ratio} is outside any plausible range for English prose, so it "
        "is probably a hardcoded constant rather than a measurement"
    )
    # A real BPE tokenizer gives ~4.0-4.2 chars/token, so a value near 4.0 means the
    # tokenizer figure was hardcoded instead of the corpus being measured.
    assert ratio > 4.5, f"ratio {ratio} looks like a hardcoded tokenizer constant (C2)"


def test_ratio_changes_with_the_corpus() -> None:
    """A measurement must respond to its input; a constant cannot."""
    from memoratum.eval_cost import measure_chars_per_ws_token

    prose = ["the quick brown fox jumps over the lazy dog again and again"]
    dense = ["aaaaaaaaaa bbbbbbbbbb cccccccccc dddddddddd"]
    assert measure_chars_per_ws_token(prose) != measure_chars_per_ws_token(dense)
    assert measure_chars_per_ws_token(prose) > 0


def test_measure_ratio_handles_empty_input_as_none() -> None:
    """M3: nothing to measure is None, never 0.0."""
    from memoratum.eval_cost import measure_chars_per_ws_token

    assert measure_chars_per_ws_token([]) is None
    assert measure_chars_per_ws_token(["", "   "]) is None


@pytest.mark.skipif(
    not os.path.exists(LONGMEMEVAL), reason="LongMemEval corpus is not present locally"
)
def test_real_corpus_ratio_is_measured_and_recorded() -> None:
    """The real 277 MB corpus, streamed rather than loaded (T050).

    The research phase measured 6.27 chars per whitespace token over 690 sessions; this
    reproduces that from a prefix and proves the reader survives the file size.
    """
    result = evaluate_cost_real_corpus(n=4)
    ratio = result["cost"]["chars_per_ws_token"]
    assert 5.5 < ratio < 7.5, f"real corpus ratio {ratio} is far from the measured 6.27"


@pytest.mark.skipif(
    not os.path.exists(LONGMEMEVAL), reason="LongMemEval corpus is not present locally"
)
@pytest.mark.parametrize(
    ("drop", "reason"),
    [
        ("raw_list", "raw haystack_sessions is a bare turn list"),
        ("normalised_text", "a normalised session carries pre-rendered text"),
    ],
)
def test_real_corpus_text_extraction_handles_both_session_shapes(drop: str, reason: str) -> None:
    """The real corpus and the fixtures use DIFFERENT session shapes.

    Raw LongMemEval puts the turn list straight into ``haystack_sessions``; the
    normalised form is a dict with ``turns`` or ``text``. Supporting only one silently
    yields zero texts, which leaves the ratio ``None`` and forces the fabricated-number
    fallback -- so each shape is asserted directly.
    """
    from memoratum.eval_cost import _session_texts

    records = head_records(LONGMEMEVAL, limit=3)
    raw_sessions = [r["haystack_sessions"] for r in records if r.get("haystack_sessions")]
    assert raw_sessions, "the real corpus must expose haystack_sessions"

    if drop == "raw_list":
        # Feeding only the bare turn lists must still produce text.
        texts = _session_texts([{"haystack_sessions": raw_sessions[0]}])
        assert texts, f"a bare turn list must be extracted ({reason})"
    else:
        texts = _session_texts([{"sessions": [{"session_id": "s0", "text": "some session body"}]}])
        assert texts == ["some session body"], reason

    # And the aggregate must be measurable, not None.
    assert measure_chars_per_ws_token(_session_texts(records)) is not None


def evaluate_cost_real_corpus(n: int):
    from memoratum.eval_cost import evaluate_cost

    return evaluate_cost(data=LONGMEMEVAL, n=n, seed=42, ks=[5, 10], mode="hybrid")


def head_records(path: str, *, limit: int):
    from memoratum.eval_cost import head_records as _head_records

    return _head_records(path, limit=limit)


def measure_chars_per_ws_token(texts):
    from memoratum.eval_cost import measure_chars_per_ws_token as _measure

    return _measure(texts)


# --- T041/C3: the token estimate is labelled --------------------------------------


def test_token_estimate_carries_its_caveat() -> None:
    """T041/C3: an unlabelled token number would be read as an absolute fact."""
    cost = _evaluate()["cost"]
    assert cost["token_estimate_mean"] is not None
    caveat = cost["token_estimate_caveat"]
    assert caveat
    assert "ratio" in caveat.lower()
    assert "same corpus" in caveat.lower()


def test_token_estimate_scales_with_the_measured_ratio() -> None:
    """The estimate is derived, so it must move when the ratio moves."""
    from memoratum.eval_cost import estimate_tokens

    assert estimate_tokens(600.0, 6.0) == pytest.approx(100.0)
    assert estimate_tokens(600.0, 12.0) == pytest.approx(50.0)
    assert estimate_tokens(None, 6.0) is None
    assert estimate_tokens(600.0, 0) is None


# --- T042/C6: the gated figure is tokenizer-independent ---------------------------


def test_gated_figure_is_identical_with_and_without_a_tokenizer() -> None:
    """T042/C6/FR-007: the gated metric must not depend on what happens to be installed."""
    path = _dataset()
    without = _evaluate(path=path)
    with_tokenizer = _evaluate(path=path, tokenizer="fake-bpe")
    assert with_tokenizer["cost"]["retrieved_chars_mean"] == without["cost"]["retrieved_chars_mean"]
    assert with_tokenizer["cost"]["hits"] == without["cost"]["hits"]
    # A configured tokenizer is reported, but in its own field.
    assert with_tokenizer["cost"]["tokenizer"] == "fake-bpe"
    assert without["cost"]["tokenizer"] is None


def test_tokenizer_never_appears_in_a_gated_field() -> None:
    """A tokenizer figure must not leak into the gated set (C6)."""
    gated = {"retrieved_chars_mean", "retrieved_chars_p95", "retrieved_chars_total"}
    for result in (_evaluate(), _evaluate(tokenizer="fake-bpe")):
        cost = result["cost"]
        assert gated <= set(cost)
        assert cost["unit"] == "chars"
        assert result["gate"]["status"] in {"pass", "fail"}


# --- T043/B3: gating --------------------------------------------------------------


def test_a_regression_fails_and_reports_a_delta() -> None:
    """T043/SC-005: a failure with no delta does not satisfy the criterion."""
    from memoratum.eval_axes import Gate
    from memoratum.eval_cost import gate_cost

    result = _evaluate()
    measured = result["cost"]["retrieved_chars_mean"]
    baseline = {"axis": "retrieved_chars_mean", "figure": measured * 0.5, "tolerance": 0.1}

    gate = Gate()
    gate_cost(gate, cost=result["cost"], baseline=baseline)
    assert gate.status == "fail"
    check = next(c for c in gate.checks if c["name"] == "retrieved_chars_mean")
    assert check["delta"] == pytest.approx(measured - baseline["figure"])


def test_within_tolerance_passes() -> None:
    from memoratum.eval_axes import Gate
    from memoratum.eval_cost import gate_cost

    result = _evaluate()
    measured = result["cost"]["retrieved_chars_mean"]
    baseline = {"axis": "retrieved_chars_mean", "figure": measured, "tolerance": 0.1}

    gate = Gate()
    gate_cost(gate, cost=result["cost"], baseline=baseline)
    assert gate.status == "pass"


@pytest.mark.parametrize(
    "baseline", [None, {}, {"axis": "retrieved_chars_mean", "figure": "see below"}]
)
def test_missing_baseline_fails_rather_than_passing(baseline) -> None:
    """B3: an absent baseline is null and null fails."""
    from memoratum.eval_axes import Gate
    from memoratum.eval_cost import gate_cost

    result = _evaluate()
    gate = Gate()
    gate_cost(gate, cost=result["cost"], baseline=baseline)
    assert gate.status == "fail", baseline


def test_a_run_with_no_baseline_reports_figures_without_inventing_a_verdict() -> None:
    result = _evaluate(baseline=None)
    assert result["cost"]["retrieved_chars_mean"] > 0
    assert result["gate"]["status"] == "fail", "an ungated run must not read as a pass"


# --- T051: the budget must not be inflated ----------------------------------------


def test_retrieval_budget_is_not_inflated() -> None:
    """T051/C5: the budget existed to feed the OLD metric. Inflating it now masks signal.

    Under the corrected `R@k`, `max(ks)` hits is all the metric reads, so the existing
    `max(2*max(ks), 10)` is merely generous. A budget scaled by mean-chunks-per-document
    would be treating a symptom that no longer exists.
    """
    from memoratum.eval_cost import retrieval_limit

    assert retrieval_limit([5, 10]) == 20, "must match eval_longmemeval's existing budget"
    assert retrieval_limit([10]) == 20
    assert retrieval_limit([]) == 10


def test_cost_and_recall_share_the_hit_unit() -> None:
    """C4: after the v1 metric fix, both count HITS.

    A session-counting cost figure beside a hit-counting recall figure would overstate
    cost by 3-7x, which is what the 277 MB corpus's ~7.6 chunks/session produces.
    """
    cost = _evaluate()["cost"]
    assert cost["hits"] == cost["returned_hits"], (
        "cost must count the same unit the corrected recall metric reads"
    )


# --- T050: the corpus reader must not exhaust memory -----------------------------


def test_corpus_is_read_without_loading_the_whole_file() -> None:
    """T050: `load_records` reads a 277 MB file as one string, which OOMs a small host."""
    from memoratum.eval_cost import head_records

    records = head_records(_dataset(), limit=3)
    assert len(records) == 3
    assert records[0]["question_id"] == "q0"


def test_head_records_handles_jsonl_and_bare_arrays() -> None:
    from memoratum.eval_cost import head_records

    jsonl = os.path.join(_tempdir(), "records.jsonl")
    with open(jsonl, "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps({"question_id": f"q{n}"}) + "\n" for n in range(5))

    assert [r["question_id"] for r in head_records(jsonl, limit=3)] == ["q0", "q1", "q2"]
    assert len(head_records(_dataset(), limit=100)) == 12


def test_head_records_rejects_a_non_array_root() -> None:
    from memoratum.eval_cost import head_records

    path = os.path.join(_tempdir(), "bad.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"question_id": "q0"}')
    with pytest.raises(ValueError, match="array"):
        head_records(path, limit=1)


def test_head_records_rejects_an_empty_limit() -> None:
    from memoratum.eval_cost import head_records

    with pytest.raises(ValueError, match="limit"):
        head_records(_dataset(), limit=0)


# --- determinism ------------------------------------------------------------------


def test_two_runs_with_the_same_seed_are_identical() -> None:
    """FR-007: every reported figure must be reproducible from the recorded command."""
    path = _dataset()
    assert _evaluate(path=path)["manifest"] == _evaluate(path=path)["manifest"]
    assert _evaluate(path=path)["cost"] == _evaluate(path=path)["cost"]


def test_a_different_seed_is_reflected_in_the_manifest() -> None:
    result = _evaluate(seed=7)
    assert result["manifest"]["seed"] == 7
    assert result["manifest"]["n"] <= result["manifest"]["requested_n"]


# --- report and CLI ---------------------------------------------------------------


def test_report_states_the_unit_and_labels_the_estimate() -> None:
    from memoratum.eval_cost import summarize

    report = summarize(_evaluate())
    assert "chars" in report
    assert "never gates" in report
    assert "Manifest" in report


def test_a_baselineless_run_does_not_read_as_a_regression() -> None:
    """B3 makes a missing baseline FAIL, but that means "unproven", not "regressed".

    A header reading `gate: FAIL` on a fresh baseline-less run would be read as a cost
    regression by anyone skimming CI output.
    """
    from memoratum.eval_cost import summarize

    ungated = summarize(_evaluate(baseline=None))
    assert "NO VERDICT" in ungated
    assert "unproven" in ungated

    measured = _evaluate()["cost"]["retrieved_chars_mean"]
    gated = summarize(
        _evaluate(baseline={"axis": "retrieved_chars_mean", "figure": measured, "tolerance": 0.1})
    )
    assert "gate: PASS" in gated
    assert "NO VERDICT" not in gated


def _run_cli(argv: list[str]):
    import io
    from contextlib import redirect_stdout

    from memoratum import eval_cost

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = eval_cost.main(argv)
    return code, buffer.getvalue()


def test_cli_runs_and_writes_artifacts(tmp_path) -> None:
    js = tmp_path / "cost.json"
    md = tmp_path / "RESULTS-cost.md"
    code, report = _run_cli(
        [
            "--data",
            _dataset(),
            "--n",
            "4",
            "--k",
            "5,10",
            "--out-json",
            str(js),
            "--out-md",
            str(md),
        ]
    )
    assert code == 0
    payload = json.loads(js.read_text())
    assert payload["axis"] == "cost"
    assert payload["cost"]["retrieved_chars_mean"] > 0
    assert "retrieved_chars_mean" in report
    assert md.exists()


def test_cli_rejects_the_bytes_unit() -> None:
    code, _ = _run_cli(["--data", _dataset(), "--n", "2", "--cost-unit", "bytes"])
    assert code != 0


def test_cli_rejects_a_missing_required_flag() -> None:
    """argparse exits 2 when --data is omitted. That is a usage error, not a gate
    failure, so it must not be confused with "the cost regressed"."""
    import pytest as _pytest

    with _pytest.raises(SystemExit) as excinfo:
        _run_cli(["--n", "2"])
    assert excinfo.value.code == 2


def test_cli_rejects_a_missing_corpus() -> None:
    code, _ = _run_cli(["--data", "/nonexistent/corpus.json", "--n", "2"])
    assert code != 0


# --- the BASELINES.md reader (T052) ----------------------------------------------


def test_baseline_is_read_from_a_committed_markdown_table(tmp_path) -> None:
    """The baselines file is Markdown, so the reader must parse the real table shape."""
    from memoratum.eval_cost import _load_baseline

    path = tmp_path / "BASELINES.md"
    path.write_text(
        "# Evaluation baselines\n\n"
        "## Cost baselines\n\n"
        "| axis | figure | tolerance | corpus | seed | mode | hardware | command |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| retrieved_chars_mean | 1790.2 | 0.1 | longmemeval-s | 42 | hybrid | n/a | uv run ... |\n"
        "\n"
        "## Latency baselines\n\n"
        "| axis | figure | tolerance | corpus | seed | mode | hardware | command |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| median_ms | 12.0 | 0.4 | longmemeval-s | 42 | hybrid | i3-7020U | uv run ... |\n",
        encoding="utf-8",
    )
    row = _load_baseline(str(path), "retrieved_chars_mean")
    assert row is not None
    assert float(row["figure"]) == pytest.approx(1790.2)
    assert float(row["tolerance"]) == pytest.approx(0.1)
    # And a latency figure must not be mistaken for a cost one.
    assert _load_baseline(str(path), "median_ms")["figure"] == "12.0"
    assert _load_baseline(str(path), "us_per_chunk") is None


def test_baseline_reader_returns_none_for_a_missing_file_or_row(tmp_path) -> None:
    from memoratum.eval_cost import _load_baseline

    assert _load_baseline("", "retrieved_chars_mean") is None
    assert _load_baseline("/nonexistent/BASELINES.md", "retrieved_chars_mean") is None

    path = tmp_path / "empty.md"
    path.write_text("# Baselines\n\nNo table here.\n", encoding="utf-8")
    assert _load_baseline(str(path), "retrieved_chars_mean") is None


def test_committed_baselines_file_is_parseable() -> None:
    """The shipped eval/BASELINES.md must be readable by the CLI's own reader."""
    from memoratum.eval_cost import _load_baseline

    path = os.path.join(REPO_ROOT, "eval", "BASELINES.md")
    assert os.path.exists(path)
    row = _load_baseline(path, "retrieved_chars_mean")
    if row is None:
        # No cost row committed yet. That is acceptable while the axis is new, but the
        # file must at least be parseable -- a malformed table would also return None and
        # be indistinguishable from "no row yet".
        assert "| axis |" in Path(path).read_text(encoding="utf-8")
        return
    assert float(row["figure"]) > 0
    assert float(row["tolerance"]) >= 0
    assert row.get("corpus"), "a committed baseline must state its corpus (FR-008)"


def test_cli_gates_against_a_baselines_file(tmp_path) -> None:
    """End-to-end: a regression against a committed baseline must exit non-zero (SC-005)."""
    baseline = tmp_path / "BASELINES.md"
    # A deliberately tiny baseline, so the measured run regresses against it.
    baseline.write_text(
        "| axis | figure | tolerance | corpus | seed | mode | hardware | command |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| retrieved_chars_mean | 1.0 | 0.1 | x | 42 | hybrid | n/a | uv run ... |\n",
        encoding="utf-8",
    )
    js = tmp_path / "cost.json"
    code, report = _run_cli(
        [
            "--data",
            _dataset(),
            "--n",
            "3",
            "--k",
            "5",
            "--baseline",
            str(baseline),
            "--out-json",
            str(js),
        ]
    )
    assert code == 1, "a cost regression must fail the run"
    assert "gate: FAIL" in report
    payload = json.loads(js.read_text())
    check = next(c for c in payload["gate"]["checks"] if c["name"] == "retrieved_chars_mean")
    assert check["status"] == "fail"
    assert check["delta"] > 0, "SC-005 requires the delta to be reported"


def test_cli_rejects_a_baseline_file_with_no_matching_row(tmp_path) -> None:
    """A named-but-absent baseline is an error, not a silent pass (B3)."""
    baseline = tmp_path / "BASELINES.md"
    baseline.write_text(
        "| axis | figure | tolerance | corpus | seed | mode | hardware | command |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| median_ms | 12.0 | 0.4 | x | 42 | hybrid | i3 | uv run ... |\n",
        encoding="utf-8",
    )
    code, _ = _run_cli(["--data", _dataset(), "--n", "2", "--baseline", str(baseline)])
    assert code != 0
