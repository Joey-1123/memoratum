"""Scope isolation axis contract (RED).

Isolation is the project's highest-severity invariant and, before this axis, nothing
measured it. A single unscoped corpus reports perfect recall for a scope bug, because
recall is monotonically indifferent to where data landed -- so the metric family that
existed could not observe the two P0 findings from feature 001.

Every test here is written to be falsifiable. The decisive ones are
`test_self_check_detects_an_injected_leak` and `test_a_leaked_hit_fails_the_gate`: a
metric that cannot fail must not be allowed to pass (invariant I4, SC-001).

Covers T022-T029 and T030-T036.
"""

from __future__ import annotations

import os
import tempfile

import pytest

_TEMP_ROOTS: list[tempfile.TemporaryDirectory] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_temp_roots():
    """Remove every temp dir these tests create.

    Without this, each ``mkdtemp`` leaks for the life of the process. Across the axis
    suites that reached ~5,000 directories and filled ``/tmp``, which made unrelated
    tests fail with I/O errors and truncated a source file mid-write.
    """
    yield
    while _TEMP_ROOTS:
        _TEMP_ROOTS.pop().cleanup()


def _tempdir(prefix: str) -> str:
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    _TEMP_ROOTS.append(holder)
    return holder.name


def _db():
    from memoratum import db

    return db.connect(os.path.join(_tempdir("memoratum-iso-"), "iso.db"))


EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_SELF_CHECK_FAILED = 2
EXIT_BAD_INPUT = 3


def _corpus(conn, projects=("proj-a", "proj-b", "proj-c"), docs_per_project=4):
    from memoratum.eval_axes import build_scoped_corpus

    return build_scoped_corpus(conn, projects=projects, docs_per_project=docs_per_project)


def _query(project: str, limit: int = 10):
    """Return a callable suitable for evaluate_isolation's query runner.

    Resolves ``memoratum.search.search`` at CALL time, not import time. That matters for
    the scope-blind tests below: they patch the attribute on the module, and a closure
    that captured the original function would silently bypass the patch -- which is
    exactly how a test can appear to prove something while testing nothing.
    """
    from memoratum import search as search_module

    def run(conn):
        from memoratum.embeddings import HashEmbedder

        return search_module.search(
            conn,
            HashEmbedder(dims=64),
            "quarterly budget review",
            container_tag="bench",
            project_id=project,
            limit=limit,
        )

    return run


ALL_PROJECTS = ("proj-a", "proj-b", "proj-c")


def _evaluate(
    conn,
    *,
    scoped=ALL_PROJECTS,
    unscoped_queries=0,
    limit=10,
    k=(5,),
    docs_per_project=4,
    include_attribution=False,
):
    """Run the axis over one scoped query per project plus ``unscoped_queries`` unscoped probes.

    The corpus always spans all three projects even when only one is queried, so a
    single-project query is still a real test: the other two projects hold lexically
    overlapping content that must be excluded.
    """
    from memoratum.eval_isolation import evaluate_isolation

    queries = [(_query(project, limit=limit), project) for project in scoped]
    queries += [(_query(None, limit=limit), None) for _ in range(unscoped_queries)]
    return evaluate_isolation(
        conn,
        queries=queries,
        projects=ALL_PROJECTS,
        container_tag="bench",
        docs_per_project=docs_per_project,
        ks=list(k),
        seed=42,
        include_attribution=include_attribution,
    )


# --- T022: the clean case --------------------------------------------------------


def test_two_projects_report_zero_leaks() -> None:
    """T022: a correctly scoped query returns hits from its own project only."""
    conn = _db()
    try:
        result = _evaluate(conn, scoped=("proj-a", "proj-b"), unscoped_queries=0)
        isolation = result["isolation"]
        assert isolation["scoped_queries"] > 0
        assert isolation["leaked_hits"] == 0
        assert isolation["leaked_queries"] == 0
        assert isolation["leak_rate"] == 0.0
        assert result["gate"]["status"] == "pass"
    finally:
        conn.close()


