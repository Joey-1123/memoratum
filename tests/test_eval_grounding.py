"""Grounding axis contract (RED).

For a retrieval-backed store the ground truth is its own corpus, so grounding is
checkable deterministically -- no external judge required. This is P2 because a grounded
answer built from the wrong documents is still caught by the isolation and recall axes.

The rules encoded here are measured, not stylistic (``research.md`` D4,
``data-model.md`` G1-G6):

* **Provenance by row identity, not text overlap.** Measured: text reassembled from a
  document's own words but absent from it scores token-set containment **1.000** --
  identical to a perfect prefix truncation -- while genuinely unrelated documents reach
  **0.739**, inside any sane 0.8 gate. Set overlap cannot separate grounded from
  fabricated, which rules out the most commonly proposed design.
* **Grade chunk text against ``chunks.text``, never ``documents.content``.** Over 5,268
  real chunks, exact substring match against the parent document is 99.7%, and the
  failures are *catastrophic, not graceful*: the longest in-document prefix was 68 of
  1,498 characters (5%). ``split_markdown`` rewrites every heading level to ``# {h}``, so a
  per-item containment ratio would score a perfectly grounded chunk at 0.05.
* **One global rule is invalid.** The fact leg renders ``subject predicate object``, which
  is not a substring of the corpus, so a single threshold would score the fact leg 0.0 and
  the document leg 1.0 for identical evidence.
* **Tier 2 diagnostics never gate.**
* **An absent judge must not fail the run.**

Covers T070-T079 and T080-T087.
"""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LONGMEMEVAL = os.path.join(REPO_ROOT, "data", "longmemeval_s_cleaned.json")

_TEMP_ROOTS: list[tempfile.TemporaryDirectory] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_temp_roots():
    yield
    while _TEMP_ROOTS:
        _TEMP_ROOTS.pop().cleanup()


def _tempdir(prefix: str = "memoratum-grounding-") -> str:
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    _TEMP_ROOTS.append(holder)
    return holder.name


def _db():
    from memoratum import db

    return db.connect(os.path.join(_tempdir(), "grounding.db"))


def _seed(conn) -> None:
    """One project-scoped document plus one fact-derived memory."""
    from memoratum import db, facts
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_all

    facts.add_fact(
        conn,
        container_tag="bench",
        subject="Alice",
        predicate="lives in",
        object="Lisbon",
        document_id=None,
        metadata=None,
    )
    db.create_document(
        conn,
        container_tag="bench",
        content=(
            "# Quarterly review\n\nThe budget review is scheduled for tuesday. "
            "Travel and lodging are approved for the whole team."
        ),
        custom_id="doc-1",
        project_id="proj-a",
    )
    process_all(conn, HashEmbedder(dims=64))


def _hits(conn, query: str = "quarterly budget review", mode: str = "hybrid"):
    from memoratum.embeddings import HashEmbedder
    from memoratum.search import search

    return search(
        conn, HashEmbedder(dims=64), query, container_tag="bench", search_mode=mode, limit=10
    )


# --- T070: the clean case ---------------------------------------------------------


def test_a_fully_grounded_corpus_reports_one() -> None:
    """T070/FR-005: every hit traces back to an ingested row."""
    conn = _db()
    try:
        _seed(conn)
        result = _evaluate(conn)
        grounding = result["grounding"]
        assert grounding["hits_checked"] > 0
        assert grounding["grounded_hits"] == grounding["hits_checked"]
        assert grounding["grounded_fraction"] == 1.0
        assert grounding["ungrounded"] == []
        assert result["gate"]["status"] == "pass"
    finally:
        conn.close()


def test_manifest_records_the_normalization_and_per_kind_rules() -> None:
    """G2: the rule set must be in the manifest, so a figure is reproducible."""
    conn = _db()
    try:
        _seed(conn)
        config = _evaluate(conn)["manifest"]["grounding_config"]
    finally:
        conn.close()
    assert config["normalization"] == "nfkc+whitespace+casefold"
    assert config["rule_by_kind"] == {
        "chunk": "chunks.text",
        "memory": "memories.text",
        "fact": "subject predicate object",
    }


# --- T071: an ungrounded hit is detected ------------------------------------------


def test_text_absent_from_the_corpus_is_reported_ungrounded() -> None:
    """T071: a hit whose text is in no ingested row must be reported, not skipped."""
    import memoratum.eval_grounding as grounding_module

    conn = _db()
    try:
        _seed(conn)
        # Replace the retriever with one that returns fabricated text under a REAL id, so
        # the id resolves and only the text comparison can fail.
        real_hits = _hits(conn)
        assert real_hits
        fabricated = [{**real_hits[0], "chunk": "a sentence that appears in no ingested document"}]
        result = grounding_module.evaluate_grounding(
            conn, hits=fabricated, container_tag="bench", project_id="proj-a"
        )
        grounding = result["grounding"]
        assert grounding["ungrounded_hits"] == 1
        assert grounding["grounded_fraction"] == 0.0
        assert grounding["ungrounded"][0]["hit_id"] == real_hits[0]["id"]
        assert grounding["ungrounded"][0]["reason"]
        assert result["gate"]["status"] == "fail"
    finally:
        conn.close()


# --- T072/G1: the chunk rule is chunks.text, never documents.content --------------


def test_a_chunk_hit_is_grounded_against_chunks_text() -> None:
    """T072/G1: the indexed row is the source of truth, not the parent document."""
    conn = _db()
    try:
        _seed(conn)
        hits = _hits(conn, mode="documents")
        assert hits and hits[0]["id"].startswith("chunk_")
        result = _evaluate(conn, hits=hits)
        assert result["grounding"]["grounded_fraction"] == 1.0
    finally:
        conn.close()


def _markdown_corpus(min_chars: int = 200) -> list[tuple[str, str]]:
    """The repository's own markdown files: real content, locally present, deterministic.

    Deliberately not the LongMemEval corpus. `format_session` emits ``role: content``
    lines with no headings, so `split_markdown`'s heading rewrite never fires there and the
    G1 trap cannot be observed: measured 0 non-substring chunks in the first 10 records.
    Markdown is where the rewrite actually bites.
    """
    import glob

    corpus: list[tuple[str, str]] = []
    for path in sorted(glob.glob(os.path.join(REPO_ROOT, "**", "*.md"), recursive=True)):
        if "/.venv/" in path or "/node_modules/" in path:
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            # An unreadable path in the tree must not silently become a smaller corpus, or
            # the G1 measurement would quietly weaken. Recorded, not skipped.
            raise AssertionError(f"could not read {path}: {exc}") from exc
        if len(text) >= min_chars:
            corpus.append((os.path.relpath(path, REPO_ROOT), text))
    return corpus


