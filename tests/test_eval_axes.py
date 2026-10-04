"""Shared eval-axis plumbing contract (RED).

These tests are the falsifiability floor for every axis. An axis that cannot fail
is an axis that proves nothing, so each behaviour here has a test that genuinely
fails when the behaviour is removed.

Covers T004-T006 (attribution), T008 (manifest), T010 (hardware required),
T012 (sampling), T014 (null fails), T016 (baseline), T020 (corpus count),
T021 (byte-identical text cannot attribute scope).
"""

from __future__ import annotations

import os
import tempfile

import pytest


def _db():
    """Fresh migrated temp database. ``db.connect`` already sets ``sqlite3.Row``."""
    from memoratum import db

    directory = tempfile.mkdtemp(prefix="memoratum-axes-")
    return db.connect(os.path.join(directory, "axes.db"))


def _seeded_corpus(conn, *, projects: tuple[str, ...] = ("proj-a", "proj-b")) -> None:
    """Two projects whose content overlaps lexically on purpose."""
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_all

    for project in projects:
        for n in range(3):
            db.create_document(
                conn,
                container_tag="bench",
                content=(
                    f"## Notes {n}\n\n"
                    f"The quarterly budget review for {project} is scheduled for tuesday. "
                    f"Line item {n} covers travel and lodging."
                ),
                custom_id=f"doc-{n}",
                project_id=project,
            )
    process_all(conn, HashEmbedder(dims=64))


def _search(conn, *, project_id: str | None, limit: int = 10) -> list[dict]:
    from memoratum.embeddings import HashEmbedder
    from memoratum.search import search

    return search(
        conn,
        HashEmbedder(dims=64),
        "quarterly budget review",
        container_tag="bench",
        project_id=project_id,
        limit=limit,
    )


# --- T004/T005/T006/T021: attribution -----------------------------------------


def test_attribute_hit_resolves_chunk_to_owning_project() -> None:
    """T004: a chunk hit resolves through chunks -> documents to its project."""
    from memoratum.eval_axes import attribute_hit

    conn = _db()
    try:
        _seeded_corpus(conn)
        hits = _search(conn, project_id="proj-a")
        assert hits, "expected at least one hit"

        attributed = attribute_hit(conn, hits[0], rank=0)
        assert attributed.hit_id == hits[0]["id"]
        assert attributed.hit_kind == "chunk"
        assert attributed.originating_project_id == "proj-a"
        assert attributed.rank == 0
        assert attributed.document_id is not None
    finally:
        conn.close()


def test_attribute_hit_resolves_memory_and_fact_rows() -> None:
    """T005: memory hits use memories.text; fact hits reconstruct 's p o'."""
    from memoratum.eval_axes import attribute_hit

    conn = _db()
    try:
        row = conn.execute(
            "INSERT INTO memories(id, container_tag, text, metadata, created_at, updated_at)"
            " VALUES ('mem_1','bench','Alice lives in Lisbon','{}',0,0) RETURNING id"
        ).fetchone()
        memory_hit = {"id": row["id"], "chunk": "Alice lives in Lisbon", "similarity": 1.0}
        attributed = attribute_hit(conn, memory_hit, rank=0)
        assert attributed.hit_kind == "memory"
        assert attributed.row_text == "Alice lives in Lisbon"

        conn.execute(
            "INSERT INTO facts(id, container_tag, subject, predicate, object, valid_from, created_at)"
            " VALUES ('fact_1','bench','Alice','lives in','Lisbon',0,0)"
        )
        fact_hit = {"id": "fact_1", "chunk": "Alice lives in Lisbon", "similarity": 1.0}
        fact_attributed = attribute_hit(conn, fact_hit, rank=1)
        assert fact_attributed.hit_kind == "fact"
        assert fact_attributed.row_text == "Alice lives in Lisbon"
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
        mixed = attribute_hits(
            conn, [ghost, {"id": "also-unknown", "chunk": "x", "similarity": 0.1}]
        )
        assert len(mixed) == 2
        assert [a.hit_kind for a in mixed] == ["unresolvable", "unresolvable"]
        assert [a.rank for a in mixed] == [0, 1]
    finally:
        conn.close()


