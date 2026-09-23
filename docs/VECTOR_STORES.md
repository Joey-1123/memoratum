# Vector stores

Vector retrieval is defined by a small provider contract: upsert records, query
by vector with a required container scope and optional organization scope, and
delete by ids or scope. The default SQLite implementation stores points in the
same local database; an in-memory implementation is available for tests and
ephemeral indexes. Optional adapters support Qdrant and Chroma installations.

Every point carries its text, kind, scope, metadata, and creation time so a
backend can return useful candidates without reopening the relational store.
Metadata filters are applied before ranking. External backends can replace the
SQLite implementation without changing API or embedding contracts.

Install optional backends with `uv sync --extra qdrant` or
`uv sync --extra chroma`. Configure the provider, endpoint, collection, and
credentials through the vector-store settings when search wiring is enabled.

The adapters use explicit vectors and scope metadata, so the embedding
provider remains independent from the vector backend.

References:
- https://qdrant.tech/documentation/manage-data/collections/
- https://qdrant.tech/documentation/search/
- https://docs.trychroma.com/docs/querying-collections/query-and-get
- https://docs.trychroma.com/docs/collections/manage-collections