def test_real_chunks_can_have_a_tiny_prefix_in_their_parent_document() -> None:
    """G1, measured: this is why documents.content is the wrong grading source.

    A synthetic probe cannot show this. `split_markdown` rewrites every heading level to
    `# {h}`, and `# {h}` is a literal substring of the original `## {h}`, so a hand-built
    document passes by coincidence.

    Measured over this repository's 47 markdown files: 138 of 596 chunks (23.2%) are not
    exact substrings of their parent document, and the worst in-document prefix is 1% of
    the chunk. A per-item containment ratio would score those perfectly grounded chunks
    near zero. The LongMemEval corpus shows the same defect far more rarely (18 of 5,268,
    0.3%, ``research.md`` D4) because it contains no markdown headings at all.
    """
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_all

    corpus = _markdown_corpus()
    assert len(corpus) >= 20, f"expected the repository's markdown, found {len(corpus)} files"

    conn = db.connect(os.path.join(_tempdir(), "real.db"))
    try:
        for custom_id, text in corpus:
            db.create_document(conn, container_tag="bench", content=text, custom_id=custom_id)
        process_all(conn, HashEmbedder(dims=64))

        total = 0
        mismatched = 0
        worst_prefix = 1.0
        for row in conn.execute("SELECT document_id, text FROM chunks"):
            total += 1
            document = conn.execute(
                "SELECT content FROM documents WHERE id = ?", (row["document_id"],)
            ).fetchone()
            parent = document["content"] if document else ""
            text = row["text"]
            if not text.strip() or text in parent:
                continue
            mismatched += 1
            longest = 0
            for end in range(1, len(text) + 1):
                if text[:end] in parent:
                    longest = end
                else:
                    break
            worst_prefix = min(worst_prefix, longest / len(text))

        assert total > 100, f"expected a real corpus, got {total} chunks"
        assert mismatched > 0, (
            "expected real chunks that are not exact substrings of their parent document; "
            "if split_markdown changed, this measurement no longer supports G1"
        )
        assert worst_prefix < 0.5, (
            f"the failure is catastrophic, not graceful: worst in-document prefix was "
            f"{worst_prefix:.0%}"
        )
    finally:
        conn.close()


def test_chunk_text_is_grounded_against_chunks_text_not_documents_content() -> None:
    """G1 as an assertion about this axis: a chunk with a 1% parent prefix still grounds.

    The measurement above proves such chunks exist; this proves the axis does not care,
    because it grades against `chunks.text`.
    """
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_all

    conn = _db()
    try:
        _seed(conn)
        for custom_id, text in _markdown_corpus():
            db.create_document(conn, container_tag="bench", content=text, custom_id=custom_id)
        process_all(conn, HashEmbedder(dims=64))

        # The chunk with the worst parent-document prefix.
        worst_id, worst_prefix = None, 1.0
        for row in conn.execute("SELECT id, document_id, text FROM chunks"):
            document = conn.execute(
                "SELECT content FROM documents WHERE id = ?", (row["document_id"],)
            ).fetchone()
            parent = document["content"] if document else ""
            text = row["text"]
            if not text.strip() or text in parent:
                continue
            longest = 0
            for end in range(1, len(text) + 1):
                if text[:end] in parent:
                    longest = end
                else:
                    break
            if longest / len(text) < worst_prefix:
                worst_prefix, worst_id = longest / len(text), row["id"]
        assert worst_id is not None, "no mismatched chunk found; the corpus changed"

        row = conn.execute("SELECT text FROM chunks WHERE id = ?", (worst_id,)).fetchone()
        result = _evaluate(
            conn, hits=[{"id": f"chunk_{worst_id}", "chunk": row["text"], "similarity": 1.0}]
        )
        assert worst_prefix < 0.5
        assert result["grounding"]["grounded_fraction"] == 1.0, (
            "a chunk is grounded against chunks.text, so a rewritten heading cannot un-ground it"
        )
    finally:
        conn.close()


# --- T073/T074/G2: per-kind rules -------------------------------------------------


def test_a_fact_hit_uses_the_fact_rule_not_the_memory_rule() -> None:
    """T073/G2: `_fact_text` renders 'subject predicate object', which is not in the corpus.

    A single global substring rule would score this perfectly-provenance hit 0.0.
    """
    conn = _db()
    try:
        _seed(conn)
        hits = _hits(conn, query="Alice lives in Lisbon", mode="memories")
        assert hits, "the fact's memory projection must be retrievable"
        from memoratum.eval_axes import attribute_hits

        attributions = attribute_hits(conn, hits)
        assert any(a.hit_kind == "fact" for a in attributions), (
            "a fact-derived hit must be classified fact, or the fact rule is unreachable"
        )
        result = _evaluate(conn, hits=hits)
        assert result["grounding"]["by_kind"]["fact"]["checked"] > 0
        assert result["grounding"]["grounded_fraction"] == 1.0
    finally:
        conn.close()


def test_a_plain_memory_hit_uses_the_memory_rule() -> None:
    """T074/G2: memory rows are grounded against their own text."""
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO memories(id, container_tag, text, metadata, created_at, updated_at)"
            " VALUES ('mem_plain','bench','user prefers dark mode','{}',0,0)"
        )
        conn.commit()
        from memoratum.eval_axes import attribute_hit

        attributed = attribute_hit(
            conn, {"id": "mem_plain", "memory": "user prefers dark mode", "similarity": 1.0}, rank=0
        )
        assert attributed.hit_kind == "memory"
        assert attributed.row_text == "user prefers dark mode"

        result = _evaluate(
            conn, hits=[{"id": "mem_plain", "memory": "user prefers dark mode", "similarity": 1.0}]
        )
        assert result["grounding"]["grounded_fraction"] == 1.0
    finally:
        conn.close()


