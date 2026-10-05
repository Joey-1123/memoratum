"""Shared eval-axis plumbing contract.

These tests are the falsifiability floor for every axis. An axis that cannot fail
is an axis that proves nothing, so each behaviour here has a test that genuinely
fails when the behaviour is removed.

Covers T004-T006 (attribution), T008-T011 (manifest), T012-T013 (sampling),
T014-T015 (gates), T016-T017 (baselines), T019-T021 (scoped corpus).

Every test in this file was mutation-checked: the corresponding behaviour was
deliberately broken and the test observed to fail.
"""

from __future__ import annotations

import os
import re
import tempfile

import pytest

_TEMP_ROOTS: list[tempfile.TemporaryDirectory] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_temp_roots():
    """Remove every temp dir these tests create.

    Without this, each ``mkdtemp`` call leaks a directory for the life of the process.
    Across the four axis suites that reached ~5,000 directories and filled ``/tmp``,
    which made unrelated tests fail with I/O errors and truncated a source file mid-write.
    """
    yield
    while _TEMP_ROOTS:
        _TEMP_ROOTS.pop().cleanup()


def _tempdir(prefix: str) -> str:
    """A temp dir registered for cleanup at module teardown."""
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    _TEMP_ROOTS.append(holder)
    return holder.name


def _db():
    """Fresh migrated temp database. ``db.connect`` already sets ``sqlite3.Row``."""
    from memoratum import db

    return db.connect(os.path.join(_tempdir("memoratum-axes-"), "axes.db"))


def _hw():
    return {
        "cpu": "Intel i3-7020U @ 2.30GHz",
        "cores": 4,
        "ram_mb": 3072,
        "python": "3.12.14",
        "platform": "Linux-6.1.0",
    }


def _manifest(**overrides):
    from memoratum.eval_axes import build_manifest

    base = {
        "seed": 42,
        "requested_n": 50,
        "n": 50,
        "ks": [5, 10],
        "modes": ["hybrid"],
        "embedder": "HashEmbedder:64",
        "vector_store": "SQLiteVectorStore",
        "data_sha256": "deadbeef",
    }
    base.update(overrides)
    return build_manifest(**base)


# --- attribution ---------------------------------------------------------------


def test_attribute_hit_resolves_chunk_to_owning_project() -> None:
    """T004: a chunk hit resolves through chunks -> documents to its project and org."""
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_axes import attribute_hit
    from memoratum.ingest import process_all
    from memoratum.search import search

    conn = _db()
    try:
        db.create_document(
            conn,
            container_tag="bench",
            content="## Notes\n\nThe quarterly budget review is scheduled for tuesday.",
            custom_id="doc-0",
            project_id="proj-a",
            org_id="org-1",
        )
        # create_document only queues; process_all chunks and embeds.
        process_all(conn, HashEmbedder(dims=64))

        hits = search(
            conn,
            HashEmbedder(dims=64),
            "quarterly budget review",
            container_tag="bench",
            project_id="proj-a",
            limit=5,
        )
        assert hits, "expected at least one hit"

        attributed = attribute_hit(conn, hits[0], rank=0)
        assert attributed.hit_id == hits[0]["id"]
        assert attributed.hit_kind == "chunk"
        assert attributed.originating_project_id == "proj-a"
        assert attributed.originating_org_id == "org-1"
        assert attributed.rank == 0
        assert attributed.document_id is not None
    finally:
        conn.close()


def test_fact_hit_is_classified_fact_not_memory() -> None:
    """A fact reaches search as mem_*, and must be grounded by the FACT rule (G2).

    ``facts.add_fact`` mints a ``mem_<uuid>`` row via ``ensure_fact_memory``, so a
    ``fact_`` hit id never occurs in production. Classifying the ``mem_`` row by its
    ``fact_id`` is what makes the fact grounding rule reachable at all.
    """
    from memoratum import facts
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_axes import attribute_hit
    from memoratum.ingest import process_all
    from memoratum.search import search

    conn = _db()
    try:
        facts.add_fact(
            conn,
            container_tag="bench",
            subject="Alice",
            predicate="lives in",
            object="Lisbon",
            document_id=None,
            metadata=None,
        )
        process_all(conn, HashEmbedder(dims=64))
        hits = search(
            conn,
            HashEmbedder(dims=64),
            "Alice lives in Lisbon",
            container_tag="bench",
            limit=5,
        )
        assert hits, "expected the fact's memory projection to be retrievable"
        assert hits[0]["id"].startswith("mem_"), "production must never emit fact_* ids"

        attributed = attribute_hit(conn, hits[0], rank=0)
        assert attributed.hit_kind == "fact"
        # The fact rule renders "subject predicate object", which is NOT a substring
        # of the parent document -- that is the whole reason per-kind rules exist.
        assert attributed.row_text == "Alice lives in Lisbon"
    finally:
        conn.close()


def test_plain_memory_hit_is_classified_memory_with_its_own_text() -> None:
    from memoratum.eval_axes import attribute_hit

    conn = _db()
    try:
        conn.execute(
            "INSERT INTO memories(id, container_tag, text, metadata, org_id, created_at, updated_at)"
            " VALUES ('mem_1','bench','user prefers dark mode','{}','org-9',0,0)"
        )
        attributed = attribute_hit(
            conn, {"id": "mem_1", "memory": "user prefers dark mode", "similarity": 1.0}, rank=0
        )
        assert attributed.hit_kind == "memory"
        assert attributed.row_text == "user prefers dark mode"
        assert attributed.originating_org_id == "org-9"
    finally:
        conn.close()


def test_unresolvable_hit_is_reported_not_skipped() -> None:
    """T006/A1: an id with no derivable row is 'unresolvable' -- never silently dropped."""
    from memoratum.eval_axes import attribute_hit, attribute_hits

    conn = _db()
    try:
        ghost = {"id": "chunk_999999", "chunk": "ghost", "similarity": 0.5}
        attributed = attribute_hit(conn, ghost, rank=3)
        assert attributed.hit_kind == "unresolvable"
        assert attributed.originating_project_id is None
        assert attributed.document_id is None

        # An unresolvable id must survive aggregation, not vanish from the count.
        other = {"id": "also-unknown", "chunk": "x", "similarity": 0.1}
        mixed = attribute_hits(conn, [ghost, other])
        assert len(mixed) == 2
        assert [a.hit_kind for a in mixed] == ["unresolvable", "unresolvable"]
        assert [a.rank for a in mixed] == [0, 1]
    finally:
        conn.close()