def test_byte_identical_text_cannot_attribute_scope() -> None:
    """T021: identical content in two projects stays id-distinguishable (A2)."""
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_axes import attribute_hits
    from memoratum.ingest import process_all

    conn = _db()
    try:
        body = "the quarterly budget review is scheduled for tuesday"
        for project in ("proj-a", "proj-b"):
            db.create_document(
                conn,
                container_tag="bench",
                content=body,
                custom_id="shared-id",
                project_id=project,
            )
        assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 2

        # create_document only queues the document; process_all chunks and embeds it.
        process_all(conn, HashEmbedder(dims=64))
        hits = _search(conn, project_id=None, limit=10)
        assert hits, "unscoped query must return hits from both projects"
        attributed = attribute_hits(conn, hits)
        chunk_attributions = [a for a in attributed if a.hit_kind == "chunk"]
        projects = {a.originating_project_id for a in chunk_attributions}
        assert projects == {"proj-a", "proj-b"}, (
            f"expected both projects attributed, got {projects!r}"
        )
        # Attribution came from ids, not text: two rows, two distinct chunk ids.
        assert len({a.hit_id for a in chunk_attributions}) == len(chunk_attributions)
    finally:
        conn.close()


def test_attribute_hits_preserves_rank_order() -> None:
    from memoratum.eval_axes import attribute_hits

    conn = _db()
    try:
        _seeded_corpus(conn)
        hits = _search(conn, project_id="proj-a")
        attributed = attribute_hits(conn, hits)
        assert [a.rank for a in attributed] == list(range(len(hits)))
    finally:
        conn.close()


# --- T008/T010: manifest -------------------------------------------------------


def test_manifest_preserves_original_keys_and_bumps_schema() -> None:
    """T008: the seven original keys stay; schema bumps; new blocks appear (M1)."""
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


def test_latency_manifest_rejects_undersampled_and_short_ladder() -> None:
    """L2/FR-004: n<20 is theatre, and fewer than 3 sizes cannot show growth."""
    from memoratum.eval_axes import ManifestError, build_manifest, validate_manifest

    hardware = {
        "cpu": "test",
        "cores": 4,
        "ram_mb": 1024,
        "python": "3.12",
        "platform": "linux",
    }

    def manifest_for(**latency):
        return build_manifest(
            seed=42,
            requested_n=1,
            n=1,
            ks=[5],
            modes=["hybrid"],
            embedder="HashEmbedder:64",
            vector_store="SQLiteVectorStore",
            data_sha256="x",
            latency_config=latency,
        )

    with pytest.raises(ManifestError, match="below the floor of 20"):
        validate_manifest(
            manifest_for(samples=5, warmup=5, ladder=[100, 400, 1600], hardware=hardware)
        )

    with pytest.raises(ManifestError, match="at least 3"):
        validate_manifest(manifest_for(samples=30, warmup=5, ladder=[100, 400], hardware=hardware))

    validate_manifest(
        manifest_for(samples=20, warmup=5, ladder=[100, 400, 1600], hardware=hardware)
    )


def test_cost_config_rejects_bytes_and_requires_measured_ratio() -> None:
    """C1/C2: bytes is not a reported unit; the ratio is measured, never hardcoded."""
    from memoratum.eval_axes import ManifestError, build_manifest, validate_manifest

    def manifest_for(**cost):
        return build_manifest(
            seed=42,
            requested_n=1,
            n=1,
            ks=[5],
            modes=["hybrid"],
            embedder="HashEmbedder:64",
            vector_store="SQLiteVectorStore",
            data_sha256="x",
            cost_config=cost,
        )

    with pytest.raises(ManifestError, match="bytes"):
        validate_manifest(manifest_for(unit="bytes", chars_per_ws_token=6.27))

    with pytest.raises(ManifestError, match="measured"):
        validate_manifest(manifest_for(unit="chars", chars_per_ws_token=None))

    validate_manifest(manifest_for(unit="chars", chars_per_ws_token=6.27))


def test_scope_config_requires_two_projects() -> None:
    """One project cannot leak, so the isolation axis would be unfalsifiable."""
    from memoratum.eval_axes import ManifestError, build_manifest, validate_manifest

    manifest = build_manifest(
        seed=42,
        requested_n=1,
        n=1,
        ks=[5],
        modes=["hybrid"],
        embedder="HashEmbedder:64",
        vector_store="SQLiteVectorStore",
        data_sha256="x",
        scope_config={"container_tag": "bench", "projects": ["only"], "expected_documents": 3},
    )
    with pytest.raises(ManifestError, match="at least 2 projects"):
        validate_manifest(manifest)


def test_latency_manifest_requires_hardware() -> None:
    """T010/B1: latency figures without hardware are invalid (FR-008)."""
    from memoratum.eval_axes import ManifestError, build_manifest, validate_manifest

    manifest = build_manifest(
        seed=42,
        requested_n=1,
        n=1,
        ks=[5],
        modes=["hybrid"],
        embedder="HashEmbedder:64",
        vector_store="SQLiteVectorStore",
        data_sha256="x",
        latency_config={"samples": 30, "warmup": 5, "ladder": [100, 400, 1600]},
    )
    with pytest.raises(ManifestError, match="hardware"):
        validate_manifest(manifest)