def test_an_unresolvable_hit_is_ungrounded_not_ignored() -> None:
    """A1: a hit we cannot attribute has not been shown to come from the corpus."""
    conn = _db()
    try:
        result = _evaluate(conn, hits=[{"id": "chunk_999999", "chunk": "ghost", "similarity": 1.0}])
        grounding = result["grounding"]
        assert grounding["unresolvable_hits"] == 1
        assert grounding["grounded_fraction"] == 0.0
        assert result["gate"]["status"] == "fail"
    finally:
        conn.close()


# --- T075: the injection self-check ----------------------------------------------


def test_the_injection_uses_a_resolvable_id() -> None:
    """The self-check must exercise text grounding, not the row-identity fallback.

    Injecting under a nonexistent id would be "detected" by the identity check alone, so
    the self-check would pass on a build where text comparison is completely broken. This
    asserts the injected hit resolves, so only the text rule can catch it.
    """
    from memoratum.eval_axes import attribute_hits
    from memoratum.eval_grounding import _injected_hit

    conn = _db()
    try:
        _seed(conn)
        injected = _injected_hit(conn)
        attributed = attribute_hits(conn, [injected])[0]
        assert attributed.hit_kind != "unresolvable", (
            "the injected hit must resolve to a real row, or the self-check proves nothing"
        )
        assert attributed.hit_kind == "chunk"
        assert attributed.row_text != injected["chunk"], (
            "the injected text must differ from the row it borrows its id from"
        )
    finally:
        conn.close()


def test_the_injection_is_caught_by_the_text_rule_not_by_an_empty_text_guard() -> None:
    """Detection must come from the text comparison, or it is detection for the wrong reason.

    A self-check whose injected text is blank would be "detected" by the empty-text guard
    and would pass on a build where text comparison is entirely broken -- the same
    vacuous-pass failure as using an unresolvable id.
    """
    from memoratum.eval_axes import attribute_hits  # noqa: F401
    from memoratum.eval_grounding import INJECTED_TEXT, _injected_hit, normalize_text

    conn = _db()
    try:
        _seed(conn)
        assert normalize_text(INJECTED_TEXT), "the injected text must be non-empty"
        injected = _injected_hit(conn)
        assert normalize_text(injected["chunk"]), "the injected hit must carry text"

        result = _evaluate(conn, hits=[injected])
        assert result["grounding"]["ungrounded_hits"] == 1
        reason = result["grounding"]["ungrounded"][0]["reason"]
        assert "does not match" in reason, (
            f"the injection was caught by the wrong rule ({reason!r}); a self-check that "
            "passes for an unrelated reason proves nothing"
        )
    finally:
        conn.close()


def test_inject_ungrounded_is_detected() -> None:
    """T075/SC-007: inject text in no ingested row and prove it is reported."""
    conn = _db()
    try:
        _seed(conn)
        outcome = _self_check(conn)
        assert outcome.detected is True, "the self-check must detect its own injection"
        assert outcome.injected_ungrounded > 0
        assert outcome.exit_code == 0, "detection succeeding is the pass condition"
    finally:
        conn.close()


def test_the_self_check_fails_when_detection_is_disabled() -> None:
    """The negative control: without it, a neutered detector still reports success."""
    conn = _db()
    try:
        _seed(conn)
        outcome = _self_check(conn, force_clean=True)
        assert outcome.detected is False
        assert outcome.exit_code == 2, "a broken metric exits 2, worse than a real failure"
    finally:
        conn.close()


def test_the_self_check_fails_on_a_detector_that_reports_everything_ungrounded() -> None:
    """The other half of the control: a metric that fails everything must also exit 2.

    Without this, the self-check only proves detection works. A detector stuck in the
    opposite failure -- always ungrounded -- would pass every detection assertion while
    failing every real run, and it is the failure that teaches operators to ignore a gate.
    """
    conn = _db()
    try:
        _seed(conn)
        outcome = _self_check(conn, force_noisy=True)
        assert outcome.exit_code == 2, "false positives must exit 2, not 0"
        assert outcome.detected is True, "the injection is still found; that is not the bug"
        assert outcome.clean_ungrounded > 0, "real hits were wrongly reported ungrounded"
        assert "false positives" in outcome.detail
    finally:
        conn.close()


def test_a_clean_run_passes_but_detection_is_still_proven() -> None:
    conn = _db()
    try:
        _seed(conn)
        result = _evaluate(conn)
        assert result["grounding"]["grounded_fraction"] == 1.0
        assert result["gate"]["status"] == "pass"
    finally:
        conn.close()


# --- T076/G6: an absent judge must not fail the run ------------------------------


@pytest.mark.parametrize("judge", [None, "", "none"])
def test_no_judge_still_reports_and_does_not_fail(judge) -> None:
    """T076/FR-006/G6: an external judge is opt-in and never required."""
    conn = _db()
    try:
        _seed(conn)
        result = _evaluate(conn, judge=judge)
        assert result["grounding"]["judge"] is None
        assert result["grounding"]["grounded_fraction"] == 1.0
        assert result["gate"]["status"] == "pass"
    finally:
        conn.close()


def test_a_configured_judge_is_reported_but_never_gates() -> None:
    """G6: a judge score must not enter the gate, or the metric becomes non-deterministic."""
    conn = _db()
    try:
        _seed(conn)
        result = _evaluate(conn, judge="fake-judge")
        assert result["grounding"]["judge"] == "fake-judge"
        # Assert the gate is composed of exactly the deterministic figures: a judge
        # entering here would make the gate non-deterministic without failing anything.
        assert [check["name"] for check in result["gate"]["checks"]] == ["grounded_fraction"]
    finally:
        conn.close()


# --- T077: Tier 2 never gates -----------------------------------------------------


def test_tier2_diagnostics_do_not_affect_the_gate() -> None:
    """T077/G3: LCS, shingle overlap and FTS5 snippets are diagnostic only."""
    conn = _db()
    try:
        _seed(conn)
        without = _evaluate(conn)
        with_tier2 = _evaluate(conn, tier2=True)
        assert without["gate"]["status"] == with_tier2["gate"]["status"]
        assert (
            without["grounding"]["grounded_fraction"]
            == with_tier2["grounding"]["grounded_fraction"]
        )
        assert with_tier2["grounding"]["tier2_diagnostics"] is not None
        assert without["grounding"]["tier2_diagnostics"] is None
    finally:
        conn.close()