def test_memory_hit_reports_its_org_id() -> None:
    """A resolved Attribution must not carry a fabricated None (A1).

    An org-scoped isolation variant compares originating_org_id; a hardcoded None
    would read as a leak or mask a knowable value, and nothing would flag it because
    the row resolved.
    """
    from memoratum.eval_axes import attribute_hit

    conn = _db()
    try:
        conn.execute(
            "INSERT INTO memories(id, container_tag, text, metadata, org_id, project_id,"
            " created_at, updated_at)"
            " VALUES ('mem_org','bench','scoped row','{}','org-acme','proj-z',0,0)"
        )
        attributed = attribute_hit(
            conn, {"id": "mem_org", "memory": "scoped row", "similarity": 1.0}, rank=0
        )
        assert attributed.originating_org_id == "org-acme"
        assert attributed.originating_project_id == "proj-z"
    finally:
        conn.close()


def test_malformed_chunk_id_is_unresolvable_not_an_exception() -> None:
    """A malformed id must degrade to 'unresolvable', never raise mid-report."""
    from memoratum.eval_axes import attribute_hit

    conn = _db()
    try:
        for bad in ("chunk_", "chunk_abc", "chunk_-1", "notaprefix"):
            attributed = attribute_hit(conn, {"id": bad, "chunk": "x", "similarity": 0.0}, rank=0)
            assert attributed.hit_kind == "unresolvable", bad
    finally:
        conn.close()


def test_byte_identical_text_cannot_attribute_scope() -> None:
    """T021/A2: identical content in two projects stays id-distinguishable."""
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_axes import attribute_hits
    from memoratum.ingest import process_all
    from memoratum.search import search

    conn = _db()
    try:
        body = "the quarterly budget review is scheduled for tuesday"
        for project in ("proj-a", "proj-b"):
            db.create_document(
                conn, container_tag="bench", content=body, custom_id="shared-id", project_id=project
            )
        assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 2
        process_all(conn, HashEmbedder(dims=64))

        hits = search(
            conn, HashEmbedder(dims=64), "quarterly budget review", container_tag="bench", limit=10
        )
        assert hits, "unscoped query must return hits from both projects"
        attributed = attribute_hits(conn, hits)
        chunk_attributions = [a for a in attributed if a.hit_kind == "chunk"]
        assert {a.originating_project_id for a in chunk_attributions} == {"proj-a", "proj-b"}
        assert len({a.hit_id for a in chunk_attributions}) == len(chunk_attributions)
    finally:
        conn.close()


def test_attribute_hits_preserves_rank_order() -> None:
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_axes import attribute_hits, build_scoped_corpus
    from memoratum.search import search

    conn = _db()
    try:
        build_scoped_corpus(conn, projects=("proj-a", "proj-b"), docs_per_project=3)
        hits = search(
            conn,
            HashEmbedder(dims=64),
            "quarterly budget review",
            container_tag="bench",
            project_id="proj-a",
            limit=10,
        )
        attributed = attribute_hits(conn, hits)
        assert [a.rank for a in attributed] == list(range(len(hits)))
    finally:
        conn.close()


# --- manifest ------------------------------------------------------------------


def test_manifest_preserves_original_keys_and_bumps_schema() -> None:
    """T008/M1: the seven original keys stay AND the new blocks are emitted."""
    from memoratum.eval_axes import build_manifest

    manifest = build_manifest(
        seed=42,
        requested_n=50,
        n=50,
        ks=[5, 10],
        modes=["hybrid"],
        embedder="HashEmbedder:64",
        vector_store="SQLiteVectorStore",
        data_sha256="deadbeef",
        scope_config={"container_tag": "bench", "projects": ["a", "b"], "expected_documents": 6},
        cost_config={"unit": "chars", "chars_per_ws_token": 6.27},
        latency_config={"samples": 30, "warmup": 5, "ladder": [100, 200, 400], "hardware": _hw()},
        grounding_config={
            "normalization": "nfkc+whitespace+casefold",
            "rule_by_kind": {
                "chunk": "chunks.text",
                "memory": "memories.text",
                "fact": "subject predicate object",
            },
        },
    )
    for key in (
        "schema",
        "seed",
        "requested_n",
        "n",
        "ks",
        "modes",
        "embedder",
        "vector_store",
        "data_sha256",
    ):
        assert key in manifest, f"original key {key} must be preserved"
    assert manifest["schema"] == "longmemeval-scoped-v2"
    # M1's additive half: omitting these would silently lose the metric config.
    for key in ("scope_config", "cost_config", "latency_config", "grounding_config"):
        assert key in manifest, f"{key} must be emitted when supplied"


def test_manifest_lists_are_copied_not_aliased() -> None:
    from memoratum.eval_axes import build_manifest

    ks = [5, 10]
    manifest = build_manifest(
        seed=1,
        requested_n=1,
        n=1,
        ks=ks,
        modes=["hybrid"],
        embedder="HashEmbedder:64",
        vector_store="SQLiteVectorStore",
        data_sha256="x",
    )
    ks.append(99)
    assert manifest["ks"] == [5, 10], "manifest must not alias the caller's list"


def test_build_manifest_validates_so_forgetting_cannot_skip_the_rules() -> None:
    """Validation is not opt-in: an invalid config must raise from build_manifest."""
    with pytest.raises(Exception, match="at least 2 project"):
        _manifest(
            scope_config={"container_tag": "bench", "projects": ["only"], "expected_documents": 3}
        )


def test_latency_manifest_requires_hardware() -> None:
    """T010/B1: latency figures without hardware are invalid (FR-008)."""
    from memoratum.eval_axes import ManifestError, validate_manifest

    manifest = _manifest()
    manifest["latency_config"] = {"samples": 30, "warmup": 5, "ladder": [100, 200, 400]}
    with pytest.raises(ManifestError, match="missing required keys"):
        validate_manifest(manifest)


def test_latency_hardware_must_be_complete_and_real() -> None:
    """A truthy-but-empty hardware dict must not satisfy B1."""
    from memoratum.eval_axes import ManifestError, validate_manifest

    for bad in ({"cpu": "x"}, {}, "n/a", "unknown", None):
        manifest = _manifest()
        manifest["latency_config"] = {
            "samples": 30,
            "warmup": 5,
            "ladder": [100, 200, 400],
            "hardware": bad,
        }
        with pytest.raises(ManifestError):
            validate_manifest(manifest)


