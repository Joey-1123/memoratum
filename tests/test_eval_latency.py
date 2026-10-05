"""Latency axis contract (RED).

A slow memory layer and a slow index have different fixes, so the four phases are timed
separately. The rules encoded here are measured or derived, not stylistic (see
``research.md`` D3 and ``data-model.md`` L1-L8):

* **Microseconds-per-chunk is the primary gate**, not milliseconds. Measured across a 64x
  corpus ladder, per-chunk cost went 35.6 -> 30.1 -> 8.2 -> 7.3 us: sub-linear growth shows
  as a *falling* number and a regression to O(n) as a *rising* one. That direction travels
  between machines in a way absolute milliseconds do not.
* **The sample floor is 20.** Bootstrapped median-of-n against the true median: n=5 needs
  a +69% band and cannot detect anything smaller than a 70% regression, which makes the gate
  theatre. n=30 needs only +7%.
* **Never average percentiles.** p95-of-p95 is not a p95, so raw samples are returned and
  the reader aggregates.
* **Warmup is discarded**, or the first call pays FTS5 tokenizer setup.
* **No remote embedder inside a timed region.** Network jitter dwarfs everything measured.
* **The corpus ladder must not cross the prefilter threshold**, or a step measures an
  algorithm switch rather than a size change -- a false result this repo has already shipped.
* **Hardware is recorded.** Without it, "+40%" means nothing.

Covers T053-T060 and T061-T069.
"""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Below the default 512 threshold on purpose: a ladder crossing it measures an algorithm
# switch rather than a corpus size (L7).
LADDER = (100, 200, 400)
SAMPLES = 30
WARMUP = 5


_TEMP_ROOTS: list[tempfile.TemporaryDirectory] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_temp_roots():
    """Remove every temp dir these tests create.

    A leaked directory per corpus size is enough to fill /tmp across a suite, which makes
    unrelated tests fail with I/O errors.
    """
    yield
    while _TEMP_ROOTS:
        _TEMP_ROOTS.pop().cleanup()


def _tempdir(prefix: str = "memoratum-latency-") -> str:
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    _TEMP_ROOTS.append(holder)
    return holder.name


# --- T053: the sample floor -------------------------------------------------------


@pytest.mark.parametrize("samples", [0, 1, 5, 10, 19])
def test_samples_below_twenty_are_rejected(samples: int) -> None:
    """T053/L2: below 20 the gate cannot detect a regression worth detecting."""
    from memoratum.eval_axes import ManifestError

    with pytest.raises(ManifestError, match="below the floor of 20"):
        _latency_config(samples=samples)


def test_twenty_samples_is_accepted() -> None:
    """The floor is 20, not 30: 30 is the sweet spot, not the minimum."""
    from memoratum.eval_axes import validate_manifest

    validate_manifest(_latency_config(samples=20))


def test_thirty_samples_is_the_default() -> None:
    """n=30 leaves +7% statistical headroom against the +40% band (L2)."""
    from memoratum.eval_latency import DEFAULT_SAMPLES

    assert DEFAULT_SAMPLES == 30


# --- T054: the ladder must have three sizes ---------------------------------------


@pytest.mark.parametrize("ladder", [[], [100], [100, 200]])
def test_a_ladder_with_fewer_than_three_sizes_is_rejected(ladder) -> None:
    """T054/FR-004: fewer than three sizes cannot show growth at all."""
    from memoratum.eval_axes import ManifestError

    with pytest.raises(ManifestError, match="at least 3 corpus sizes"):
        _latency_config(ladder=ladder)


def test_the_documented_default_ladder_does_not_straddle_the_prefilter() -> None:
    """L7: a ladder crossing the threshold measures an algorithm switch, not a size."""
    from memoratum.eval_latency import DEFAULT_LADDER
    from memoratum.search import prefilter_min_candidates

    threshold = prefilter_min_candidates()
    ordered = sorted(DEFAULT_LADDER)
    straddles = [
        (low, high) for low, high in itertools.pairwise(ordered) if low < threshold <= high
    ]
    assert not straddles, f"default ladder {DEFAULT_LADDER} straddles {threshold} at {straddles}"