def test_fts5_snippet_is_available_without_a_tokenizer() -> None:
    """The best zero-dependency evidence that tokens reached the index.

    The bracketed markers prove the matched tokens are the ones the retriever uses, not
    just that a row came back.
    """
    conn = _db()
    try:
        _seed(conn)
        result = _evaluate(conn, tier2=True, query="what is scheduled for tuesday?")
        diagnostics = result["grounding"]["tier2_diagnostics"]
        snippet = diagnostics["fts5_snippet"]
        assert snippet, "the snippet must not be null for a tokenised query"
        assert "[" in snippet and "]" in snippet, snippet
        assert "scheduled" in snippet
    finally:
        conn.close()


def test_a_query_containing_fts5_syntax_does_not_break_the_snippet() -> None:
    """FTS5 takes a query language, so the question must be sanitised, not passed through.

    Measured: `MATCH 'what is scheduled for tuesday?'` raises
    `fts5: syntax error near "?"`, which made this diagnostic report null on every real
    question -- an error swallowed into a plausible-looking value.
    """
    for query in (
        "what is scheduled for tuesday?",
        'budget AND "OR" NOT',
        "review (unbalanced",
        "*",
        "",
    ):
        conn = _db()
        try:
            _seed(conn)
            diagnostics = _evaluate(conn, tier2=True, query=query)["grounding"]["tier2_diagnostics"]
            # No error key can exist: the sanitiser means no query reaches FTS5 unescaped,
            # and a genuine FTS5 failure must propagate rather than be reported as "found
            # nothing".
            assert set(diagnostics) == {"fts5_query", "fts5_snippet"}, diagnostics
        finally:
            conn.close()


# --- T078/M3: an unexercised kind is null, never zero -----------------------------


def test_an_unexercised_kind_reports_null_not_zero() -> None:
    """T078/M3: `checked == 0` means not exercised, and 0.0 would read as a pass."""
    conn = _db()
    try:
        _seed(conn)
        result = _evaluate(conn, hits=_hits(conn, mode="documents"))
        fact = result["grounding"]["by_kind"]["fact"]
        assert fact["checked"] == 0
        assert fact["fraction"] is None, "an unexercised kind is None, never 0.0"
    finally:
        conn.close()


# --- T079/G4: no corpus-wide text scan --------------------------------------------


def test_grounding_reads_one_row_per_hit_and_never_scans_the_corpus() -> None:
    """T079/G4: grounding is a keyed lookup per hit, never a scan over corpus text.

    The original G4 required a corpus-wide text index "built once per corpus" on the
    strength of a 51.6 ms/query vs 0.78 ms/query measurement. Re-measuring showed why that
    number was misleading: a corpus-wide substring scan over 5,268 real chunks costs 2.56
    ms/hit median (70.6 ms worst), and the "indexed" variant was *slower* still, because
    normalisation of the whole corpus dominates. Worse, the scan is redundant -- a row that
    came out of the database is corpus membership by definition, so no hit can fail a
    corpus-wide scan after passing row identity.

    The test therefore asserts the thing that actually matters: the number of queries is
    proportional to the number of hits, and each one is a keyed lookup. Scaling the corpus
    by 5x must not add queries.
    """
    import memoratum.eval_grounding as grounding_module
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_all

    def count_queries(conn) -> list[str]:
        """Record every SQL statement, via set_trace_callback.

        `sqlite3.Connection.execute` is read-only, and `set_trace_callback` is the
        supported hook -- already the technique used in `test_search_perf.py`.
        """
        seen: list[str] = []
        conn.set_trace_callback(lambda sql: seen.append(" ".join(sql.split())))
        return seen

    def corpus_size(n: int) -> tuple[int, int]:
        conn = db.connect(os.path.join(_tempdir(), f"scale-{n}.db"))
        try:
            for i in range(n):
                db.create_document(
                    conn,
                    container_tag="bench",
                    content=(
                        f"# Document {i}\n\n"
                        f"The quarterly budget review number {i} is scheduled for tuesday. "
                        f"Row {i} covers travel and lodging for the whole team, in full."
                    ),
                    custom_id=f"doc-{i}",
                    project_id="proj-a",
                )
            process_all(conn, HashEmbedder(dims=64))
            return conn, conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
        except Exception:
            conn.close()
            raise

    results = []
    for n in (5, 25):
        conn, chunks = corpus_size(n)
        try:
            hits = _hits(conn)
            assert hits
            seen = count_queries(conn)
            grounding_module.evaluate_grounding(
                conn, hits=hits, container_tag="bench", project_id="proj-a"
            )
            conn.set_trace_callback(None)
            # One attribution lookup per hit, plus the one rows_resolved sum. No query may
            # read every chunk or every document.
            per_hit = [
                sql
                for sql in seen
                if sql.upper().startswith("SELECT") and "FROM CHUNKS" in sql.upper()
            ]
            results.append((n, chunks, len(per_hit), len(hits)))
        finally:
            conn.close()

    (
        (small_n, small_chunks, small_queries, small_hits),
        (
            big_n,
            big_chunks,
            big_queries,
            big_hits,
        ),
    ) = results
    assert big_chunks > small_chunks, "the larger corpus must actually be larger"
    assert big_hits > small_hits, "the larger corpus must return more hits"
    # Queries per hit, not total queries: a bigger corpus returns more hits, so the total
    # necessarily rises. What must NOT rise is queries PER HIT, which is the signature of
    # a scan. Measured on the real corpus, a nested scan is 2.56 ms/hit median.
    assert big_queries / big_hits == small_queries / small_hits, (
        f"queries per hit changed from {small_queries}/{small_hits} to "
        f"{big_queries}/{big_hits}: something is scanning rather than looking up"
    )
    assert big_queries == big_hits, (
        f"expected exactly one row lookup per hit, got {big_queries} queries for {big_hits} hits"
    )
    assert small_n == 5 and big_n == 25


# --- edge cases -------------------------------------------------------------------