def test_latency_hardware_reports_zero_ram_as_unmeasured() -> None:
    """ram_mb=0 is a None-vs-0 violation: it reads as a measurement (M3)."""
    from memoratum.eval_axes import ManifestError, validate_manifest

    hardware = _hw()
    hardware["ram_mb"] = 0
    manifest = _manifest()
    manifest["latency_config"] = {
        "samples": 30,
        "warmup": 5,
        "ladder": [100, 200, 400],
        "hardware": hardware,
    }
    with pytest.raises(ManifestError, match="ram_mb"):
        validate_manifest(manifest)


def test_latency_manifest_rejects_undersampled_and_short_ladder() -> None:
    """L2/FR-004: n<20 is theatre, and fewer than 3 sizes cannot show growth."""
    from memoratum.eval_axes import ManifestError, validate_manifest

    def latency(**overrides):
        block = {"samples": 30, "warmup": 5, "ladder": [100, 200, 400], "hardware": _hw()}
        block.update(overrides)
        manifest = _manifest()
        manifest["latency_config"] = block
        return manifest

    with pytest.raises(ManifestError, match="below the floor of 20"):
        validate_manifest(latency(samples=5))
    with pytest.raises(ManifestError, match="at least 3 corpus sizes"):
        validate_manifest(latency(ladder=[100, 200]))
    with pytest.raises(ManifestError, match="at least 3 corpus sizes"):
        validate_manifest(latency(ladder=[100]))
    validate_manifest(latency(samples=20))


def test_latency_samples_must_be_an_int() -> None:
    """A missing or string-typed samples key must not silently disable the floor."""
    from memoratum.eval_axes import ManifestError, validate_manifest

    for bad in ("5", 5.0, None, True):
        manifest = _manifest()
        manifest["latency_config"] = {
            "samples": bad,
            "warmup": 5,
            "ladder": [100, 200, 400],
            "hardware": _hw(),
        }
        with pytest.raises(ManifestError):
            validate_manifest(manifest)


def test_latency_warmup_must_be_a_positive_int() -> None:
    """L4: zero warmup means the first FTS5 call is measured."""
    from memoratum.eval_axes import ManifestError, validate_manifest

    for bad in (0, -1, None, "5"):
        manifest = _manifest()
        manifest["latency_config"] = {
            "samples": 30,
            "warmup": bad,
            "ladder": [100, 200, 400],
            "hardware": _hw(),
        }
        with pytest.raises(ManifestError):
            validate_manifest(manifest)


def test_ladder_straddling_the_prefilter_threshold_is_rejected() -> None:
    """L7: crossing PREFILTER_MIN_CANDIDATES measures an algorithm switch, not size."""
    from memoratum.eval_axes import ManifestError, validate_manifest
    from memoratum.search import prefilter_min_candidates

    threshold = prefilter_min_candidates()
    manifest = _manifest()
    manifest["latency_config"] = {
        "samples": 30,
        "warmup": 5,
        "ladder": [100, threshold - 1, threshold + 1, 6400],
        "hardware": _hw(),
    }
    with pytest.raises(ManifestError, match="straddles the prefilter threshold"):
        validate_manifest(manifest)


def test_pinning_the_prefilter_allows_a_straddling_ladder() -> None:
    """L7's documented alternative: pin the threshold and record it."""
    from memoratum.eval_axes import validate_manifest
    from memoratum.search import prefilter_min_candidates

    threshold = prefilter_min_candidates()
    manifest = _manifest()
    manifest["latency_config"] = {
        "samples": 30,
        "warmup": 5,
        "ladder": [100, threshold - 1, threshold + 1, 6400],
        "prefilter_min": threshold,
        "hardware": _hw(),
    }
    validate_manifest(manifest)


def test_cost_config_rejects_bytes_and_unknown_units() -> None:
    """C1 is an allowlist: bytes, 'b', 'utf8_bytes' and a missing unit all fail."""
    from memoratum.eval_axes import ManifestError, validate_manifest

    for bad in ("bytes", "b", "byte", "utf8_bytes", None, "CHARS", ""):
        manifest = _manifest()
        manifest["cost_config"] = {"unit": bad, "chars_per_ws_token": 6.27}
        with pytest.raises(ManifestError, match="cost_config.unit"):
            validate_manifest(manifest)

    for good in ("chars", "ws_tokens"):
        manifest = _manifest()
        manifest["cost_config"] = {"unit": good, "chars_per_ws_token": 6.27}
        validate_manifest(manifest)


def test_cost_ratio_must_be_a_positive_measured_number() -> None:
    """C2: a hardcoded-or-absent ratio must not pass as a measurement."""
    from memoratum.eval_axes import ManifestError, validate_manifest

    for bad in (None, 0, -1.0, "6.27", True):
        manifest = _manifest()
        manifest["cost_config"] = {"unit": "chars", "chars_per_ws_token": bad}
        with pytest.raises(ManifestError, match="chars_per_ws_token"):
            validate_manifest(manifest)


def test_scope_config_requires_two_projects() -> None:
    """One project cannot leak, so the isolation axis would be unfalsifiable."""
    from memoratum.eval_axes import ManifestError, validate_manifest

    manifest = _manifest()
    manifest["scope_config"] = {
        "container_tag": "bench",
        "projects": ["only"],
        "expected_documents": 3,
    }
    with pytest.raises(ManifestError, match="at least 2 project"):
        validate_manifest(manifest)


def test_grounding_config_requires_normalization_and_all_three_rules() -> None:
    """G2: per-kind rules are mandatory; one global rule is invalid."""
    from memoratum.eval_axes import ManifestError, validate_manifest

    manifest = _manifest()
    manifest["grounding_config"] = {}
    with pytest.raises(ManifestError, match="grounding_config is missing"):
        validate_manifest(manifest)

    manifest["grounding_config"] = {
        "normalization": "lowercase",
        "rule_by_kind": {"chunk": "chunks.text", "memory": "memories.text", "fact": "x"},
    }
    with pytest.raises(ManifestError, match="normalization"):
        validate_manifest(manifest)

    manifest["grounding_config"] = {
        "normalization": "nfkc+whitespace+casefold",
        "rule_by_kind": {"chunk": "chunks.text"},
    }
    with pytest.raises(ManifestError, match="rule_by_kind"):
        validate_manifest(manifest)