def test_scoped_hits_all_originate_from_the_requested_project() -> None:
    conn = _db()
    try:
        result = _evaluate(conn, scoped=("proj-a",), unscoped_queries=0)
        for row in result["isolation"]["per_query"]:
            assert row["scope"] == "proj-a"
            assert row["leaked_hits"] == 0
            assert row["returned_hits"] > 0
    finally:
        conn.close()


def test_higher_ranked_other_project_content_is_still_excluded_and_counted() -> None:
    """T023: project B ranks highest lexically, yet is excluded and the exclusion counted.

    This is the scenario the spec calls out. If the corpus did not overlap lexically,
    exclusion would prove nothing -- so the corpus bodies deliberately share query terms
    and only the project name differs.
    """
    conn = _db()
    try:
        result = _evaluate(conn, scoped=("proj-c",), unscoped_queries=0, include_attribution=True)
        isolation = result["isolation"]
        assert isolation["returned_hits"] > 0, "the query must match something"
        assert isolation["leaked_hits"] == 0
        # The returned hits must actually be project-c's, not an empty result.
        origins = {a["originating_project_id"] for a in isolation["_attributed"]}
        assert origins == {"proj-c"}, (
            f"a proj-c query returned {sorted(origins)}; exclusion must hold even though "
            "the other projects' bodies match the same query terms"
        )
    finally:
        conn.close()


# --- T024: unscoped queries are bucketed separately -----------------------------


def test_unscoped_queries_are_reported_separately() -> None:
    """T024/I2: project_id=None means 'no scope requested', so it is not a leak."""
    conn = _db()
    try:
        result = _evaluate(conn, scoped=("proj-a", "proj-b"), unscoped_queries=1)
        isolation = result["isolation"]
        assert isolation["unscoped_queries"] == 1
        assert isolation["unscoped_cross_scope_hits"] > 0, (
            "an unscoped query must be able to return cross-project hits"
        )
        # Neither bucket may contaminate the other.
        assert isolation["leaked_hits"] == 0
        assert "proj-b" in isolation["_unscoped_origins"] or len(isolation["_unscoped_origins"]) > 1
    finally:
        conn.close()


def test_unscoped_queries_are_excluded_from_the_gate_denominator() -> None:
    """I2: a leak rate diluted by unscoped queries hides the failure it exists to catch."""
    conn = _db()
    try:
        result = _evaluate(conn, scoped=("proj-a",), unscoped_queries=3)
        isolation = result["isolation"]
        assert isolation["scoped_queries"] == 1
        assert isolation["unscoped_queries"] == 3
        # leak_rate is computed over SCOPED returned hits only.
        assert isolation["leak_rate"] == isolation["leaked_hits"] / isolation["returned_hits"]
    finally:
        conn.close()


def test_unscoped_only_run_reports_no_leaks_and_no_gate_confusion() -> None:
    """With no scoped queries there is nothing to leak, and the gate must say so."""
    conn = _db()
    try:
        from memoratum.eval_isolation import evaluate_isolation

        result = evaluate_isolation(
            conn,
            queries=[(_query(None), None)],
            projects=("proj-a", "proj-b"),
            container_tag="bench",
            docs_per_project=4,
            ks=[5],
            seed=42,
        )
        isolation = result["isolation"]
        assert isolation["scoped_queries"] == 0
        assert isolation["leaked_hits"] == 0
        # Zero scoped queries means the axis measured nothing, which must not pass.
        assert result["gate"]["status"] == "fail"
    finally:
        conn.close()


# --- T025: reporting shape -------------------------------------------------------


def test_returned_hits_is_always_emitted_next_to_leak_rate() -> None:
    """I3: 0.5% over 2 hits and 0.5% over 200 hits are different facts."""
    conn = _db()
    try:
        result = _evaluate(conn, scoped=("proj-a", "proj-b"), unscoped_queries=0)
        for row in result["isolation"]["per_query"]:
            assert "returned_hits" in row
            assert "leak_rate" in row
            assert row["returned_hits"] > 0
            if row["leaked_hits"]:
                assert row["leak_rate"] == pytest.approx(row["leaked_hits"] / row["returned_hits"])
            else:
                assert row["leak_rate"] == 0.0
    finally:
        conn.close()