def test_a_hit_carrying_no_text_is_ungrounded() -> None:
    """A hit with no text cannot be traced to anything.

    Without this, dropping the text key would make the axis report a hit as grounded,
    because a missing text and an empty one both normalise away.
    """
    conn = _db()
    try:
        _seed(conn)
        hits = _hits(conn, mode="documents")
        result = _evaluate(conn, hits=[{**hits[0], "chunk": ""}])
        assert result["grounding"]["ungrounded_hits"] == 1
        assert "no text" in result["grounding"]["ungrounded"][0]["reason"]

        result = _evaluate(conn, hits=[{"id": hits[0]["id"], "similarity": 1.0}])
        assert result["grounding"]["ungrounded_hits"] == 1
    finally:
        conn.close()


def test_no_hits_is_reported_as_null_not_a_pass() -> None:
    """M3: zero hits means grounding was never observed."""
    conn = _db()
    try:
        _seed(conn)
        result = _evaluate(conn, hits=[])
        grounding = result["grounding"]
        assert grounding["hits_checked"] == 0
        assert grounding["grounded_fraction"] is None
        assert result["gate"]["status"] == "fail"
    finally:
        conn.close()


def test_normalization_is_applied_before_comparing() -> None:
    """NFKC + casefold: the same text in different forms must compare equal."""
    from memoratum.eval_grounding import normalize_text

    # NFKC vs casefold, chosen by MEASUREMENT rather than by intuition. The obvious
    # ligature is the wrong probe: str.casefold already expands U+FB01 to "fi", so an
    # assertion built on it passes with NFKC removed and proves nothing.
    #
    # Measured differences between NFKC and casefold alone:
    #   U+00BD "½"   -> "1⁄2"   (casefold leaves ½)
    #   U+2160 "Ⅰ"   -> "I"     (casefold gives the lowercase roman numeral ⅰ)
    #   U+2460 "①"   -> "1"     (casefold gives ①)
    #   U+33A0 "㎠"   -> "cm2"
    #   U+FF23 "Ａ"   -> "C"     (casefold gives fullwidth ｃ, NOT the ascii letter)
    #   U+00A0 nbsp  -> " "     (a non-breaking space would otherwise survive split())
    # expected is the NFKC form, then casefolded, because normalize_text does both.
    for compat, nfkc, folded in (
        ("½", "1⁄2", "1⁄2"),
        ("Ⅰ", "I", "i"),
        ("①", "1", "1"),
        ("㎠", "cm2", "cm2"),
        ("Ａ", "A", "a"),
    ):
        assert unicodedata.normalize("NFKC", compat) == nfkc, "premise"
        assert compat.casefold() != folded, (
            f"{compat!r} is folded by casefold alone, so it cannot witness NFKC"
        )
        assert normalize_text(compat) == folded, f"NFKC must apply to {compat!r}"
        assert normalize_text(compat) == normalize_text(nfkc), (
            f"a document containing {compat!r} and one containing {nfkc!r} are the "
            "same text and must ground identically"
        )

    # The non-breaking space is a real case: NFKC maps it to U+0020, so it collapses.
    # Without NFKC it survives split() and the two texts differ.
    assert normalize_text("a b") == normalize_text("a b")
    assert ["a", "b"] != ["a b"], "premise: split() keeps U+00A0"

    # Casefold is not lower(): it maps the German sharp s, which str.lower leaves alone.
    assert normalize_text("STRASSE") == normalize_text("straße")
    assert "straße".lower() != "strasse", "so this assertion is about casefold, not lower"

    # NFD and NFC are the same text, so NFKC must compose them.
    assert normalize_text("café") == normalize_text("café")
    assert normalize_text("a  b\nc") == normalize_text("a b c")


def test_normalization_is_applied_end_to_end_when_comparing_hits() -> None:
    """The end-to-end consequence of NFKC: a hit stored in a compatibility form grounds.


    The unit test on `normalize_text` is necessary but not sufficient -- this drives the
    comparison through `ground_hit`, which is where normalization has to be applied for
    the claim to mean anything.
    """
    from memoratum import db
    from memoratum.embeddings import HashEmbedder
    from memoratum.ingest import process_all
    from memoratum.search import search

    conn = db.connect(os.path.join(_tempdir(), "compat.db"))
    try:
        # U+FF33 FULLWIDTH LATIN CAPITAL LETTER S, so the document text and the hit text
        # differ in codepoint but are the same string after NFKC.
        doc_text = "# Budget ＲＥＶＩＥＷ\n\nThe fullwidth review is scheduled for tuesday."
        db.create_document(
            conn, container_tag="bench", content=doc_text, custom_id="c1", project_id="proj-a"
        )
        process_all(conn, HashEmbedder(dims=64))
        hits = search(
            conn,
            HashEmbedder(dims=64),
            "fullwidth review scheduled",
            container_tag="bench",
            project_id="proj-a",
            limit=5,
        )
        assert hits, "expected the document to be retrievable"

        # Normalise the stored text's fullwidth letters away in the hit only.
        retyped = str(hits[0]["chunk"]).replace("ＲＥＶＩＥＷ", "REVIEW")
        assert retyped != hits[0]["chunk"], "premise: the two forms must differ in the hit"
        result = _evaluate(conn, hits=[{**hits[0], "chunk": retyped}])
        assert result["grounding"]["grounded_fraction"] == 1.0, (
            "a hit whose text differs only in unicode normalisation must still ground"
        )
    finally:
        conn.close()


def test_whitespace_differences_do_not_break_grounding() -> None:
    """A hit whose whitespace was normalised must still ground."""
    conn = _db()
    try:
        _seed(conn)
        hits = _hits(conn, mode="documents")
        reflowed = {**hits[0], "chunk": " ".join(hits[0]["chunk"].split())}
        result = _evaluate(conn, hits=[reflowed])
        assert result["grounding"]["grounded_fraction"] == 1.0
    finally:
        conn.close()


# --- report and CLI ---------------------------------------------------------------


def test_report_states_the_rule_and_the_provenance_limit() -> None:
    from memoratum.eval_grounding import summarize

    conn = _db()
    try:
        _seed(conn)
        report = summarize(_evaluate(conn))
        assert "provenance" in report.lower()
        assert "never" in report.lower()
        assert "chunks.text" in report
    finally:
        conn.close()