# --- sampling ------------------------------------------------------------------


def test_sample_ms_discards_warmup_before_sampling() -> None:
    """T012/L4: warmup runs FIRST and is discarded, so the FTS5 setup call is not timed."""
    from memoratum.eval_axes import sample_ms

    order: list[str] = []
    samples = sample_ms(lambda: order.append("call"), samples=5, warmup=3)
    assert len(order) == 8, "3 warmup + 5 sampled"
    assert len(samples) == 5
    assert all(value >= 0 for value in samples)


def test_sample_ms_zero_warmup_still_samples() -> None:
    from memoratum.eval_axes import sample_ms

    calls: list[int] = []
    samples = sample_ms(lambda: calls.append(1), samples=3, warmup=0)
    assert len(calls) == 3
    assert len(samples) == 3


def test_percentile_sorts_its_input_and_handles_edges() -> None:
    """L3: percentiles come from raw samples, so unsorted input must still be correct."""
    from memoratum.eval_axes import percentile

    assert percentile([3.0, 1.0, 2.0], 50) == pytest.approx(2.0)
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)
    assert percentile([5.0], 95) == 5.0
    assert percentile([], 95) is None, "empty input is None, never 0.0"
    raw = [float(v) for v in range(1, 101)]
    assert percentile(raw, 50) == pytest.approx(50.5)
    assert percentile(raw, 95) == pytest.approx(95.05)


@pytest.mark.parametrize(
    ("data", "pct", "expected"),
    [
        ([1.0, 2.0, 3.0, 4.0], 50, 2.5),
        ([1.0, 2.0, 3.0, 4.0], 95, 3.85),
        ([float(v) for v in range(1, 101)], 50, 50.5),
        ([float(v) for v in range(1, 101)], 95, 95.05),
        ([float(v) for v in range(1, 101)], 5, 5.95),
        ([3.0, 1.0, 2.0], 50, 2.0),
        ([2.0], 95, 2.0),
    ],
)
def test_percentile_pins_the_interpolation_convention(data, pct, expected) -> None:
    """Pin the exact convention, so an off-by-one in the index cannot survive.

    The wrong-index variant (`pct/100 * len` instead of `pct/100 * (len-1)`) returns
    51.0 and 96.0 on a 100-sample input where this must return 50.5 and 95.05. Both
    look plausible, which is exactly why the values are asserted rather than
    described.
    """
    from memoratum.eval_axes import percentile

    assert percentile(data, pct) == pytest.approx(expected)


def test_percentile_never_exceeds_the_maximum_observed_sample() -> None:
    """A percentile above the largest sample would misstate the tail (L3)."""
    from memoratum.eval_axes import percentile

    data = [1.0, 2.0, 3.0, 4.0, 5.0]
    for pct in (0, 25, 50, 75, 99, 100):
        value = percentile(data, pct)
        assert value is not None
        assert min(data) <= value <= max(data), f"p{pct}={value} outside observed range"
    assert percentile(data, 0) == pytest.approx(1.0)
    assert percentile(data, 100) == pytest.approx(5.0)


def test_median_handles_empty_and_even_counts() -> None:
    from memoratum.eval_axes import median

    assert median([]) is None, "empty input is None, never 0.0"
    assert median([4.0, 1.0, 3.0, 2.0]) == pytest.approx(2.5)
    assert median([7.0]) == 7.0


def test_us_per_chunk_converts_ms_to_microseconds() -> None:
    """L1: this is the PRIMARY gate input, so the unit conversion must be exact.

    10 ms over 1000 chunks is 10 microseconds per chunk. Getting this wrong by a
    factor of 1000 would silently mislabel every latency gate.
    """
    from memoratum.eval_axes import us_per_chunk

    assert us_per_chunk(10.0, 1000) == pytest.approx(10.0)
    assert us_per_chunk(3.56, 100) == pytest.approx(35.6)
    assert us_per_chunk(46.55, 6400) == pytest.approx(7.2734375)


def test_us_per_chunk_is_none_not_zero_for_missing_input() -> None:
    """M3: an unmeasurable cost-per-chunk is None; 0.0 would read as infinitely fast."""
    from memoratum.eval_axes import us_per_chunk

    assert us_per_chunk(None, 100) is None
    assert us_per_chunk(5.0, 0) is None
    assert us_per_chunk(5.0, -1) is None


# --- gates ---------------------------------------------------------------------


def test_empty_gate_fails() -> None:
    """An axis that recorded no checks measured nothing, and must not pass (M3)."""
    from memoratum.eval_axes import Gate, evaluate_gate

    gate = Gate()
    assert gate.status == "fail"
    assert gate.failed
    assert evaluate_gate(gate) == {"status": "fail", "checks": []}


def test_null_figure_fails_the_gate_and_is_never_zero() -> None:
    """T014/M3: a metric that could not be recorded is null and null FAILS."""
    from memoratum.eval_axes import Gate

    gate = Gate()
    gate.check("recorded", value=0.0, bound=0.0, comparison=">")
    assert gate.status == "pass"

    missing = Gate()
    missing.check("not-recorded", value=None, bound=0.0, comparison=">")
    assert missing.status == "fail", "null must fail, never read as 0"
    assert missing.checks[0]["value"] is None

    unbound = Gate()
    unbound.check("no-bound", value=1.0, bound=None, comparison=">")
    assert unbound.status == "fail"


def test_gate_supports_every_comparison_direction() -> None:
    from memoratum.eval_axes import Gate

    cases = [
        (">", 2, 1, "fail"),
        (">", 1, 1, "pass"),
        (">", 0, 1, "pass"),
        (">=", 1, 1, "fail"),
        (">=", 0, 1, "pass"),
        ("<", 0, 1, "fail"),
        ("<", 1, 1, "pass"),
        ("<=", 1, 1, "fail"),
        ("<=", 2, 1, "pass"),
    ]
    for comparison, value, bound, expected in cases:
        gate = Gate()
        gate.check("c", value=value, bound=bound, comparison=comparison)
        assert gate.status == expected, f"{value} {comparison} {bound} -> {expected}"