def test_leak_count_and_leak_records_never_disagree() -> None:
    """The gate count and the operator-facing records must be the same set.

    Two copies of the leak rule could drift, so an operator debugging a real leak would
    be shown a different set of hits than the one that failed the gate.
    """
    conn = _db()
    try:
        result = _leaked_result(conn)
        iso = result["isolation"]
        listed = len(iso["leaks_detail"])
        summed = sum(row["leak_records"] for row in iso["per_query"])
        assert summed == iso["leaked_hits"], (
            f"leaked_hits={iso['leaked_hits']} but per-query records sum to {summed}"
        )
        assert listed == iso["leaked_hits"], (
            f"leaked_hits={iso['leaked_hits']} but {listed} records were listed"
        )
        ranks = [leak["rank"] for leak in iso["leaks_detail"]]
        assert len(set(ranks)) == len(ranks), "ranks must identify distinct hits"
    finally:
        conn.close()


def test_leak_rate_is_a_trend_and_never_the_gate() -> None:
    """I3: the gate is the absolute count, so a tiny non-zero rate cannot pass either."""
    conn = _db()
    try:
        result = _evaluate(conn, scoped=("proj-a",), unscoped_queries=0)
        gate_names = {check["name"] for check in result["gate"]["checks"]}
        assert "leaked_hits" in gate_names
        assert "leak_rate" not in gate_names
    finally:
        conn.close()


# --- T026/T028: leaks fail the gate and are attributed ----------------------------


def _leaked_result(conn, *, project="proj-a", foreign="proj-b"):
    """Force a leak by running an unscoped query while claiming a scope.

    This is the honest way to exercise the detector: the store's own filtering is not
    broken, but the METRIC must still catch a cross-scope hit in a scoped result. If the
    metric cannot see this, it cannot see a real leak either.
    """
    from memoratum.eval_isolation import evaluate_isolation

    def leaky(conn):
        from memoratum.embeddings import HashEmbedder
        from memoratum.search import search

        # Unscoped search, but attributed to `project` -- a genuine scope violation
        # from the caller's point of view.
        return search(
            conn,
            HashEmbedder(dims=64),
            "quarterly budget review",
            container_tag="bench",
            limit=20,
        )

    return evaluate_isolation(
        conn,
        queries=[(leaky, project)],
        projects=(project, foreign, "proj-c"),
        container_tag="bench",
        docs_per_project=4,
        ks=[5],
        seed=42,
    )


def test_a_leaked_hit_fails_the_gate() -> None:
    """T026/FR-002/I1: a non-zero leak count MUST fail the run."""
    conn = _db()
    try:
        result = _leaked_result(conn)
        isolation = result["isolation"]
        assert isolation["leaked_hits"] > 0, "the forced leak must be detected"
        assert isolation["leaked_queries"] == 1
        assert result["gate"]["status"] == "fail"
        failed = [c for c in result["gate"]["checks"] if c["status"] == "fail"]
        assert any(c["name"] == "leaked_hits" for c in failed)
    finally:
        conn.close()


def test_each_leak_is_attributed_with_hit_id_project_and_rank() -> None:
    """T028/SC-004: an operator must be able to see WHICH hit leaked and WHERE."""
    conn = _db()
    try:
        result = _leaked_result(conn)
        leaks = result["isolation"]["leaks_detail"]
        assert leaks, "leaks must be attributed, not merely counted"
        foreign = {"proj-b", "proj-c"}
        assert any(leak["originating_project_id"] in foreign for leak in leaks), (
            "at least one leak must come from another project, not from proj-a itself"
        )
        for leak in leaks:
            assert leak["hit_id"].startswith("chunk_")
            assert leak["requested_project_id"] == "proj-a"
            # The corpus spans three projects, so any of the other two is a genuine leak.
            assert leak["originating_project_id"] in foreign
            assert leak["document_id"]
            assert leak["rank"] >= 0
    finally:
        conn.close()


def test_query_leak_rate_is_per_query_not_per_hit() -> None:
    """I3: robust to varying hits-per-query, which the per-hit rate is not."""
    conn = _db()
    try:
        result = _leaked_result(conn)
        isolation = result["isolation"]
        assert isolation["query_leak_rate"] == 1.0
        assert 0.0 < isolation["leak_rate"] < 1.0
    finally:
        conn.close()