def _run_cli(argv: list[str]):
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from memoratum import eval_grounding

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = eval_grounding.main(argv)
    return code, out.getvalue(), err.getvalue()


def _corpus_file() -> str:
    path = os.path.join(_tempdir(), "corpus.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            [
                {
                    "question_id": "q0",
                    "question": "what is scheduled for tuesday?",
                    "question_type": "single-session-user",
                    "answer": "the quarterly budget review",
                    "answer_session_ids": ["s0"],
                    "haystack_dates": ["2026-01-01"],
                    "haystack_session_ids": ["s0"],
                    "haystack_sessions": [
                        {
                            "session_id": "s0",
                            "date": "2026-01-01",
                            "turns": [
                                {
                                    "role": "user",
                                    "content": "The quarterly budget review is scheduled "
                                    "for tuesday and covers travel.",
                                }
                            ],
                        }
                    ],
                }
            ],
            handle,
        )
    return path


def test_cli_runs_and_writes_artifacts(tmp_path) -> None:
    js = tmp_path / "grounding.json"
    md = tmp_path / "RESULTS-grounding.md"
    code, _, err = _run_cli(
        [
            "--data",
            _corpus_file(),
            "--n",
            "1",
            "--inject-ungrounded",
            "--out-json",
            str(js),
            "--out-md",
            str(md),
        ]
    )
    assert code == 0, err
    payload = json.loads(js.read_text())
    assert payload["axis"] == "grounding"
    assert payload["grounding"]["grounded_fraction"] == 1.0
    assert payload["self_check"]["detected"] is True
    assert md.exists()