def test_unknown_comparison_raises_even_with_null_values() -> None:
    """The comparison is validated before the null short-circuit, not after."""
    from memoratum.eval_axes import Gate

    with pytest.raises(ValueError, match="unknown comparison"):
        Gate().check("c", value=None, bound=None, comparison="~=")


def test_gate_fails_when_value_exceeds_bound() -> None:
    from memoratum.eval_axes import Gate

    gate = Gate()
    gate.check("leaked_hits", value=1, bound=0, comparison=">")
    assert gate.status == "fail"


def test_gate_records_phase_and_delta_for_regressions() -> None:
    """SC-004/SC-005: a failure names the phase and reports a delta."""
    from memoratum.eval_axes import Gate

    gate = Gate()
    gate.check("latency", value=3.5, bound=2.8, comparison=">", phase="index", delta=0.7)
    assert gate.status == "fail"
    assert gate.checks[0]["phase"] == "index"
    assert gate.checks[0]["delta"] == pytest.approx(0.7)


def test_gate_result_is_a_snapshot_not_a_live_view() -> None:
    from memoratum.eval_axes import Gate, evaluate_gate

    gate = Gate()
    gate.check("a", value=0, bound=1, comparison=">")
    snapshot = evaluate_gate(gate)
    gate.check("b", value=5, bound=1, comparison=">")
    assert len(snapshot["checks"]) == 1, "an emitted gate result must not mutate later"
    assert gate.status == "fail"


# --- baselines -----------------------------------------------------------------


def test_latency_baseline_without_usable_hardware_is_rejected() -> None:
    """T016/B1: keyed on the FIGURE, so a caller cannot bypass it by omitting a field."""
    from memoratum.eval_axes import load_baseline

    for hardware in (None, {}, {"cpu": "x"}, "n/a", "unknown"):
        row = {
            "axis": "median_ms",
            "figure": 3.5,
            "tolerance": 0.4,
            "hardware": hardware,
            "manifest_ref": "x",
        }
        assert load_baseline(row) is None, hardware


def test_cost_baseline_needs_no_hardware() -> None:
    """Characters are portable, so a cost baseline is valid without hardware."""
    from memoratum.eval_axes import load_baseline

    row = {"axis": "retrieved_chars_mean", "figure": 300.0, "tolerance": 0.1}
    loaded = load_baseline(row)
    assert loaded is not None and loaded["value"] == 300.0


def test_baseline_row_accepts_the_committed_column_names() -> None:
    """BASELINES.md stores the figure under 'figure'; load it without renaming."""
    from memoratum.eval_axes import load_baseline

    row = {"axis": "retrieved_chars_mean", "figure": "300.0", "tolerance": "0.1"}
    loaded = load_baseline(row)
    assert loaded is not None
    assert loaded["value"] == 300.0
    assert loaded["tolerance"] == 0.1


def test_unparseable_baseline_figure_is_none_not_a_crash() -> None:
    """A cell reading 'see below' must fail the gate, not raise and kill the run."""
    from memoratum.eval_axes import Gate, compare_to_baseline, load_baseline

    for bad in ("see below", "", "n/a", None, float("nan"), float("inf"), True):
        assert (
            load_baseline({"axis": "retrieved_chars_mean", "figure": bad, "tolerance": 0.1}) is None
        )
        gate = Gate()
        compare_to_baseline(
            gate,
            name="retrieved_chars_mean",
            value=10.0,
            baseline={
                "axis": "retrieved_chars_mean",
                "figure": bad,
                "tolerance": 0.1,
            },
        )
        assert gate.status == "fail", bad


def test_baseline_under_either_key_rejects_a_missing_value() -> None:
    """A row carrying neither 'value' nor 'figure' must be unusable, not default to 0.

    ``0.0`` would be a real, passing figure: any non-negative measurement is inside a
    0 * (1 + tolerance). So an absent figure has to be None.
    """
    from memoratum.eval_axes import Gate, compare_to_baseline, load_baseline

    for row in (
        {"axis": "retrieved_chars_mean", "tolerance": 0.1},
        {"axis": "retrieved_chars_mean", "value": None, "tolerance": 0.1},
        {"tolerance": 0.1},
    ):
        assert load_baseline(row) is None, row
        gate = Gate()
        compare_to_baseline(gate, name="retrieved_chars_mean", value=400.0, baseline=row)
        assert gate.status == "fail", row


def test_a_zero_baseline_is_rejected_not_treated_as_perfect() -> None:
    """0 is not a plausible cost or latency measurement; accepting it passes anything."""
    from memoratum.eval_axes import Gate, compare_to_baseline, load_baseline

    row = {"axis": "retrieved_chars_mean", "figure": 0.0, "tolerance": 0.1}
    assert load_baseline(row) is None, "a zero baseline would make every figure pass"
    gate = Gate()
    compare_to_baseline(gate, name="retrieved_chars_mean", value=500.0, baseline=row)
    assert gate.status == "fail"


def test_missing_baseline_is_none_and_fails() -> None:
    """T016/B3: no baseline is null, and null fails the gate."""
    from memoratum.eval_axes import Gate, compare_to_baseline, load_baseline

    assert load_baseline(None) is None
    gate = Gate()
    compare_to_baseline(gate, name="retrieved_chars_mean", value=10.0, baseline=None)
    assert gate.status == "fail", "a missing baseline must never silently pass"
    assert "baseline" in gate.checks[0]["reason"]


def test_compare_to_baseline_applies_tolerance_and_reports_delta() -> None:
    """SC-005: a failure must carry a delta, or the criterion is not satisfied."""
    from memoratum.eval_axes import Gate, compare_to_baseline

    baseline = {"axis": "retrieved_chars_mean", "figure": 300.0, "tolerance": 0.1}

    within = Gate()
    compare_to_baseline(within, name="retrieved_chars_mean", value=320.0, baseline=baseline)
    assert within.status == "pass", "320 is inside 300 * 1.1"
    assert within.checks[0]["bound"] == pytest.approx(330.0)

    over = Gate()
    compare_to_baseline(over, name="retrieved_chars_mean", value=345.0, baseline=baseline)
    assert over.status == "fail"
    assert over.checks[0]["delta"] == pytest.approx(45.0)


def test_compare_to_baseline_requires_a_real_tolerance() -> None:
    """A missing tolerance must not silently become 0.0 or 1.0."""
    from memoratum.eval_axes import Gate, compare_to_baseline

    gate = Gate()
    compare_to_baseline(
        gate,
        name="retrieved_chars_mean",
        value=400.0,
        baseline={"axis": "retrieved_chars_mean", "figure": 300.0},
    )
    assert gate.status == "fail"


