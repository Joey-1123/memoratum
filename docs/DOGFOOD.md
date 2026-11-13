# Dogfood evidence

## OpenCode memory layer — 2026-11-13

The whole loop was exercised against a running Memoratum and a real OpenCode
plugin: capture at compaction, ingest, dreaming, fact extraction, and recall
injected into a later session's model context.

### Environment

- Memoratum: `python -m memoratum` plus `python -m memoratum.worker`, a
  temporary SQLite data directory, admin key from `MEMORATUM_API_KEY`
- Embeddings: the default `hash` provider (offline, deterministic)
- Chat model for dreaming: a local OpenAI-compatible stub that extracts a
  decision-shaped fact from the transcript it is given. It is not a model.
  A stub was used because the run had to be repeatable and vendor-free; the
  stub proves the transcript reaches the dreaming step with the right
  content, and the fact it returns is derived from that transcript rather
  than being a canned constant
- OpenCode: 1.19.3, headless `opencode serve`, the plugin installed as a
  package directory referenced from the project's `opencode.jsonc`
- Network: loopback only; no hosted memory or telemetry service

### Flow

1. A session was created and given the prompt *"we decided to use RRF fusion
   instead of weighted cosine because FTS5 has no vector index"*.
2. `POST /session/:id/summarize` forced the compaction path.
3. The plugin's compaction hook fired and stored the transcript as one
   document with `customId = oc:<sessionID>:<lastMessageID>`.
4. The worker ingested the document (`status: done`) and the dreaming step
   called the chat endpoint with the transcript text.
5. The extracted fact was
   `the team | decided to use | use RRF fusion instead of weighted cosine because FTS5 has no vector index`.
6. A second session asked *"why did we choose RRF?"*. The plugin's recall
   ran against the project tag and injected this into the model's context:

   ```
   [memoratum]
   - the team decided to use use RRF fusion instead of weighted cosine because FTS5 has no vector index
   ```

### What the run measured

- `similarity` for the matching fact was **0.009836**. This is an RRF fusion
  score, not a cosine, and it confirms the warning in
  `docs/ARCHITECTURE.md`: the value is small, and a cosine-style threshold
  would reject everything. Recall is bounded by hit count and character
  budget instead.
- The plugin's requests were visible in the server's local usage counters:
  one search and one document write under the project tag.

### What the run did not prove

- The `hash` embeddings provider carries no semantic similarity, so the
  matching here came from the keyword leg. Retrieval quality against a real
  embeddings provider is covered by the evaluation harness, not by this run.
- The chat model was a stub, so the *quality* of extracted facts is not
  evidenced here — only that the pipeline carries the transcript into the
  extraction step and stores what comes back.
- The V2 adapter was not exercised against a live V2 build; no stable V2
  OpenCode exists. Its behaviour is covered by the unit suite against the
  documented hook contract.

## Earlier run — repository docs with local Ollama embeddings

The release path was exercised locally against the running service, the
repository README/API/security docs, and local Ollama embeddings.

### Environment

- Embeddings: local Ollama `nomic-embed-text` through its OpenAI-compatible
  endpoint, 768 dimensions
- Storage: temporary local SQLite data directory
- Network: loopback only; no hosted memory or telemetry service
- Documents: `README.md`, `docs/API.md`, and `docs/SECURITY.md`

### Flow

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