def _attribution(hit_id, hit_kind, project_id):
    from memoratum.eval_axes import Attribution

    return Attribution(
        hit_id=hit_id,
        hit_kind=hit_kind,
        document_id="doc-1",
        originating_project_id=project_id,
        originating_org_id=None,
        rank=0,
        row_text="x",
    )


def test_is_leak_treats_unresolvable_as_a_leak_regardless_of_project() -> None:
    """A1: an unresolvable hit counts as a leak even when its project field matches.

    Defence in depth, and the reason it is tested directly: today an unresolvable
    attribution always carries ``project_id=None``, so a scoped query would already
    count it via the project mismatch. If attribution ever resolves a project but not a
    kind, this rule must still refuse to clear the hit -- otherwise a hit we cannot fully
    explain would be silently marked in-scope.
    """
    from memoratum.eval_isolation import is_leak

    # Matching project, unresolved kind -> must still be a leak.
    assert is_leak(_attribution("chunk_1", "unresolvable", "proj-a"), "proj-a") is True
    # Matching project, resolved kind -> must not be a leak.
    assert is_leak(_attribution("chunk_1", "chunk", "proj-a"), "proj-a") is False
    # Mismatched project -> leak, whatever the kind.
    assert is_leak(_attribution("chunk_1", "chunk", "proj-b"), "proj-a") is True
    assert is_leak(_attribution("chunk_1", "memory", "proj-b"), "proj-a") is True


def test_unresolvable_hit_counts_as_a_failure_not_a_pass() -> None:
    """A1: a hit we cannot attribute cannot be cleared, so it must fail."""
    conn = _db()
    try:
        from memoratum.eval_isolation import evaluate_isolation

        def ghost(conn):
            return [{"id": "chunk_999999", "chunk": "ghost", "similarity": 1.0}]

        result = evaluate_isolation(
            conn,
            queries=[(ghost, "proj-a")],
            projects=("proj-a", "proj-b"),
            container_tag="bench",
            docs_per_project=4,
            ks=[5],
            seed=42,
        )
        assert result["isolation"]["unresolvable_hits"] == 1
        assert result["gate"]["status"] == "fail"
    finally:
        conn.close()


# --- T027: the self-check must detect its own injected leak ----------------------


def test_self_check_detects_an_injected_leak() -> None:
    """T027/SC-001/I4: the falsifiability self-check.

    Injecting a known leak and DETECTING it is the only evidence that a clean run means
    anything. Exit 0 here means 'detection succeeded'.
    """
    conn = _db()
    try:
        from memoratum.eval_isolation import run_self_check

        outcome = run_self_check(conn, projects=("proj-a", "proj-b"), docs_per_project=3)
        assert outcome.detected is True, "the self-check must detect its own injection"
        assert outcome.injected_leaks > 0
        assert outcome.exit_code == 0, "detection succeeding is the pass condition"
    finally:
        conn.close()


def test_self_check_reports_failure_when_detection_is_disabled() -> None:
    """If the detector were neutered the self-check must FAIL, not pass.

    This is the negative control: without it, a self-check that silently stops
    detecting would still report success.
    """
    conn = _db()
    try:
        from memoratum.eval_isolation import run_self_check

        outcome = run_self_check(
            conn,
            projects=("proj-a", "proj-b"),
            docs_per_project=3,
            detector=lambda hits, requested: 0,
        )
        assert outcome.detected is False
        assert outcome.exit_code == 2, "a broken metric exits 2, which is worse than a leak"
    finally:
        conn.close()