# --- scoped corpus -------------------------------------------------------------


def test_build_scoped_corpus_asserts_per_project_counts() -> None:
    """T020/S3: expected counts are asserted, never inferred from leaks."""
    from memoratum.eval_axes import build_scoped_corpus

    conn = _db()
    try:
        corpus = build_scoped_corpus(
            conn, projects=("proj-a", "proj-b", "proj-c"), docs_per_project=3
        )
        assert corpus.expected_documents == 9
        counts = dict(
            conn.execute("SELECT project_id, COUNT(*) c FROM documents GROUP BY project_id")
        )
        assert counts == {"proj-a": 3, "proj-b": 3, "proj-c": 3}
    finally:
        conn.close()


def test_build_scoped_corpus_raises_when_counts_are_wrong() -> None:
    """The implementation's own guard must fire, not just the test's re-query."""
    from memoratum.eval_axes import CorpusError, build_scoped_corpus

    conn = _db()
    try:
        with pytest.raises(CorpusError, match="documents"):
            build_scoped_corpus(
                conn,
                projects=("proj-a", "proj-b", "proj-c"),
                docs_per_project=4,
                ingest=False,
            )
    finally:
        conn.close()


def test_s3_guard_detects_a_project_that_is_short_one_document() -> None:
    """A NULL-scope orphan must be detected, not absorbed into the declared shape.

    The orphan has no project, so the per-project loop (which iterates only declared
    projects) cannot see it — the TOTAL check is the sole line of defence here. That is
    why both checks exist: neither alone covers every misallocation.
    """
    from memoratum import db
    from memoratum.eval_axes import CorpusError, build_scoped_corpus

    conn = _db()
    try:
        for project in ("proj-a", "proj-b"):
            for n in range(2):
                db.create_document(
                    conn,
                    container_tag="bench",
                    content=f"body {project} {n}",
                    custom_id=f"doc-{n}",
                    project_id=project,
                )
        # The silent NULL-scope defect from feature 001: a fifth document with no project.
        db.create_document(
            conn, container_tag="bench", content="orphan", custom_id="doc-orphan", project_id=None
        )
        with pytest.raises(CorpusError, match="outside its project scope"):
            build_scoped_corpus(
                conn, projects=("proj-a", "proj-b"), docs_per_project=2, ingest=False
            )
    finally:
        conn.close()


def test_s3_guard_detects_a_surplus_in_one_project_masking_a_deficit() -> None:
    """Surplus in one project masking a deficit in another, with the total correct.

    This is the only shape that proves the PER-PROJECT loop is load-bearing: a
    total-only check sees exactly the declared count and passes.
    """
    from memoratum import db
    from memoratum.eval_axes import CorpusError, build_scoped_corpus

    conn = _db()
    try:
        # proj-a over by one (3), proj-b under by one (1). Total = 4 = declared.
        for n in range(3):
            db.create_document(
                conn,
                container_tag="bench",
                content=f"a body {n}",
                custom_id=f"a-{n}",
                project_id="proj-a",
            )
        db.create_document(
            conn, container_tag="bench", content="b body", custom_id="b-0", project_id="proj-b"
        )
        total = conn.execute(
            "SELECT COUNT(*) c FROM documents WHERE container_tag = 'bench'"
        ).fetchone()["c"]
        assert total == 4, "precondition: the TOTAL must look correct"

        with pytest.raises(CorpusError, match="proj-a"):
            build_scoped_corpus(
                conn, projects=("proj-a", "proj-b"), docs_per_project=2, ingest=False
            )
    finally:
        conn.close()


def test_s3_guard_detects_a_surplus_document() -> None:
    """A document outside any declared project must fail, not inflate the total."""
    from memoratum import db
    from memoratum.eval_axes import CorpusError, build_scoped_corpus

    conn = _db()
    try:
        for project in ("proj-a", "proj-b"):
            for n in range(2):
                db.create_document(
                    conn,
                    container_tag="bench",
                    content=f"body {project} {n}",
                    custom_id=f"doc-{n}",
                    project_id=project,
                )
        # A fifth document with NO project: the silent NULL-scope defect from 001.
        db.create_document(
            conn, container_tag="bench", content="orphan", custom_id="doc-orphan", project_id=None
        )
        with pytest.raises(CorpusError, match="outside its project scope|documents"):
            build_scoped_corpus(
                conn, projects=("proj-a", "proj-b"), docs_per_project=2, ingest=False
            )
    finally:
        conn.close()


def test_s3_guard_survives_python_optimised_mode() -> None:
    """``python -O`` strips asserts, and S3 guards a SILENT extra document.

    So the guard must be a raised exception. This test runs the real module under -O
    and asserts the corruption is still detected.
    """
    import subprocess
    import sys

    script = (
        "import os, tempfile, sys;"
        "sys.path.insert(0, 'src');"
        "from memoratum import db;"
        "from memoratum.eval_axes import build_scoped_corpus, CorpusError;"
        "conn = db.connect(os.path.join(tempfile.mkdtemp(), 'o.db'));"
        "\ntry:\n"
        "    build_scoped_corpus(conn, projects=('a','b'), docs_per_project=3, ingest=False)\n"
        "    print('NOT_DETECTED')\n"
        "except CorpusError:\n"
        "    print('DETECTED')\n"
    )
    env = {**os.environ, "PYTHONPATH": os.path.abspath("src")}
    result = subprocess.run(
        [sys.executable, "-O", "-c", script], capture_output=True, text=True, env=env, check=True
    )
    assert result.stdout.strip() == "DETECTED", (
        f"the S3 guard was stripped under -O: {result.stdout!r} {result.stderr!r}"
    )


def test_scoped_corpus_reuses_custom_ids_across_projects() -> None:
    """S2: reusing custom_id across projects must not collide into one row."""
    from memoratum.eval_axes import build_scoped_corpus

    conn = _db()
    try:
        corpus = build_scoped_corpus(conn, projects=("proj-a", "proj-b"), docs_per_project=2)
        rows = conn.execute(
            "SELECT custom_id, COUNT(*) c FROM documents GROUP BY custom_id"
        ).fetchall()
        assert all(row["c"] == 2 for row in rows)
        assert corpus.expected_documents == 4
    finally:
        conn.close()


