# Vector stores

Vector retrieval is defined by a small provider contract: upsert records, query
by vector with a required container scope and optional organization scope, and
delete by ids or scope. The default SQLite implementation stores points in the
same local database; an in-memory implementation is available for tests and
ephemeral indexes.

Every point carries its text, kind, scope, metadata, and creation time so a
backend can return useful candidates without reopening the relational store.
Metadata filters are applied before ranking. External backends can replace the
SQLite implementation without changing API or embedding contracts.

The next backend slices add remote/local store adapters and move vector
candidate selection behind the same contract.

Reference: https://qdrant.github.io/fastembed/