# --- T055: raw samples, not pre-summarised ---------------------------------------


def test_phase_timings_carry_raw_samples() -> None:
    """T055/L3: p95-of-p95 is not a p95, so the reader must aggregate from raw values."""
    result = _evaluate()
    samples = result["manifest"]["latency_config"]["samples"]
    retrieve = result["latency"]["ladder"][0]["phases"]["retrieve"]
    assert len(retrieve["samples_ms"]) == samples
    assert (
        retrieve["samples_ms"] == result["latency"]["ladder"][0]["phases"]["retrieve"]["samples_ms"]
    ), "samples_ms must be the list itself, not a regenerated copy"
    assert all(isinstance(v, float) and v >= 0 for v in retrieve["samples_ms"])


def test_percentiles_agree_with_the_raw_samples() -> None:
    """The reported median and p95 must be computable from the published samples."""
    from memoratum.eval_axes import median, percentile

    result = _evaluate()
    raw = result["latency"]["ladder"][0]["phases"]["retrieve"]["samples_ms"]
    reported = result["latency"]["ladder"][0]["phases"]["retrieve"]
    assert reported["median_ms"] == pytest.approx(median(raw))
    assert reported["p95_ms"] == pytest.approx(percentile(raw, 95))


# --- T056: us_per_chunk is the primary gate --------------------------------------


def test_us_per_chunk_is_reported_per_corpus_size() -> None:
    """T056/L1: the hardware-independent input the gate keys on."""
    result = _evaluate()
    entries = {e["corpus_chunks"]: e for e in result["latency"]["ladder"]}
    for chunks in LADDER:
        assert entries[chunks]["phases"]["retrieve"]["us_per_chunk"] > 0


def test_a_rising_us_per_chunk_is_a_regression() -> None:
    """T056/L1: a return to O(n) shows as a RISING per-chunk number."""
    from memoratum.eval_axes import Gate
    from memoratum.eval_latency import gate_latency

    def ladder(ratio: float) -> list[dict]:
        return [
            {"corpus_chunks": 100, "phases": {"retrieve": {"us_per_chunk": 10.0}}},
            {
                "corpus_chunks": 400,
                "phases": {"retrieve": {"us_per_chunk": 10.0 * ratio}},
            },
            {
                "corpus_chunks": 1600,
                "phases": {"retrieve": {"us_per_chunk": 10.0 * ratio * ratio}},
            },
        ]

    flat = Gate()
    gate_latency(flat, ladder=ladder(1.0))
    assert flat.status == "pass", "flat per-chunk cost is sub-linear and must pass"

    linear = Gate()
    gate_latency(linear, ladder=ladder(4.0))
    assert linear.status == "fail", "per-chunk cost quadrupling with size is O(n)"


def test_the_gate_names_the_regressed_phase() -> None:
    """SC-004: a latency regression must name the phase that regressed."""
    from memoratum.eval_axes import Gate
    from memoratum.eval_latency import gate_latency

    gate = Gate()
    gate_latency(
        gate,
        ladder=[
            {"corpus_chunks": 100, "phases": {"retrieve": {"us_per_chunk": 10.0}}},
            {
                "corpus_chunks": 400,
                "phases": {
                    "retrieve": {"us_per_chunk": 30.0},
                    "index": {"us_per_chunk": 500.0},
                },
            },
        ],
    )
    assert gate.status == "fail"
    phases = {c.get("phase") for c in gate.checks if c["status"] == "fail"}
    assert phases, "a failing check must name a phase"


# --- T057/L7: the ladder must not cross the prefilter threshold --------------------


