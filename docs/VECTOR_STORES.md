# Vector stores

Vector retrieval is defined by a small provider contract: upsert records, query
by vector with a required container scope and optional organization scope, and
delete by ids or scope. The default SQLite implementation stores points in the
same local database; an in-memory implementation is available for tests and
ephemeral indexes. Optional adapters support Qdrant, Chroma, and Postgres/pgvector
installations.

Every point carries its text, kind, scope, metadata, and creation time so a
backend can return useful candidates without reopening the relational store.
Metadata filters are applied before ranking. External backends can replace the
SQLite implementation without changing API or embedding contracts.

Install optional backends with `uv sync --extra qdrant`,
`uv sync --extra chroma`, or `uv sync --extra pgvector`. The Postgres adapter
expects the `vector` extension to be available in the target database.

The adapters use explicit vectors and scope metadata, so the embedding provider
remains independent from the vector backend.

Configure the active index with `MEMORATUM_VECTOR_STORE` (`sqlite`, `qdrant`,
`chroma`, `pgvector`, or `memory`), `MEMORATUM_VECTOR_STORE_ENDPOINT`,
`MEMORATUM_VECTOR_STORE_KEY`, `MEMORATUM_VECTOR_STORE_PATH`,
`MEMORATUM_VECTOR_STORE_COLLECTION`, and optional
`MEMORATUM_VECTOR_STORE_DIMS`. The default SQLite index is populated by the
worker and used by search; if it is empty or unavailable, search falls back to
the relational cosine path.

References:
- https://qdrant.tech/documentation/manage-data/collections/
- https://qdrant.tech/documentation/search/
- https://docs.trychroma.com/docs/querying-collections/query-and-get
- https://docs.trychroma.com/docs/collections/manage-collections
- https://github.com/pgvector/pgvector