def test_latency_manifest_with_hardware_validates() -> None:
    from memoratum.eval_axes import build_manifest, validate_manifest

    manifest = build_manifest(
        seed=42,
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
            "ladder": [100, 400, 1600],
            "hardware": {
                "cpu": "test",
                "cores": 4,
                "ram_mb": 1024,
                "python": "3.12",
                "platform": "linux",
            },
        },
    )
    validate_manifest(manifest)


# --- T012: sampling -------------------------------------------------------------


def test_sample_ms_discards_warmup_iterations() -> None:
    """T012/L4: warmup is discarded so the first FTS5 call is not measured."""
    from memoratum.eval_axes import sample_ms

    calls: list[int] = []

    def fn() -> None:
        calls.append(1)

    samples = sample_ms(fn, samples=5, warmup=3)
    assert len(calls) == 8, "3 warmup + 5 sampled"
    assert len(samples) == 5
    assert all(value >= 0 for value in samples)


def test_percentiles_come_from_raw_samples() -> None:
    """L3: p95-of-p95 is not a p95 -- readers must aggregate raw samples."""
    from memoratum.eval_axes import percentile

    raw = [float(v) for v in range(1, 101)]
    assert percentile(raw, 50) == pytest.approx(50.5)
    assert percentile(raw, 95) == pytest.approx(95.05)
    assert percentile([5.0], 95) == 5.0


# --- T014: null fails the gate --------------------------------------------------


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
    check = gate.checks[0]
    assert check["phase"] == "index"
    assert check["delta"] == pytest.approx(0.7)


# --- T016: baselines ------------------------------------------------------------


def test_latency_baseline_without_hardware_is_rejected() -> None:
    """T016/B1: an invalid baseline yields null, which fails (never passes)."""
    from memoratum.eval_axes import load_baseline

    baseline = load_baseline(
        {"kind": "latency", "axis": "retrieve", "value": 3.5, "tolerance": 0.4, "manifest_ref": "x"}
    )
    assert baseline is None, "a latency baseline with no hardware is invalid"


def test_missing_baseline_is_null_and_fails() -> None:
    """T016/B3: no baseline is null, and null fails the gate."""
    from memoratum.eval_axes import Gate, load_baseline

    assert load_baseline(None) is None
    gate = Gate()
    gate.check("cost", value=10.0, bound=None, comparison=">")
    assert gate.status == "fail", "a missing baseline must never silently pass"


def test_compare_to_baseline_reports_delta() -> None:
    from memoratum.eval_axes import Gate, load_baseline

    baseline = load_baseline(
        {
            "kind": "cost",
            "axis": "retrieved_chars_mean",
            "value": 300.0,
            "tolerance": 0.1,
            "manifest_ref": "x",
        }
    )
    assert baseline is not None and baseline["value"] == 300.0

    gate = Gate()
    gate.check(
        "retrieved_chars_mean",
        value=345.0,
        bound=baseline["value"] * (1 + baseline["tolerance"]),
        comparison=">",
        delta=345.0 - baseline["value"],
    )
    assert gate.status == "fail"
    assert gate.checks[0]["delta"] == pytest.approx(45.0)


# --- T020: corpus construction --------------------------------------------------


def test_build_scoped_corpus_asserts_per_project_counts() -> None:
    """T020/S3: expected document counts are asserted, never inferred from leaks."""
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
        # A missing document must be detectable, not hidden by a zero leak count.
        with pytest.raises(AssertionError):
            build_scoped_corpus(
                conn, projects=("proj-a", "proj-b", "proj-c"), docs_per_project=4, ingest=False
            )
    finally:
        conn.close()


def test_scoped_corpus_reuses_custom_ids_across_projects() -> None:
    """S2: reusing custom_id across projects must not collide into one row."""
    from memoratum.eval_axes import build_scoped_corpus

    conn = _db()
    try:
        corpus = build_scoped_corpus(conn, projects=("proj-a", "proj-b"), docs_per_project=2)
        rows = conn.execute(
            "SELECT custom_id, COUNT(*) c FROM documents GROUP BY custom_id"
        ).fetchall()
        assert all(row["c"] == 2 for row in rows), "each custom_id appears once per project"
        assert corpus.expected_documents == 4
    finally:
        conn.close()