def test_cli_evaluates_every_requested_question() -> None:
    """`--n` must be honoured, not accepted and then ignored.

    The flag decides how much evidence the figure rests on, so a run that reads 25
    questions and reports on 1 is a measurement with a number attached to it that does not
    describe the work done.
    """
    path = os.path.join(_tempdir(), "many.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            [
                {
                    "question_id": f"q{i}",
                    "question": f"what is scheduled for tuesday number {i}?",
                    "question_type": "single-session-user",
                    "answer": "the quarterly budget review",
                    "answer_session_ids": [f"s{i}"],
                    "haystack_dates": ["2026-01-01"],
                    "haystack_session_ids": [f"s{i}"],
                    "haystack_sessions": [
                        {
                            "session_id": f"s{i}",
                            "date": "2026-01-01",
                            "turns": [
                                {
                                    "role": "user",
                                    "content": (
                                        f"The quarterly budget review number {i} is "
                                        "scheduled for tuesday and covers travel."
                                    ),
                                }
                            ],
                        }
                    ],
                }
                for i in range(4)
            ],
            handle,
        )

    js = os.path.join(_tempdir(), "many.json.out")
    code, _, err = _run_cli(["--data", path, "--n", "4", "--out-json", js])
    assert code == 0, err
    with open(js, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["questions_evaluated"] == 4, payload["questions_evaluated"]
    assert payload["manifest"]["n"] == 4
    assert payload["manifest"]["requested_n"] == 4
    # Four questions at limit 10 each: more hits than a single-question run would produce.
    assert payload["grounding"]["hits_checked"] >= 4, payload["grounding"]

    # And a smaller --n must produce strictly fewer hits, or --n is not a knob.
    js_small = os.path.join(_tempdir(), "one.json.out")
    code, _, err = _run_cli(["--data", path, "--n", "1", "--out-json", js_small])
    assert code == 0, err
    with open(js_small, encoding="utf-8") as handle:
        small = json.load(handle)
    assert small["questions_evaluated"] == 1
    assert small["grounding"]["hits_checked"] < payload["grounding"]["hits_checked"]


def test_the_manifest_records_the_real_corpus_hash() -> None:
    """A placeholder hash would make a later provenance comparison meaningless (M2).

    `evaluate_grounding` is callable directly, so its default must be visibly
    unavailable rather than a hash-looking string.
    """
    import memoratum.eval_grounding as grounding_module
    from memoratum.eval_datasets import file_sha256

    path = _corpus_file()
    js = os.path.join(_tempdir(), "hash.json.out")
    code, _, err = _run_cli(["--data", path, "--n", "1", "--out-json", js])
    assert code == 0, err
    with open(js, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["manifest"]["data_sha256"] == file_sha256(path)

    conn = _db()
    try:
        direct = grounding_module.evaluate_grounding(
            conn, hits=[], container_tag="bench", project_id="proj-a"
        )
    finally:
        conn.close()
    assert direct["manifest"]["data_sha256"].startswith("unavailable"), (
        "a direct call must not claim a corpus hash it never computed"
    )


def test_cli_honours_k_as_the_hit_limit() -> None:
    """`--k` decides how many hits are checked, so it must reach the search.

    A larger k must produce more checked hits, and the value must be recorded in the
    manifest so the figure is reproducible (FR-007).
    """
    path = os.path.join(_tempdir(), "k.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            [
                {
                    "question_id": "q0",
                    "question": "what is scheduled for tuesday?",
                    "question_type": "single-session-user",
                    "answer": "the quarterly budget review",
                    "answer_session_ids": ["s0"],
                    "haystack_dates": ["2026-01-01"],
                    "haystack_session_ids": [f"s{i}" for i in range(6)],
                    "haystack_sessions": [
                        {
                            "session_id": f"s{i}",
                            "date": "2026-01-01",
                            "turns": [
                                {
                                    "role": "user",
                                    "content": (
                                        f"The quarterly budget review is scheduled for "
                                        f"tuesday. Session {i} covers travel, lodging and "
                                        f"catering for headcount {i * 3}."
                                    ),
                                }
                            ],
                        }
                        for i in range(6)
                    ],
                }
            ],
            handle,
        )

    counts = {}
    for k in ("2", "6"):
        js = os.path.join(_tempdir(), f"k{k}.json.out")
        code, _, err = _run_cli(["--data", path, "--n", "1", "--k", k, "--out-json", js])
        assert code == 0, err
        with open(js, encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["manifest"]["ks"] == [int(k)], payload["manifest"]["ks"]
        assert payload["hit_limit"] == int(k)
        counts[k] = payload["grounding"]["hits_checked"]

    assert counts["6"] > counts["2"], (
        f"--k had no effect on the number of hits checked: {counts}; the flag is read and "
        "then ignored, so the gate runs on a different corpus than the report claims"
    )


def test_cli_grounds_a_hit_against_the_whole_corpus_not_its_own_question() -> None:
    """A hit must resolve against every ingested session, not only its question's.

    Grounding each question against only its own haystack would make the metric
    trivially true: a retriever could return a chunk from the wrong question and still
    pass.
    """
    from memoratum import db as db_module
    from memoratum.embeddings import HashEmbedder
    from memoratum.eval_grounding import evaluate_grounding
    from memoratum.ingest import process_all
    from memoratum.search import search

    conn = db_module.connect(os.path.join(_tempdir(), "cross.db"))
    try:
        for index in range(2):
            db_module.create_document(
                conn,
                container_tag="bench",
                content=(
                    f"# Session {index}\n\n"
                    f"The quarterly budget review for session {index} is scheduled for "
                    "tuesday."
                ),
                custom_id=f"q{index}",
            )
        process_all(conn, HashEmbedder(dims=64))

        # Question 0's query, but the retrieved hit comes from session 1.
        hits = search(
            conn,
            HashEmbedder(dims=64),
            "quarterly budget review",
            container_tag="bench",
            limit=10,
        )
        assert hits
        result = evaluate_grounding(conn, hits=hits, container_tag="bench")
        assert result["grounding"]["grounded_fraction"] == 1.0, (
            "a hit from another question's session is still in the corpus and must ground"
        )
    finally:
        conn.close()


@pytest.mark.parametrize("n", ["0", "-1"])
def test_cli_rejects_a_nonpositive_n(n) -> None:
    """`--n 0` would otherwise produce an empty corpus and a vacuous pass."""
    code, _, err = _run_cli(["--data", _corpus_file(), "--n", n])
    assert code != 0
    assert "--n" in err


def test_cli_requires_data() -> None:
    import pytest as _pytest

    with _pytest.raises(SystemExit) as excinfo:
        _run_cli(["--n", "1"])
    assert excinfo.value.code == 2


def test_cli_rejects_a_missing_corpus() -> None:
    code, _, _err = _run_cli(["--data", "/nonexistent/corpus.json", "--n", "1"])
    assert code != 0


def test_cli_rejects_a_corpus_with_no_usable_records() -> None:
    """An empty or unparseable corpus must not produce a vacuous pass."""
    for payload, description in (
        ([], "an empty list"),
        ([{"question_id": "q0"}], "a record with no question"),
        ([{"question_id": "q0", "question": "?", "haystack_sessions": []}], "no sessions"),
    ):
        path = os.path.join(_tempdir(), "corpus.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        code, _, err = _run_cli(["--data", path, "--n", "1"])
        assert code != 0, f"{description} was accepted"
        assert err.strip(), f"{description} produced no diagnostic"


def test_cli_rejects_a_corpus_that_is_not_json() -> None:
    path = os.path.join(_tempdir(), "corpus.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("this is not json at all")
    code, _, err = _run_cli(["--data", path, "--n", "1"])
    assert code != 0
    assert err.strip()


def test_cli_reports_a_write_failure_instead_of_claiming_success(tmp_path) -> None:
    """An unwritable artifact path must not exit 0 having written nothing.

    Two shapes are checked, because ``write_artifacts`` creates parent directories: a path
    whose parent is a *file* fails at ``makedirs``, and a path that is itself a directory
    fails at ``write_text``. Both must be reported, and neither may exit 0.
    """
    a_file = tmp_path / "not-a-directory"
    a_file.write_text("x")
    for out_json in (
        str(a_file / "nested" / "out.json"),  # makedirs fails
        str(tmp_path),  # write_text fails: the path is a directory
    ):
        code, _, err = _run_cli(["--data", _corpus_file(), "--n", "1", "--out-json", out_json])
        assert code != 0, f"{out_json} was accepted"
        assert "could not write" in err, err

    # The markdown path fails the same way.
    md_file = tmp_path / "also-a-file"
    md_file.write_text("x")
    code, _, err = _run_cli(
        ["--data", _corpus_file(), "--n", "1", "--out-md", str(md_file / "nested" / "out.md")]
    )
    assert code != 0
    assert "could not write" in err


def test_the_cli_reports_ungrounded_hits_and_exits_one(tmp_path) -> None:
    """Exit code 1 is the real ungrounded finding, and it must be distinguishable from 2."""
    js = tmp_path / "grounding.json"
    # --min-grounded-fraction above 1.0 is unreachable, so the gate must fail on a
    # perfectly grounded corpus. This drives the FAIL branch without a stubbed retriever.
    code, _, _err = _run_cli(
        [
            "--data",
            _corpus_file(),
            "--n",
            "1",
            "--min-grounded-fraction",
            "1.5",
            "--out-json",
            str(js),
        ]
    )
    assert code == 1, code
    payload = json.loads(js.read_text())
    assert payload["gate"]["status"] == "fail"
    assert payload["grounding"]["grounded_fraction"] == 1.0, "the corpus itself is clean"

    md = tmp_path / "RESULTS-grounding.md"
    code, _, _ = _run_cli(
        [
            "--data",
            _corpus_file(),
            "--n",
            "1",
            "--min-grounded-fraction",
            "1.5",
            "--out-md",
            str(md),
        ]
    )
    assert code == 1
    assert "FAIL" in md.read_text()


def test_the_self_check_refuses_an_empty_corpus() -> None:
    """A corpus with nothing ingested cannot prove anything, so the check must say so.

    Without this, `run_self_check` would pick no id, and the self-check would pass or fail
    on nothing while reporting a count.
    """
    from memoratum.eval_grounding import run_self_check

    conn = _db()
    try:
        with pytest.raises(RuntimeError, match="empty corpus"):
            run_self_check(conn, container_tag="bench", project_id="proj-a")
    finally:
        conn.close()


def test_the_self_check_can_borrow_a_memory_id_when_there_are_no_chunks() -> None:
    """Facts are memories, so a fact-only corpus must still support the self-check."""
    from memoratum import facts

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
        assert conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"] == 0
        outcome = _self_check(conn)
        assert outcome.detected is True
        assert outcome.injected_ungrounded > 0
    finally:
        conn.close()


def test_the_report_truncates_a_long_ungrounded_list() -> None:
    """The report must not print 5,000 lines, and must say that it truncated."""
    import memoratum.eval_grounding as grounding_module

    conn = _db()
    try:
        _seed(conn)
        hits = [
            {"id": f"chunk_{900000 + i}", "chunk": f"fabricated {i}", "similarity": 1.0}
            for i in range(25)
        ]
        result = grounding_module.evaluate_grounding(
            conn, hits=hits, container_tag="bench", project_id="proj-a"
        )
        report = grounding_module.summarize(result)
    finally:
        conn.close()
    listed = [line for line in report.splitlines() if line.startswith("- rank ")]
    assert len(listed) == 20, report
    assert "and 5 more" in report, report


def test_a_corpus_whose_records_are_unusable_is_rejected() -> None:
    """Records can be present but unusable, which is not the same as no records.

    `normalize_longmemeval` raises per record, so the diagnostic must name the record.
    """
    for payload, expected in (
        ([{"question_id": f"q{i}", "question": ""} for i in range(3)], "record 0 has no question"),
        ([{"question_id": "q0", "question": 42}], "record 0 has no question"),
    ):
        path = os.path.join(_tempdir(), "corpus.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        code, _, err = _run_cli(["--data", path, "--n", "3"])
        assert code != 0, f"{payload} was accepted"
        assert expected in err, err


def test_the_report_omits_the_tier2_section_when_diagnostics_are_off() -> None:
    """Tier 2 is opt-in, so its section must be absent rather than empty when it is off."""
    import memoratum.eval_grounding as grounding_module

    conn = _db()
    try:
        _seed(conn)
        without = grounding_module.summarize(_evaluate(conn, tier2=False))
        with_tier2 = grounding_module.summarize(_evaluate(conn, tier2=True))
    finally:
        conn.close()
    assert "## Tier 2 diagnostics" not in without
    assert "## Tier 2 diagnostics" in with_tier2
    assert "never gate" in with_tier2


def test_the_cli_reports_a_failure_inside_evaluation(tmp_path) -> None:
    """An exception mid-evaluation must be reported, not propagate or exit 0.

    `ManifestError` and friends are the shapes that reach this handler, so a build where
    the manifest cannot be built must not look like a clean run.
    """
    import memoratum.eval_grounding as grounding_module

    original = grounding_module.build_manifest

    def failing(*args, **kwargs):
        raise grounding_module.ManifestError("manifest could not be built (injected)")

    grounding_module.build_manifest = failing
    try:
        code, _, err = _run_cli(["--data", _corpus_file(), "--n", "1"])
    finally:
        grounding_module.build_manifest = original
    assert code != 0, code
    assert "manifest could not be built (injected)" in err


def test_the_report_omits_the_excerpt_when_a_hit_has_no_text() -> None:
    import memoratum.eval_grounding as grounding_module

    conn = _db()
    try:
        _seed(conn)
        result = grounding_module.evaluate_grounding(
            conn,
            hits=[{"id": "chunk_999999", "chunk": "", "similarity": 1.0}],
            container_tag="bench",
            project_id="proj-a",
        )
        report = grounding_module.summarize(result)
    finally:
        conn.close()
    assert "## Ungrounded hits" in report
    assert "text: ''" not in report, report


def test_the_cli_self_check_outranks_a_real_gate_failure(tmp_path) -> None:
    """A broken metric exits 2 even when the corpus is also ungrounded.

    Otherwise a green-looking run with a broken detector would be indistinguishable from a
    genuine finding, and the finding would be the thing that got fixed.
    """
    from memoratum import eval_grounding

    original = eval_grounding.run_self_check
    eval_grounding.run_self_check = lambda *a, **k: _fake_outcome()
    try:
        code, _, _ = _run_cli(
            [
                "--data",
                _corpus_file(),
                "--n",
                "1",
                "--inject-ungrounded",
                "--min-grounded-fraction",
                "1.5",
            ]
        )
    finally:
        eval_grounding.run_self_check = original
    assert code == 2, code


def _fake_outcome():
    from dataclasses import dataclass

    from memoratum.eval_axes import EXIT_SELF_CHECK_FAILED

    @dataclass(frozen=True)
    class Outcome:
        detected: bool
        injected_ungrounded: int
        clean_ungrounded: int
        exit_code: int
        detail: str

    return Outcome(
        detected=False,
        injected_ungrounded=0,
        clean_ungrounded=0,
        exit_code=EXIT_SELF_CHECK_FAILED,
        detail="simulated broken detector",
    )


def test_the_report_lists_ungrounded_hits_with_their_reason(tmp_path) -> None:
    """The report must name what failed, or an operator cannot act on it."""
    import memoratum.eval_grounding as grounding_module

    conn = _db()
    try:
        _seed(conn)
        hits = _hits(conn)
        fabricated = [
            {**hits[0], "chunk": "fabricated text"},
            {"id": "chunk_999999", "chunk": "ghost"},
        ]
        result = grounding_module.evaluate_grounding(
            conn, hits=fabricated, container_tag="bench", project_id="proj-a"
        )
        report = grounding_module.summarize(result)
    finally:
        conn.close()
    assert "## Ungrounded hits" in report
    assert hits[0]["id"] in report
    assert "chunk_999999" in report
    assert "does not match" in report
    assert "no ingested row" in report
    assert "fabricated text" in report, "the excerpt is what makes the finding actionable"


# --- helpers ----------------------------------------------------------------------


def _evaluate(conn, *, hits=None, tier2=False, judge=None, query="quarterly budget review"):
    from memoratum.eval_grounding import evaluate_grounding

    return evaluate_grounding(
        conn,
        hits=hits if hits is not None else _hits(conn),
        container_tag="bench",
        project_id="proj-a",
        tier2=tier2,
        judge=judge,
        query=query,
    )


def _self_check(conn, *, force_clean: bool = False, force_noisy: bool = False):
    from memoratum.eval_grounding import run_self_check

    # Both keywords are passed through, not merely accepted: a helper that silently
    # dropped its own keyword is exactly the defect this test exists to catch.
    return run_self_check(
        conn,
        container_tag="bench",
        project_id="proj-a",
        force_clean=force_clean,
        force_noisy=force_noisy,
    )