def test_the_axis_detects_a_genuinely_broken_scope_filter(monkeypatch) -> None:
    """End-to-end: reproduce the feature-001 defect and prove the axis FAILS.

    This is the strongest evidence the axis works, and it is not a mock. The real
    ``search`` is wrapped so it ignores ``project_id`` -- exactly the defect that let
    ``Mem0AddIn`` write into the NULL scope. Every layer is real code: the corpus, the
    retrieval, the attribution, the detector and the gate.

    A unit test with an injected leak only proves the DETECTOR works. This proves the
    whole chain notices a store that has genuinely stopped enforcing scope.
    """
    import memoratum.eval_isolation as isolation_module
    import memoratum.search as search_module

    conn = _db()
    real_search = search_module.search

    def scope_blind_search(
        conn, embedder, query, *, container_tag, project_id=None, limit=10, **kw
    ):
        # The spec-001 defect, faithfully: the scope argument is accepted and ignored.
        return real_search(
            conn, embedder, query, container_tag=container_tag, project_id=None, limit=limit, **kw
        )

    # Patch BOTH bindings: default_query closes over the module-global, and the test's
    # own _query resolves through memoratum.search at call time.
    monkeypatch.setattr(search_module, "search", scope_blind_search)
    monkeypatch.setattr(isolation_module, "search", scope_blind_search)
    try:
        result = isolation_module.evaluate_isolation(
            conn,
            queries=[(_query("proj-a"), "proj-a")],
            projects=ALL_PROJECTS,
            container_tag="bench",
            docs_per_project=4,
            ks=[5],
            seed=42,
        )
    finally:
        monkeypatch.undo()

    iso = result["isolation"]
    assert iso["leaked_hits"] > 0, "a scope-blind store must produce leaks"
    assert iso["leaked_queries"] == 1
    assert 0.0 < iso["leak_rate"] < 1.0
    assert result["gate"]["status"] == "fail", "SC-001: a real leak MUST fail the run"

    # Every leak must name a genuinely foreign project, not the requested one.
    for leak in iso["leaks_detail"]:
        assert leak["originating_project_id"] in {"proj-b", "proj-c"}
        assert leak["requested_project_id"] == "proj-a"


def test_self_check_catches_false_positives_when_the_store_leaks_everything(monkeypatch) -> None:
    """The other half of I4: a detector that fails everything is also broken.

    A self-check that only tests for missed leaks would pass here, and CI would go red on
    every run for an unrelated reason -- teaching operators to ignore it.
    """
    import memoratum.eval_isolation as isolation_module
    import memoratum.search as search_module

    real_search = search_module.search

    def scope_blind_search(
        conn, embedder, query, *, container_tag, project_id=None, limit=10, **kw
    ):
        return real_search(
            conn, embedder, query, container_tag=container_tag, project_id=None, limit=limit, **kw
        )

    conn = _db()
    monkeypatch.setattr(search_module, "search", scope_blind_search)
    monkeypatch.setattr(isolation_module, "search", scope_blind_search)
    try:
        outcome = isolation_module.run_self_check(
            conn, projects=ALL_PROJECTS, docs_per_project=4, seed=42
        )
    finally:
        monkeypatch.undo()

    assert outcome.detected is True, "the injected leak IS detected"
    assert outcome.clean_leaked_hits > 0, "a scope-blind store leaks on a clean query too"
    assert outcome.exit_code == EXIT_SELF_CHECK_FAILED, (
        "false positives must exit 2: the detector cannot distinguish a real leak"
    )
    assert "false positive" in outcome.detail


def _run_cli(argv: list[str]):
    """Invoke the CLI in-process and capture its report and exit code."""
    import io
    from contextlib import redirect_stdout

    from memoratum import eval_isolation

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = eval_isolation.main(argv)
    return code, buffer.getvalue()


def test_cli_reports_a_clean_run_and_exits_zero() -> None:
    """The documented invocation must work and pass (contract §1.1)."""
    code, report = _run_cli(
        [
            "--projects",
            "3",
            "--sessions-per-project",
            "6",
            "--queries",
            "6",
            "--unscoped-queries",
            "2",
            "--k",
            "5,10",
            "--require-clean",
        ]
    )
    assert code == EXIT_OK
    assert "gate: PASS" in report
    assert "leaked_hits: 0" in report
    # Unscoped cross-scope hits must be visibly non-zero and marked informational.
    assert "unscoped_cross_scope_hits:" in report
    assert "never gates" in report
    assert "Falsifiability self-check" in report


def test_cli_rejects_a_single_project() -> None:
    """One project cannot leak, so the CLI must refuse rather than report a clean axis."""
    code, _ = _run_cli(["--projects", "1"])
    assert code == EXIT_BAD_INPUT