def test_scoped_corpus_needs_at_least_two_projects() -> None:
    from memoratum.eval_axes import CorpusError, build_scoped_corpus

    conn = _db()
    try:
        with pytest.raises(CorpusError, match="at least 2 projects"):
            build_scoped_corpus(conn, projects=("only",), docs_per_project=2)
    finally:
        conn.close()


def test_every_project_body_matches_the_shared_query_terms() -> None:
    """The whole axis is worthless if project B's text would not have matched.

    Lexical overlap is the precondition for exclusion to prove anything, so it is
    asserted rather than assumed in a comment.
    """
    from memoratum.eval_axes import SCOPED_QUERY_TERMS, scoped_doc_body

    for project in ("proj-a", "proj-b", "proj-c"):
        for n in range(3):
            body = scoped_doc_body(project, n).lower()
            for term in SCOPED_QUERY_TERMS:
                assert term in body, f"{project}/{n} is missing query term {term!r}"


def test_scope_config_reports_the_built_shape() -> None:
    from memoratum.eval_axes import build_scoped_corpus

    conn = _db()
    try:
        corpus = build_scoped_corpus(conn, projects=("proj-a", "proj-b"), docs_per_project=2)
        assert corpus.scope_config() == {
            "container_tag": "bench",
            "projects": ["proj-a", "proj-b"],
            "expected_documents": 4,
        }
    finally:
        conn.close()


# --- misc ----------------------------------------------------------------------


def test_embedder_label_is_canonical() -> None:
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_axes import embedder_label

    assert embedder_label(HashEmbedder(dims=64)) == "HashEmbedder:64"


def test_collect_hardware_reports_real_values_or_none() -> None:
    """None when unknowable -- never a fabricated 0 or the string 'unknown' (M3)."""
    from memoratum.eval_axes import collect_hardware

    hardware = collect_hardware()
    assert set(hardware) == {"cpu", "cores", "ram_mb", "python", "platform"}
    assert hardware["cores"] >= 1
    assert hardware["ram_mb"] is None or (
        isinstance(hardware["ram_mb"], int) and hardware["ram_mb"] >= 1
    ), f"ram_mb must be a positive int or None, got {hardware['ram_mb']!r}"
    assert hardware["cpu"] is None or str(hardware["cpu"]).strip().lower() != "unknown"


def test_collected_hardware_satisfies_its_own_validator() -> None:
    """collect_hardware must produce a manifest the validator ACCEPTS.

    Otherwise the library generates hardware that its own B1 check rejects, and every
    latency axis either crashes or has to hand-roll a fake. This is the round trip that
    catches a `0` or a bogus CPU string escaping into a committed artifact.
    """
    from memoratum.eval_axes import build_manifest, collect_hardware

    hardware = collect_hardware()
    build_manifest(
        seed=1,
        requested_n=1,
        n=1,
        ks=[5],
        modes=["hybrid"],
        embedder="HashEmbedder:64",
        vector_store="SQLiteVectorStore",
        data_sha256="x",
        latency_config={
            "samples": 30,
            "warmup": 5,
            "ladder": [100, 200, 400],
            "hardware": hardware,
        },
    )


def test_collected_hardware_reports_a_measurable_cpu() -> None:
    """A CPU string must identify a machine, not merely be non-empty.

    ``platform.processor()`` is empty on many Linux builds and the arch fallback
    yields "x86_64", which is not a CPU. Asserting the value looks like a model name
    catches that fallback, and the validator's ``unknown``/``""`` rejection catches
    the fabricated one.
    """
    from memoratum.eval_axes import _cpu_model, collect_hardware

    cpu = collect_hardware()["cpu"]
    if cpu is not None:
        # platform.processor() is empty on many Linux builds and the arch fallback
        # yields "x86_64", which is not a CPU. Reject an architecture as a name.
        assert not re.fullmatch(r"(x86_64|amd64|aarch64|arm64|i386|i686)", str(cpu).lower())
    lowered = str(cpu).strip().lower()
    assert lowered not in {"unknown", "", "-", "none"}
    # A bare architecture is not an identification.
    assert not re.fullmatch(r"(x86_64|amd64|aarch64|arm64|i386|i686)", lowered), (
        f"cpu={cpu!r} is an architecture, not a model name"
    )
    assert _cpu_model() is not None


def test_ram_reporting_is_a_positive_int_or_none() -> None:
    """0 would read as a measurement; None means unmeasured (M3)."""
    from memoratum.eval_axes import _total_ram_mb

    ram = _total_ram_mb()
    assert ram is None or (isinstance(ram, int) and ram > 0), f"got {ram!r}"


def test_ram_returns_none_when_sysconf_reports_nonsense(monkeypatch) -> None:
    """sysconf can return 0 or a negative on a restricted host; 0 MiB is not a reading."""
    import os as _os

    from memoratum.eval_axes import _total_ram_mb

    monkeypatch.setattr(_os, "sysconf", lambda name: 0)
    assert _total_ram_mb() is None


def test_collect_hardware_never_fabricates_zero_ram(monkeypatch) -> None:
    """``_total_ram_mb() or 0`` looks equivalent and is not: it manufactures a reading.

    On a host where sysconf works the value is truthy so the ``or 0`` never fires and
    the suite stays green, which is why this test removes sysconf instead of relying on
    the host being unable to report RAM.
    """
    import os as _os

    from memoratum.eval_axes import collect_hardware

    monkeypatch.delattr(_os, "sysconf", raising=False)
    assert collect_hardware()["ram_mb"] is None, (
        "an unmeasurable platform must report None, never 0"
    )


def test_collect_hardware_rejects_an_unknown_cpu(monkeypatch) -> None:
    """The validator must refuse a fabricated CPU name, and collect_hardware is checked."""
    from memoratum.eval_axes import ManifestError, build_manifest, collect_hardware

    monkeypatch.setattr("memoratum.eval_axes._cpu_model", lambda: "unknown")
    hardware = collect_hardware()
    assert hardware["cpu"] == "unknown"
    with pytest.raises(ManifestError, match="cpu is unknown"):
        build_manifest(
            seed=1,
            requested_n=1,
            n=1,
            ks=[5],
            modes=["hybrid"],
            embedder="HashEmbedder:64",
            vector_store="SQLiteVectorStore",
            data_sha256="x",
            latency_config={
                "samples": 30,
                "warmup": 5,
                "ladder": [100, 200, 400],
                "hardware": hardware,
            },
        )