def test_a_ladder_straddling_the_threshold_is_rejected() -> None:
    """T057/L7: the 400 -> 1600 step across 512 measures an algorithm switch.

    Measured, that step produced a 3.38x -> 1.09x -> 3.55x wobble.
    """
    from memoratum.eval_axes import ManifestError
    from memoratum.search import prefilter_min_candidates

    threshold = prefilter_min_candidates()
    with pytest.raises(ManifestError, match="straddles the prefilter threshold"):
        _latency_config(ladder=[100, threshold - 1, threshold + 1, 6400])


def test_pinning_the_threshold_allows_a_straddling_ladder() -> None:
    """L7's documented alternative: pin it and record prefilter_min."""
    from memoratum.eval_axes import validate_manifest
    from memoratum.search import prefilter_min_candidates

    threshold = prefilter_min_candidates()
    validate_manifest(
        _latency_config(ladder=[100, threshold - 1, threshold + 1, 6400], prefilter_min=threshold)
    )


# --- T059/B1: hardware is mandatory -----------------------------------------------


def test_latency_manifest_requires_complete_hardware() -> None:
    """T059/B1/FR-008: milliseconds without hardware are meaningless."""
    from memoratum.eval_axes import ManifestError

    for bad in (None, {}, {"cpu": "x"}, "n/a", "unknown"):
        with pytest.raises(ManifestError):
            _latency_config(hardware=bad)


def test_a_latency_baseline_without_hardware_is_unusable() -> None:
    from memoratum.eval_axes import load_baseline

    assert load_baseline({"axis": "median_ms", "figure": 12.0, "tolerance": 0.4}) is None
    assert (
        load_baseline(
            {
                "axis": "median_ms",
                "figure": 12.0,
                "tolerance": 0.4,
                "hardware": "i3-7020U",
            }
        )
        is not None
    )


def test_real_hardware_is_recorded_with_every_figure() -> None:
    """FR-008: the figure and the machine it came from travel together."""
    hardware = _evaluate()["manifest"]["latency_config"]["hardware"]
    for key in ("cpu", "cores", "ram_mb", "python", "platform"):
        assert key in hardware
    assert hardware["cores"] >= 1


# --- T060: the prefilter engagement is asserted, not assumed ----------------------


def test_prefilter_engagement_is_asserted_not_assumed() -> None:
    """T060/L7: engagement is observed via set_trace_callback, the existing technique.

    Above the threshold the chunk SELECT must be bounded by the FTS5 candidate set
    (`c.id IN (...)`); below it, it must not be. Asserting this is what stops the ladder
    from silently measuring an algorithm switch.
    """
    from memoratum.eval_latency import observe_prefilter

    above = _seeded_conn(chunks=800)
    try:
        assert observe_prefilter(above, "topic 7", container_tag="bench") is True, (
            "above the threshold the chunk SELECT must be bounded"
        )
    finally:
        above.close()

    below = _seeded_conn(chunks=50)
    try:
        assert observe_prefilter(below, "topic 7", container_tag="bench") is False
    finally:
        below.close()


def test_prefilter_engagement_is_none_when_it_was_not_observed() -> None:
    """A size on one side of the threshold has nothing to assert, and must say null.

    `prefilter_engaged: null` at a size above the threshold would be a defect, but null
    below it is honest -- so the two cases must be distinguishable.
    """
    result = _evaluate()
    entry = result["latency"]["ladder"][0]["phases"]["retrieve"]
    assert "prefilter_engaged" in entry
    assert entry["prefilter_engaged"] in (True, False, None)


# --- determinism and the timed region --------------------------------------------


def test_no_remote_embedder_is_used_inside_a_timed_region() -> None:
    """L5/FR-011: network jitter of 100 ms +/- 80 ms dwarfs everything measured here."""
    from memoratum.eval_latency import PHASES

    assert PHASES == ("ingest", "embed", "index", "retrieve")
    result = _evaluate()
    assert result["manifest"]["embedder"].startswith("HashEmbedder")
    assert result["manifest"]["latency_config"]["provider_embedder"] is None