def test_cli_rejects_degenerate_sizes() -> None:
    """Zero projects, documents or queries must be refused, not measured as clean."""
    for argv in (
        ["--sessions-per-project", "0"],
        ["--queries", "0"],
        ["--projects", "0"],
        ["--projects", "-1"],
    ):
        code, _ = _run_cli(argv)
        assert code == EXIT_BAD_INPUT, argv


def test_cli_writes_both_artifacts(tmp_path) -> None:
    import json

    md = tmp_path / "out" / "RESULTS-isolation.md"
    js = tmp_path / "out" / "isolation.json"
    code, _ = _run_cli(
        [
            "--projects",
            "3",
            "--sessions-per-project",
            "5",
            "--queries",
            "3",
            "--unscoped-queries",
            "1",
            "--k",
            "5",
            "--out-md",
            str(md),
            "--out-json",
            str(js),
        ]
    )
    assert code == EXIT_OK
    assert md.exists() and js.exists()
    payload = json.loads(js.read_text())
    assert payload["axis"] == "isolation"
    assert payload["schema"] == "memoratum-eval-axes-v1"
    assert payload["manifest"]["schema"] == "longmemeval-scoped-v2"
    assert payload["gate"]["status"] == "pass"


def test_cli_records_the_self_check_outcome_in_json(tmp_path) -> None:
    import json

    js = tmp_path / "isolation.json"
    code, _ = _run_cli(
        [
            "--projects",
            "3",
            "--sessions-per-project",
            "5",
            "--queries",
            "3",
            "--unscoped-queries",
            "0",
            "--k",
            "5",
            "--require-clean",
            "--out-json",
            str(js),
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(js.read_text())
    assert payload["self_check"]["detected"] is True
    assert payload["self_check"]["injected_leaks"] > 0
    assert payload["self_check"]["clean_leaked_hits"] == 0


def test_cli_exits_one_when_the_gate_fails(tmp_path, monkeypatch) -> None:
    """A failing gate MUST produce a non-zero exit, or CI cannot gate on it.

    Driven through the CLI's own code path: a real leak is injected by making the store
    scope-blind, then the documented command must report FAIL and exit 1.
    """
    import memoratum.eval_isolation as isolation_module
    import memoratum.search as search_module

    real_search = search_module.search

    def scope_blind(conn, embedder, query, *, container_tag, project_id=None, limit=10, **kw):
        return real_search(
            conn, embedder, query, container_tag=container_tag, project_id=None, limit=limit, **kw
        )

    monkeypatch.setattr(search_module, "search", scope_blind)
    monkeypatch.setattr(isolation_module, "search", scope_blind)
    js = tmp_path / "isolation.json"
    code, report = _run_cli(
        [
            "--projects",
            "3",
            "--sessions-per-project",
            "6",
            "--queries",
            "6",
            "--unscoped-queries",
            "0",
            "--k",
            "5",
            "--out-json",
            str(js),
        ]
    )
    assert code == EXIT_GATE_FAILED, "FR-002: a non-zero leak count MUST fail the run"
    assert "gate: FAIL" in report


def test_cli_exits_two_when_the_self_check_cannot_detect(tmp_path, monkeypatch) -> None:
    """Exit 2 means the METRIC is broken, which is worse than a leak and must differ.

    Uses the self-check's own detector seam: a detector that never fires must exit 2 even
    though the measured axis looks perfectly clean.
    """
    import memoratum.eval_isolation as isolation_module

    monkeypatch.setattr(isolation_module, "default_detector", lambda attributions, scope: 0)
    code, report = _run_cli(
        [
            "--projects",
            "3",
            "--sessions-per-project",
            "4",
            "--queries",
            "3",
            "--unscoped-queries",
            "0",
            "--k",
            "5",
            "--require-clean",
        ]
    )
    assert code == EXIT_SELF_CHECK_FAILED
    assert "detected injected leak: False" in report


def test_summary_names_the_regressed_leaks_and_keeps_manifest_visible() -> None:
    conn = _db()
    try:
        result = _leaked_result(conn)
        from memoratum.eval_isolation import summarize

        report = summarize(result)
        assert "gate: FAIL" in report
        assert "## Leaks" in report
        # Attribution must name the origin and the requested scope.
        assert "proj-a" in report
        # The manifest stays visible so the figure is reproducible (FR-007).
        assert "longmemeval-scoped-v2" in report
        assert "expected_documents" in report
    finally:
        conn.close()


def test_summary_truncates_a_large_leak_list() -> None:
    """A 500-leak report must stay readable rather than dumping every row."""
    conn = _db()
    try:
        result = _leaked_result(conn)
        iso = result["isolation"]
        iso["leaks_detail"] = [
            {
                "hit_id": f"chunk_{i}",
                "hit_kind": "chunk",
                "document_id": f"doc_{i}",
                "originating_project_id": "proj-b",
                "requested_project_id": "proj-a",
                "rank": i,
            }
            for i in range(45)
        ]
        from memoratum.eval_isolation import summarize

        report = summarize(result)
        assert "and 25 more" in report
        assert len([line for line in report.splitlines() if line.startswith("- rank ")]) == 20
    finally:
        conn.close()


def test_a_clean_run_passes_but_the_self_check_still_runs() -> None:
    """Both halves matter: no false positives, and detection still proven."""
    conn = _db()
    try:
        result = _evaluate(conn, scoped=("proj-a", "proj-b"), unscoped_queries=1)
        assert result["isolation"]["leaked_hits"] == 0
        assert result["gate"]["status"] == "pass"
    finally:
        conn.close()


# --- T029: manifest --------------------------------------------------------------


def test_manifest_records_scope_config() -> None:
    """T029: ≥2 projects and the expected document count must be recorded."""
    conn = _db()
    try:
        result = _evaluate(conn, scoped=("proj-a", "proj-b"), unscoped_queries=0)
        scope = result["manifest"]["scope_config"]
        assert len(scope["projects"]) >= 2
        assert scope["container_tag"] == "bench"
        # 3 projects x 4 documents: the corpus spans every project even when only two
        # are queried, so the recorded shape is the corpus, not the query set.
        assert scope["projects"] == list(ALL_PROJECTS)
        assert scope["expected_documents"] == 12
        assert result["manifest"]["schema"] == "longmemeval-scoped-v2"
    finally:
        conn.close()


def test_manifest_validates() -> None:
    conn = _db()
    try:
        from memoratum.eval_axes import validate_manifest

        validate_manifest(
            _evaluate(conn, scoped=("proj-a", "proj-b"), unscoped_queries=0)["manifest"]
        )
    finally:
        conn.close()


# --- scope ----------------------------------------------------------------------


def test_isolation_requires_at_least_two_projects() -> None:
    """One project cannot leak, so the axis would be unfalsifiable."""
    conn = _db()
    try:
        from memoratum.eval_isolation import evaluate_isolation

        with pytest.raises(Exception, match="at least 2 projects"):
            evaluate_isolation(
                conn,
                queries=[(_query("proj-a"), "proj-a")],
                projects=("proj-a",),
                container_tag="bench",
                docs_per_project=2,
                ks=[5],
                seed=42,
            )
    finally:
        conn.close()


def test_empty_query_list_is_reported_not_silently_passed() -> None:
    """A run that measured nothing must not report a clean axis."""
    conn = _db()
    try:
        from memoratum.eval_isolation import evaluate_isolation

        result = evaluate_isolation(
            conn,
            queries=[],
            projects=("proj-a", "proj-b"),
            container_tag="bench",
            docs_per_project=2,
            ks=[5],
            seed=42,
        )
        assert result["isolation"]["scoped_queries"] == 0
        assert result["gate"]["status"] == "fail"
    finally:
        conn.close()


def test_leaked_hits_zero_is_exact_not_a_tolerance() -> None:
    """I1: the correct value is exactly zero, so there is no tolerance to tune."""
    conn = _db()
    try:
        result = _leaked_result(conn)
        assert result["isolation"]["leaked_hits"] >= 1
        bound = next(c["bound"] for c in result["gate"]["checks"] if c["name"] == "leaked_hits")
        assert bound == 0, "the gate bound must be zero, not a small positive number"
    finally:
        conn.close()