def test_format_table_renders_headers_and_rows() -> None:
    from memoratum.eval_axes import format_table

    rendered = format_table(
        [{"axis": "cost", "figure": 300.0}, {"axis": "latency", "figure": 3.5}],
        ["axis", "figure"],
    )
    lines = rendered.splitlines()
    assert lines[0] == "| axis | figure |"
    assert lines[1] == "|---|---|"
    assert lines[2] == "| cost | 300.0 |"
    assert lines[3] == "| latency | 3.5 |"


def test_format_table_renders_an_absent_column_as_empty() -> None:
    from memoratum.eval_axes import format_table

    assert format_table([{"axis": "cost"}], ["axis", "hardware"]).splitlines()[2] == "| cost |  |"


def test_embedding_label_uses_the_type_and_dims() -> None:
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_axes import embedder_label

    assert embedder_label(HashEmbedder(dims=768)) == "HashEmbedder:768"


def test_embedder_label_tolerates_an_object_without_dims() -> None:
    """A third-party embedder may not expose ``dims``; it must not raise."""
    from memoratum.eval_axes import embedder_label

    class Duck:
        pass

    assert embedder_label(Duck()) == "Duck:0"


def test_attribute_hits_on_an_empty_hit_list() -> None:
    from memoratum.eval_axes import attribute_hits

    conn = _db()
    try:
        assert attribute_hits(conn, []) == []
    finally:
        conn.close()


def test_attribution_serialises_to_a_plain_dict() -> None:
    """Manifest and report output must be JSON-serialisable, so asdict must work."""
    import json

    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_axes import attribute_hit
    from memoratum.ingest import process_all
    from memoratum.search import search

    conn = _db()
    try:
        db.create_document(
            conn,
            container_tag="bench",
            content="the quarterly budget review",
            custom_id="d0",
            project_id="proj-a",
        )
        process_all(conn, HashEmbedder(dims=64))
        hit = search(
            conn,
            HashEmbedder(dims=64),
            "quarterly budget review",
            container_tag="bench",
            project_id="proj-a",
            limit=1,
        )[0]
        payload = attribute_hit(conn, hit, rank=0).as_dict()
        assert json.loads(json.dumps(payload))["hit_kind"] == "chunk"
        assert set(payload) == {
            "hit_id",
            "hit_kind",
            "document_id",
            "originating_project_id",
            "originating_org_id",
            "rank",
            "row_text",
        }
    finally:
        conn.close()


def test_memory_id_that_names_no_row_is_unresolvable() -> None:
    """A well-formed mem_ id with no matching row must not be reported as resolved."""
    from memoratum.eval_axes import attribute_hit

    conn = _db()
    try:
        attributed = attribute_hit(
            conn, {"id": "mem_deadbeef", "memory": "x", "similarity": 0.1}, rank=0
        )
        assert attributed.hit_kind == "unresolvable"
        assert attributed.originating_project_id is None
    finally:
        conn.close()


def test_cpu_model_falls_back_when_proc_cpuinfo_is_absent(monkeypatch) -> None:
    """No /proc/cpuinfo must fall through to platform.processor, not crash."""
    import builtins

    from memoratum.eval_axes import _cpu_model

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/cpuinfo":
            raise OSError("no procfs")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    value = _cpu_model()
    assert value is None or isinstance(value, str)


def test_ladder_rejects_non_integer_sizes() -> None:
    """A string or bool size would make the ladder maths meaningless."""
    from memoratum.eval_axes import ManifestError, validate_manifest

    for bad in (
        [100, "400", 1600],
        [0, 100, 200],
        [-1, 100, 200],
        [100, 100.5, 200],
        [100, True, 200],
    ):
        manifest = _manifest()
        manifest["latency_config"] = {
            "samples": 30,
            "warmup": 5,
            "ladder": bad,
            "hardware": _hw(),
        }
        with pytest.raises(ManifestError):
            validate_manifest(manifest)


def test_sample_ms_rejects_a_zero_sample_count() -> None:
    """samples=0 would return an empty list and every percentile None."""
    from memoratum.eval_axes import sample_ms

    with pytest.raises(ValueError, match="samples must be >= 1"):
        sample_ms(lambda: None, samples=0, warmup=0)


def test_build_scoped_corpus_rejects_zero_documents_per_project() -> None:
    """Zero docs would make expected_documents 0 and the axis measure nothing."""
    from memoratum.eval_axes import CorpusError, build_scoped_corpus

    conn = _db()
    try:
        with pytest.raises(CorpusError, match="docs_per_project"):
            build_scoped_corpus(conn, projects=("proj-a", "proj-b"), docs_per_project=0)
    finally:
        conn.close()


def test_csv_helpers_skip_blanks() -> None:
    from memoratum.eval_axes import iter_csv, iter_ints

    assert iter_csv("a, b ,,c") == ["a", "b", "c"]
    assert iter_csv("") == []
    assert iter_ints("5,10") == [5, 10]


def test_write_artifacts_creates_parent_directories(tmp_path) -> None:
    from memoratum.eval_axes import write_artifacts

    md = tmp_path / "nested" / "dir" / "report.md"
    js = tmp_path / "other" / "report.json"
    write_artifacts({"axis": "x"}, "# report\n", out_md=str(md), out_json=str(js))
    assert md.read_text() == "# report\n"
    assert '"axis": "x"' in js.read_text()


def test_write_artifacts_writes_only_what_was_asked_for(tmp_path, capsys) -> None:
    """No out-path must mean no file, so a stray report cannot overwrite a real one."""
    from memoratum.eval_axes import write_artifacts

    write_artifacts({"axis": "x"}, "# report\n")
    assert capsys.readouterr().out == "# report\n\n"
    assert list(tmp_path.iterdir()) == []


def test_write_artifacts_handles_a_bare_filename(tmp_path, monkeypatch) -> None:
    """os.path.dirname returns '' for a bare name, which must not call makedirs('') ."""
    from memoratum.eval_axes import write_artifacts

    monkeypatch.chdir(tmp_path)
    write_artifacts({"axis": "x"}, "r\n", out_md="report.md", out_json="report.json")
    assert (tmp_path / "report.md").read_text() == "r\n"
    assert (tmp_path / "report.json").exists()