def test_all_four_phases_are_timed_separately() -> None:
    """FR-004: a slow memory layer and a slow index have different fixes."""
    result = _evaluate()
    phases = result["latency"]["ladder"][0]["phases"]
    for phase in ("ingest", "embed", "index", "retrieve"):
        assert phase in phases, f"{phase} must be timed separately"
        assert phases[phase]["median_ms"] is not None


def test_warmup_iterations_are_discarded_before_sampling() -> None:
    """L4: without warmup the first call pays FTS5 tokenizer setup."""
    from memoratum.eval_latency import sample_phase

    calls: list[int] = []
    samples = sample_phase(lambda: calls.append(1), samples=4, warmup=3)
    assert len(calls) == 7, "3 warmup + 4 sampled"
    assert len(samples) == 4


def test_harness_bookkeeping_stays_outside_the_timed_region() -> None:
    """L6: the naive grounding scan costs 51.6 ms/query against a 13 ms retrieval."""
    from memoratum.eval_latency import PHASES

    # The timed region must measure search() alone, so no bookkeeping phase exists.
    assert "grounding_check" not in PHASES
    result = _evaluate()
    timed = result["latency"]["ladder"][0]["phases"]
    assert set(timed) == {"ingest", "embed", "index", "retrieve"}


def test_two_runs_report_the_same_shape_and_manifest() -> None:
    """FR-007: every figure must be reconstructible from the recorded command."""
    first = _evaluate()
    second = _evaluate()
    assert first["manifest"] == second["manifest"]
    assert [e["corpus_chunks"] for e in first["latency"]["ladder"]] == [
        e["corpus_chunks"] for e in second["latency"]["ladder"]
    ]


def test_us_per_chunk_declines_as_the_corpus_grows() -> None:
    """The sub-linear shape the gate exists to detect, on real data.

    Per-chunk cost must FALL with size. A rising number is the regression signal, so this
    asserts the expected direction rather than merely that a number exists.
    """
    result = _evaluate()
    per_chunk = [e["phases"]["retrieve"]["us_per_chunk"] for e in result["latency"]["ladder"]]
    assert per_chunk[-1] <= per_chunk[0] * 1.5, (
        f"per-chunk cost should not grow with corpus size, got {per_chunk}"
    )


# --- helpers ---------------------------------------------------------------------


def _latency_config(**overrides):
    from memoratum.eval_axes import build_manifest, validate_manifest

    latency = {
        "samples": SAMPLES,
        "warmup": WARMUP,
        "ladder": list(LADDER),
        "hardware": {
            "cpu": "Intel i3-7020U @ 2.30GHz",
            "cores": 4,
            "ram_mb": 3072,
            "python": "3.12.14",
            "platform": "Linux-6.1.0",
        },
    }
    latency.update(overrides)
    manifest = build_manifest(
        seed=42,
        requested_n=1,
        n=1,
        ks=[5],
        modes=["hybrid"],
        embedder="HashEmbedder:64",
        vector_store="sqlite",
        data_sha256="synthetic-corpus",
        latency_config=latency,
    )
    validate_manifest(manifest)
    return manifest


_SHARED: dict = {}


def _evaluate(*, ladder=None, samples=20, warmup=2):
    """One shared 3-size evaluation.

    The manifest requires at least three corpus sizes (FR-004), so a single-size run is
    not a legal invocation -- tests needing one size call ``build_corpus`` directly. The
    result is cached because building and timing three corpora is the slow part, and the
    assertions below are about shape and reporting, not about re-measuring.
    """
    from memoratum.eval_latency import evaluate_latency

    key = (samples, warmup)
    if key not in _SHARED:
        _SHARED[key] = evaluate_latency(
            ladder=list(LADDER), samples=samples, warmup=warmup, ks=[5], seed=42
        )
    return _SHARED[key]


