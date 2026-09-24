# Dogfood evidence

The release path was exercised locally against the running service, the
repository README/API/security docs, and local Ollama embeddings.

## Environment

- Embeddings: local Ollama `nomic-embed-text` through its OpenAI-compatible
  endpoint, 768 dimensions
- Storage: temporary local SQLite data directory
- Network: loopback only; no hosted memory or telemetry service
- Documents: `README.md`, `docs/API.md`, and `docs/SECURITY.md`

## Flow

1. Start `python -m memoratum` and `python -m memoratum.worker` with the local
   embedding settings.
2. Add the three documents to the `repo-docs` container.
3. Poll each async ingest job to `done`.
4. Search for exact concepts from the imported documents, including the
   revoked-key rule, the telemetry boundary, and SQLite backup behavior.
5. Stop both processes and remove the temporary data directory.

The run returned document hits for the API/security content and local search
results for the repository documentation. The same setup was also exercised
with an instant-dreaming marker document; the local chat model produced a fact
and a subsequent `memories` search returned it.

This is a local smoke gate, not a published benchmark result. Full benchmark
commands and manifests live in [`EVALUATION.md`](EVALUATION.md).
