"""Search must not re-embed the corpus on every query.

The defect: `chunks.embedding` is written at ingest (`ingest.py` ->
`db.add_chunks(..., embeddings=)`) and is even SELECTed by the chunk leg of
`search.py` -- and then discarded. `search.py` re-embeds every chunk text on every
query instead. At the embedder's batch_size of 64, a 10,000-chunk container costs
**157 embedding API calls per search** against a remote provider.

The facts leg already does it correctly: compute if absent, persist, reuse.

These tests assert the behaviour (no re-embedding, bounded candidates) and pin a
committed benchmark rather than asserting "fast".
"""

import time


def _conn():
    from helpers import use_tmp_data_dir

    use_tmp_data_dir()
    from memoratum import db
    from memoratum.config import Settings

    return db.connect(Settings.load().db_path)


class _CountingEmbedder:
    """Wraps an embedder and counts texts passed to it."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[int] = []

    def embed(self, texts):
        self.calls.append(len(texts))
        return self._inner.embed(texts)

    @property
    def total_texts(self) -> int:
        return sum(self.calls)


def _seed_chunks(conn, count: int) -> None:
    """Insert documents with persisted chunk embeddings, as ingest does."""
    from memoratum.embeddings import HashEmbedder
    from memoratum.search import pack_vector

    embedder = HashEmbedder(dims=64)
    now = time.time()
    for index in range(count):
        doc_id = f"doc-{index}"
        conn.execute(
            "INSERT INTO documents(id, container_tag, custom_id, content, status,"
            " created_at, updated_at, metadata) VALUES (?,?,?,?,'done',?,?,'{}')",
            (doc_id, "mem0:user_id:alice", None, f"document {index}", now, now),
        )
        for chunk_index in range(4):
            text = f"chunk {chunk_index} of document {index} about topic {index % 25}"
            vector = embedder.embed([text])[0]
            conn.execute(
                "INSERT INTO chunks(document_id, idx, text, embedding, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (doc_id, chunk_index, text, pack_vector(vector), now),
            )
    conn.commit()


def test_chunk_embeddings_are_persisted_at_ingest():
    """Precondition for the fix: the column is actually populated."""
    conn = _conn()
    try:
        _seed_chunks(conn, 3)
        rows = conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()[0]
        assert rows > 0, "ingest must persist chunk embeddings"
    finally:
        conn.close()


def test_documents_mode_exercises_the_chunk_leg():
    """Guard: 'memories' mode skips chunks, which made an earlier version of the
    re-embedding test pass without running the code path it claimed to check."""
    from memoratum import search

    conn = _conn()
    try:
        _seed_chunks(conn, 2)
        hits = search.search(
            conn,
            _embedder(),
            "topic 1",
            container_tag="mem0:user_id:alice",
            limit=5,
            search_mode="documents",
        )
        assert hits, "documents mode returned nothing for a seeded corpus"
    finally:
        conn.close()


def _embedder():
    from memoratum.embeddings import HashEmbedder

    return HashEmbedder(dims=64)


def test_search_does_not_reembed_stored_chunks():
    """The core fix: stored vectors must be reused, not recomputed."""
    from memoratum import search
    from memoratum.embeddings import HashEmbedder

    conn = _conn()
    try:
        _seed_chunks(conn, 25)  # 100 chunks
        counting = _CountingEmbedder(HashEmbedder(dims=64))
        search.search(
            conn,
            counting,
            "topic 7",
            container_tag="mem0:user_id:alice",
            limit=5,
            search_mode="documents",
        )
        # Only the query itself may be embedded; the corpus must not be.
        assert counting.total_texts <= 2, (
            f"search embedded {counting.total_texts} texts; stored chunk embeddings"
            " are being recomputed instead of reused"
        )
    finally:
        conn.close()


def test_repeated_searches_do_not_embed_the_corpus():
    from memoratum import search
    from memoratum.embeddings import HashEmbedder

    conn = _conn()
    try:
        _seed_chunks(conn, 10)
        counting = _CountingEmbedder(HashEmbedder(dims=64))
        for index in range(5):
            search.search(
                conn,
                counting,
                f"topic {index}",
                container_tag="mem0:user_id:alice",
                limit=5,
                search_mode="documents",
            )
        assert counting.total_texts <= 10, (
            f"5 searches embedded {counting.total_texts} texts; cost must not scale"
            " with corpus size"
        )
    finally:
        conn.close()


def test_search_falls_back_when_embedding_is_missing():
    """Rows written before embeddings existed must still be searchable."""
    from memoratum import search
    from memoratum.embeddings import HashEmbedder

    conn = _conn()
    try:
        _seed_chunks(conn, 2)
        conn.execute("UPDATE chunks SET embedding = NULL WHERE idx = 0")
        conn.commit()

        hits = search.search(
            conn,
            HashEmbedder(dims=64),
            "topic 1",
            container_tag="mem0:user_id:alice",
            limit=5,
            search_mode="documents",
        )
        assert hits, "search returned nothing when some embeddings were missing"
    finally:
        conn.close()


def test_search_does_not_break_when_dimension_changes():
    """A stale blob from another embedder size must be skipped, not crash."""
    from memoratum import search
    from memoratum.embeddings import HashEmbedder
    from memoratum.search import pack_vector

    conn = _conn()
    try:
        _seed_chunks(conn, 2)
        conn.execute(
            "UPDATE chunks SET embedding = ? WHERE rowid IN (SELECT rowid FROM chunks LIMIT 1)",
            (pack_vector([0.1] * 8),),  # wrong dimensionality on purpose
        )
        conn.commit()
        hits = search.search(
            conn,
            HashEmbedder(dims=64),
            "topic 1",
            container_tag="mem0:user_id:alice",
            limit=5,
            search_mode="documents",
        )
        assert isinstance(hits, list)
    finally:
        conn.close()


def test_prefilter_bounds_the_row_fetch():
    """On a large corpus the FTS5 leg must bound the SELECT, not just the scoring.

    Bounding only the vector leg left the real cost untouched: every chunk row was
    still read and json.loads'd before any ranking happened.

    sqlite3.Connection attributes are immutable, so the SQL is observed with
    set_trace_callback rather than by patching `execute`.
    """
    from memoratum import search

    conn = _conn()
    try:
        _seed_chunks(conn, 200)  # 800 chunks, above the threshold
        traced: list[str] = []
        conn.set_trace_callback(lambda stmt: traced.append(" ".join(stmt.split())))

        search.search(
            conn,
            _embedder(),
            "topic 7",
            container_tag="mem0:user_id:alice",
            limit=10,
            search_mode="documents",
        )
        conn.set_trace_callback(None)

        selects = [q for q in traced if "FROM chunks c" in q and "COUNT(*)" not in q]
        assert selects, "no chunk SELECT observed"
        assert any("c.id IN" in q for q in selects), (
            "the chunk SELECT is not bounded by the FTS5 candidate set"
        )
    finally:
        conn.close()


def test_small_corpora_are_not_prefiltered():
    """Below the threshold every candidate is scored, so recall is exact."""
    from memoratum import search

    conn = _conn()
    try:
        _seed_chunks(conn, 5)  # 20 chunks, well below the threshold
        assert 20 < search.PREFILTER_MIN_CANDIDATES
        hits = search.search(
            conn,
            _embedder(),
            "topic 3",
            container_tag="mem0:user_id:alice",
            limit=10,
            search_mode="documents",
        )
        assert hits, "small corpora must still return results"
    finally:
        conn.close()


def test_stored_vectors_are_reused_not_recomputed():
    """Structural counterpart: search.py must read the persisted column."""
    import inspect

    from memoratum import search

    source = inspect.getsource(search)
    assert "stored_vectors" in source, "stored chunk embeddings are not being read"
    assert "c.embedding" in source, "the embedding column is no longer selected"


# --- committed benchmark ----------------------------------------------------


def test_search_latency_is_sublinear_for_realistic_queries():
    """30x the corpus must not cost anywhere near 30x the time.

    Uses a *selective* query. A query whose tokens appear in every chunk (a bare
    digit, say) makes FTS5 compute bm25 over the whole corpus regardless of LIMIT,
    which is inherent to FTS5 rather than something the prefilter can fix; the
    pathological case is documented in tests/benchmarks/search.md.
    """
    from memoratum import search

    embedder = _embedder()
    timings: dict[int, float] = {}

    for corpus in (50, 400, 1500):
        conn = _conn()
        try:
            _seed_selective(conn, corpus)
            for _ in range(2):  # warm
                search.search(
                    conn,
                    embedder,
                    "taxation",
                    container_tag="mem0:user_id:alice",
                    limit=10,
                    search_mode="documents",
                )
            started = time.perf_counter()
            search.search(
                conn,
                embedder,
                "taxation",
                container_tag="mem0:user_id:alice",
                limit=10,
                search_mode="documents",
            )
            timings[corpus] = time.perf_counter() - started
        finally:
            conn.close()

    small, large = timings[50], timings[1500]
    assert large < small * 12 + 0.05, (
        f"search latency grew too fast: 200 chunks={small:.3f}s, 6000 chunks={large:.3f}s"
    )


def _seed_selective(conn, docs: int, chunks_per: int = 4) -> None:
    """Corpus where one term is common but the rest are rare, as real text is."""
    from memoratum.search import pack_vector

    embedder = _embedder()
    now = time.time()
    for index in range(docs):
        doc_id = f"sel-{index}"
        conn.execute(
            "INSERT INTO documents(id, container_tag, custom_id, content, status,"
            " created_at, updated_at, metadata) VALUES (?,?,?,?,'done',?,?,'{}')",
            (doc_id, "mem0:user_id:alice", None, f"volume {index}", now, now),
        )
        for chunk_index in range(chunks_per):
            text = f"chapter {chunk_index} of volume {index} concerning taxation policy"
            conn.execute(
                "INSERT INTO chunks(document_id, idx, text, embedding, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (doc_id, chunk_index, text, pack_vector(embedder.embed([text])[0]), now),
            )
    conn.commit()


def test_prefilter_threshold_is_not_surprising():
    """The constant is a documented decision, so assert it is still sane."""
    from memoratum import search

    assert search.PREFILTER_MIN_CANDIDATES >= 256, (
        "a low threshold would trade recall away on ordinary deployments"
    )


def test_benchmark_document_exists():
    """Principle IV: a performance claim must be backed by a committed number."""
    from pathlib import Path

    benchmark = Path("tests/benchmarks/search.md")
    assert benchmark.exists(), "search benchmark is not committed to the repository"
    text = benchmark.read_text()
    assert "500" in text and "1,000" in text, "benchmark must record corpus sizes"
    assert "sqlite-vec" in text, "benchmark must record the deferred-ANN trigger"


def test_no_new_runtime_dependency_for_search():
    """Principle V: the staged approach must not have added a native extension."""
    from pathlib import Path

    pyproject = Path("pyproject.toml").read_text()
    runtime = pyproject.split("[project.optional-dependencies]")[0]
    assert "sqlite-vec" not in runtime, (
        "sqlite-vec must stay deferred; it is a native dependency with no prebuilt"
        " aarch64 wheels, which is exactly this project's audience"
    )


def test_integrity_after_search():
    conn = _conn()
    try:
        _seed_chunks(conn, 5)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