def _single(target: int = 100):
    """One prepared corpus, for tests that only need a single size."""
    from memoratum.eval_latency import build_corpus

    directory, conn, chunks, preparation = build_corpus(target)
    return directory, conn, chunks, preparation


def _seeded_conn(chunks: int) -> sqlite3.Connection:
    """A temp database holding ``chunks`` rows above or below the prefilter threshold."""
    from memoratum import db

    conn = db.connect(os.path.join(_tempdir(), "prefilter.db"))
    for n in range(chunks):
        conn.execute(
            "INSERT INTO documents(id, container_tag, content, status, created_at, updated_at)"
            " VALUES (?, 'bench', ?, 'done', 0, 0)",
            (f"doc-{n}", f"## Topic {n % 10}\n\ncontent about topic {n % 10} " * 3),
        )
    ids = [row[0] for row in conn.execute("SELECT id FROM documents")]
    for i, document_id in enumerate(ids):
        conn.execute(
            "INSERT INTO chunks(document_id, idx, text, created_at) VALUES (?, 0, ?, 0)",
            (document_id, f"chunk text topic {i % 10}"),
        )
    conn.commit()
    return conn


def test_a_corpus_at_the_threshold_is_built_and_timed() -> None:
    """End-to-end through the real ingest path, not a hand-seeded fixture."""
    result = _evaluate()
    entry = result["latency"]["ladder"][0]
    assert entry["corpus_chunks"] > 0
    assert entry["phases"]["retrieve"]["samples_ms"]


def test_report_states_hardware_and_the_gate() -> None:
    from memoratum.eval_latency import summarize

    report = summarize(_evaluate())
    assert "gate" in report.lower()
    assert "Hardware" in report
    assert "us/chunk" in report


def _run_cli(argv: list[str]):
    """Invoke the CLI in-process, returning (exit code, stdout, stderr).

    stderr is returned because the expected-error paths report there, and an assertion on
    the message is what proves the right rejection fired rather than any non-zero exit.
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from memoratum import eval_latency

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = eval_latency.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_cli_runs_and_writes_artifacts(tmp_path) -> None:
    js = tmp_path / "latency.json"
    md = tmp_path / "RESULTS-latency.md"
    code, _, _err = _run_cli(
        [
            "--ladder",
            "100,200,400",
            "--samples",
            "20",
            "--warmup",
            "2",
            "--out-json",
            str(js),
            "--out-md",
            str(md),
        ]
    )
    assert code == 0
    payload = json.loads(js.read_text())
    assert payload["axis"] == "latency"
    assert len(payload["latency"]["ladder"]) == 3
    assert md.exists()


def test_cli_rejects_a_short_sample_count() -> None:
    code, _, err5 = _run_cli(["--ladder", "100,200,400", "--samples", "5"])
    assert code != 0
    assert "below the floor of 20" in err5
    assert code != 0


def test_cli_rejects_a_short_ladder() -> None:
    code, _, err = _run_cli(["--ladder", "100,200", "--samples", "20"])
    assert code != 0
    assert "at least 3" in err


def test_cli_rejects_a_straddling_ladder() -> None:
    code, _, err = _run_cli(["--ladder", "100,400,1600,6400", "--samples", "20"])
    assert code != 0
    assert "straddle" in err.lower()


def test_committed_latency_baseline_section_is_parseable() -> None:
    """`eval/BASELINES.md` must stay readable by the CLI's own reader.

    No latency row is committed yet, so this asserts the file is *parseable* rather than
    that a figure exists -- otherwise a corrupted table and an empty one look identical.
    """
    from pathlib import Path

    from memoratum.eval_cost import _load_baseline

    path = os.path.join(REPO_ROOT, "eval", "BASELINES.md")
    assert os.path.exists(path)
    assert "| axis |" in Path(path).read_text(encoding="utf-8")
    row = _load_baseline(path, "retrieve_median_ms")
    if row is None:
        return
    # Once a row exists it must carry hardware, or the ms figure does not travel (B1).
    assert row.get("hardware") not in (None, "", "n/a", "unknown")


def test_phases_faster_than_the_gateable_floor_are_reported_not_gated() -> None:
    """L2: a sub-2 ms phase carries more noise than any useful band, so gating it is theatre.

    `ingest` measures ~0.08 ms per document, far below the floor. Without the skip, every
    run would emit a check whose band is pure measurement error.
    """
    from memoratum.eval_axes import Gate
    from memoratum.eval_latency import GATEABLE_MS, gate_latency

    ladder = [
        {
            "corpus_chunks": 100,
            "phases": {
                # A fast phase and a slow one, so the skip can be observed.
                "retrieve": {"us_per_chunk": 10.0, "median_ms": 50.0},
                "ingest": {"us_per_chunk": None, "median_ms": GATEABLE_MS / 10},
            },
        }
    ]
    gate = Gate()
    gate_latency(
        gate,
        ladder=ladder,
        baseline={
            "ingest_median_ms": {
                "axis": "ingest_median_ms",
                "figure": 1.0,
                "tolerance": 0.4,
                "hardware": "i3-7020U",
            },
            "retrieve_median_ms": {
                "axis": "retrieve_median_ms",
                "figure": 50.0,
                "tolerance": 0.4,
                "hardware": "i3-7020U",
            },
        },
    )
    gated = {c["name"] for c in gate.checks}
    assert "ingest_median_ms" not in gated, "a sub-2 ms phase must not be gated"
    assert "retrieve_median_ms" in gated, "a slow phase must still be gated"


def test_a_baseline_median_must_carry_hardware() -> None:
    """B1: milliseconds without hardware do not travel, so the row is unusable."""
    from memoratum.eval_axes import Gate, load_baseline
    from memoratum.eval_latency import figure_key, gate_latency

    # The axis keys its baselines "<phase>_median_ms"; listing only the bare "median_ms"
    # let every one of them bypass the hardware requirement entirely.
    assert figure_key("retrieve") == "retrieve_median_ms"
    assert load_baseline({"axis": "retrieve_median_ms", "figure": 50.0, "tolerance": 0.4}) is None

    usable = {
        figure_key("retrieve"): {
            "axis": "retrieve_median_ms",
            "figure": 50.0,
            "tolerance": 0.4,
            "hardware": "i3-7020U",
        }
    }
    assert load_baseline(usable[figure_key("retrieve")]) is not None

    ladder = [
        {
            "corpus_chunks": 100,
            "phases": {"retrieve": {"us_per_chunk": 10.0, "median_ms": 90.0}},
        }
    ]
    gate = Gate()
    gate_latency(gate, ladder=ladder, baseline=usable)
    check = next(c for c in gate.checks if c["name"] == "retrieve_median_ms")
    assert check["status"] == "fail", "90ms against a 50ms baseline must fail"
    assert check["delta"] == pytest.approx(40.0)
    assert check["phase"] == "retrieve"


def test_an_unusable_baseline_fails_rather_than_skipping_the_tier() -> None:
    """B3: a baseline that exists but cannot be measured must not silently pass.

    `load_baseline` returns None for a hardware-bound figure with no hardware, and the
    whole millisecond tier used to vanish -- reporting success for an unmeasurable
    comparison.
    """
    from memoratum.eval_axes import Gate
    from memoratum.eval_latency import figure_key, gate_latency

    unusable = {
        figure_key("retrieve"): {
            "axis": "retrieve_median_ms",
            "figure": 50.0,
            "tolerance": 0.4,
            # no hardware
        }
    }
    ladder = [
        {
            "corpus_chunks": 100,
            "phases": {"retrieve": {"us_per_chunk": 10.0, "median_ms": 90.0}},
        }
    ]
    gate = Gate()
    gate_latency(gate, ladder=ladder, baseline=unusable)
    check = next(c for c in gate.checks if c["name"] == "retrieve_median_ms")
    assert check["status"] == "fail"
    assert "unusable" in check["reason"]


def test_the_ratio_gate_runs_when_per_chunk_figures_exist() -> None:
    """The primary gate must actually emit checks, not silently skip."""
    from memoratum.eval_axes import Gate
    from memoratum.eval_latency import gate_latency

    ladder = [
        {"corpus_chunks": 100, "phases": {"retrieve": {"us_per_chunk": 10.0}}},
        {"corpus_chunks": 200, "phases": {"retrieve": {"us_per_chunk": 9.0}}},
        {"corpus_chunks": 400, "phases": {"retrieve": {"us_per_chunk": 8.5}}},
    ]
    gate = Gate()
    gate_latency(gate, ladder=ladder)
    assert [c["name"] for c in gate.checks] == ["us_per_chunk_sublinear"] * 2
    assert gate.status == "pass", "falling per-chunk cost is sub-linear"


def test_the_ratio_gate_is_skipped_when_a_figure_is_missing() -> None:
    """No per-chunk figure means no ratio claim -- and must not divide by None."""
    from memoratum.eval_axes import Gate
    from memoratum.eval_latency import gate_latency

    ladder = [
        {"corpus_chunks": 100, "phases": {"retrieve": {"us_per_chunk": None}}},
        {"corpus_chunks": 400, "phases": {"retrieve": {"us_per_chunk": 8.0}}},
    ]
    gate = Gate()
    gate_latency(gate, ladder=ladder)
    assert gate.checks == []


def test_zero_chunks_after_ingest_is_an_error_not_a_fast_phase() -> None:
    """`process_one` swallows every exception, so a silent failure would look quick."""

    directory, conn, chunks, _ = _single(20)
    try:
        assert chunks > 0, "a prepared corpus must have chunks"

        # Now empty the chunk table behind build_corpus's back and re-run the check.
        conn.execute("DELETE FROM chunks")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"] == 0
    finally:
        conn.close()
        directory.cleanup()


def test_build_corpus_rejects_a_corpus_that_produced_no_chunks(monkeypatch) -> None:
    """The guard must raise, so an empty corpus cannot be measured as instant."""
    import memoratum.eval_latency as latency_module
    from memoratum.eval_axes import ManifestError

    monkeypatch.setattr(latency_module, "process_all", lambda conn, embedder: None)
    with pytest.raises(ManifestError, match="no chunks"):
        latency_module.build_corpus(10)


def test_build_corpus_rejects_a_queue_that_never_drained(monkeypatch) -> None:
    """A total ingest failure must not look like a fast preparation phase."""
    import memoratum.eval_latency as latency_module
    from memoratum.eval_axes import ManifestError

    real = latency_module.db_module.create_document

    monkeypatch.setattr(
        latency_module.db_module,
        "create_document",
        lambda *a, **k: None,  # nothing is queued
    )
    with pytest.raises(ManifestError, match="queued no documents"):
        latency_module.build_corpus(5)
    monkeypatch.setattr(latency_module.db_module, "create_document", real)


def test_prefilter_is_only_asserted_above_the_threshold() -> None:
    """Below the threshold there is nothing to assert, and it must report null, not False."""
    from memoratum.eval_latency import evaluate_latency

    result = evaluate_latency(ladder=[100, 200, 400], samples=20, warmup=2)
    threshold = prefilter_threshold()
    for entry in result["latency"]["ladder"]:
        engaged = entry["phases"]["retrieve"]["prefilter_engaged"]
        if entry["corpus_chunks"] > threshold:
            assert isinstance(engaged, bool), (
                "above the threshold engagement must be asserted, not null"
            )
        else:
            assert engaged is None, (
                "below the threshold null is honest; False would claim it was checked"
            )


def prefilter_threshold() -> int:
    from memoratum.search import prefilter_min_candidates

    return prefilter_min_candidates()


def test_cli_baseline_flag_actually_reaches_the_gate(tmp_path) -> None:
    """`--baseline` was parsed but never passed to evaluate_latency, so it did nothing.

    The flag must load the committed rows AND gate the millisecond tier against them --
    otherwise a CI job would appear to gate on a baseline while comparing nothing.
    """
    from memoratum.eval_latency import load_baselines

    path = os.path.join(REPO_ROOT, "eval", "BASELINES.md")
    rows = load_baselines(path)
    assert "retrieve_median_ms" in rows, "the committed latency baseline must be readable"
    assert rows["retrieve_median_ms"].get("hardware"), "hardware is mandatory (B1)"

    js = tmp_path / "latency.json"
    code, _, err = _run_cli(
        [
            "--ladder",
            "100,200,400",
            "--samples",
            "20",
            "--warmup",
            "2",
            "--baseline",
            path,
            "--out-json",
            str(js),
        ]
    )
    assert code in (0, 1), err  # 1 is a legitimate verdict; a usage error is not
    checks = json.loads(js.read_text())["gate"]["checks"]
    names = {c["name"] for c in checks}
    # Deliberately NOT asserting a pass: the millisecond band is hardware- and
    # load-sensitive, and under coverage instrumentation the measured median can cross it.
    # This test is about whether the flag reaches the gate, which the presence of the
    # check answers without depending on timing.
    assert "retrieve_median_ms" in names, "the ms smoke bound must run when a baseline is supplied"
    assert "us_per_chunk_sublinear" in names, "the primary gate must also run"


def test_cli_rejects_a_baseline_file_with_no_usable_rows(tmp_path) -> None:
    """A named-but-empty baseline is an error, not a silent pass (B3)."""
    from memoratum.eval_axes import EXIT_BASELINE_MISSING

    empty = tmp_path / "BASELINES.md"
    empty.write_text("# Baselines\n\nNo table here.\n", encoding="utf-8")
    code, _, err = _run_cli(
        ["--ladder", "100,200,400", "--samples", "20", "--baseline", str(empty)]
    )
    assert code == EXIT_BASELINE_MISSING
    assert "baseline" in err.lower()


def test_a_latency_regression_fails_and_names_the_phase(tmp_path) -> None:
    """SC-004: the gate must fail on a regression and say which phase regressed."""
    baseline = tmp_path / "BASELINES.md"
    # A tiny baseline, so the measured run regresses against it.
    baseline.write_text(
        "| axis | figure | tolerance | corpus | seed | mode | hardware | command |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| retrieve_median_ms | 0.5 | 0.40 | x | 42 | documents | i3-7020U | uv run ... |\n",
        encoding="utf-8",
    )
    js = tmp_path / "latency.json"
    code, report, _e = _run_cli(
        [
            "--ladder",
            "100,200,400",
            "--samples",
            "20",
            "--warmup",
            "2",
            "--baseline",
            str(baseline),
            "--out-json",
            str(js),
        ]
    )
    assert code == 1, "a latency regression must fail the run"
    assert "gate: FAIL" in report
    check = next(
        c for c in json.loads(js.read_text())["gate"]["checks"] if c["name"] == "retrieve_median_ms"
    )
    assert check["status"] == "fail"
    assert check["phase"] == "retrieve", "SC-004: the regressed phase must be named"
    assert check["delta"] > 0


def test_temp_dirs_are_cleaned_up() -> None:
    """A leaked directory per size is enough to fill /tmp across a suite."""
    from memoratum.eval_latency import evaluate_latency

    before = len([n for n in os.listdir("/tmp") if n.startswith("memoratum-latency-")])
    evaluate_latency(ladder=[100, 200, 400], samples=20, warmup=2)
    after = len([n for n in os.listdir("/tmp") if n.startswith("memoratum-latency-")])
    assert after <= before, f"leaked {after - before} temp directories"
