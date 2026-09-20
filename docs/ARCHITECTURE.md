# Memoratum architecture

Single SQLite file (`memoratum.db`, WAL mode, `busy_timeout`, `FOREIGN_KEYS=ON`),
forward-only migrations in `db.py` (`_MIGRATIONS`, tracked in `schema_migrations`).
One database per deployment; multi-tenancy is logical via `container_tag`, enforced
as a mandatory filter on every read path.

## Tables

- `documents(id, container_tag, custom_id, content, status, metadata, dreamed_at, timestamps)` —
  `UNIQUE(container_tag, custom_id)` makes re-ingest idempotent.
- `chunks(id, document_id → CASCADE, idx, text, embedding BLOB float32, created_at)` +
  `chunks_fts` FTS5 shadow table kept in sync by insert/delete triggers.
- `facts(id, container_tag, subject, predicate, object, document_id → SET NULL,
  valid_from, valid_to, superseded_by, metadata)` with `(container_tag, subject, predicate)` index.
- `api_keys(key_hash PK, container_tag NULL = wildcard)` + `revoked_keys(key_hash PK)`.
  Raw keys exist only at creation time; only SHA-256 hashes are stored.

## Ingest (`ingest.py`)

```
POST /v3/documents → create (or customId upsert: content replaced, chunks
dropped, dreamed_at cleared) → process_one: split_markdown → embed →
add_chunks → status done|failed
```

Upsert resets `dreamed_at` so replaced content re-dreams. Embedding-dimension
changes across ingests fail loudly instead of silently corrupting ranking.
Chunking keeps delimiter-attached units (chunks are verbatim substrings),
prefixes Markdown headings, and keeps small fenced code blocks atomic.

## Dreaming (`dreaming.py` + `facts.py`)

LLM extraction (strict-validated `{subject, predicate, object}` JSON; malformed
output yields nothing, never raises) feeds `add_fact`, which closes the validity
window of live `(subject, predicate)` rows (`valid_to`, `superseded_by`) and
returns the existing row on exact duplicates — history is append-only, truth is
"latest valid wins". Modes: `instant` (one LLM call per document, facts carry
`document_id`) vs `dynamic` (per-tag bundles packed into 8000-char windows;
oversized docs are windowed, never silently dropped). `dream_pending` is scoped
to the ingested tag. `DELETE` endpoints exist for facts, tags, and keys.

## Retrieval (`search.py`)

```
candidates = chunks (+ facts as "subject predicate object")
vector leg   = cosine over stored embeddings (brute force; sqlite-vec upgrade path).
  Chunk vectors embed per search; fact vectors are cached per tag and rebuilt
  only when the tag's live facts change. Embedding calls are batched (64/call).
keyword leg  = FTS5 for chunks (sanitized OR-of-tokens) + token overlap for facts
fusion       = RRF, 0.6 vector / 0.4 keyword, k=60 → threshold → limit
rerank=true  = re-sort top 3×limit by 0.7·fused + 0.3·recency(1/(1+age_days))
```

Notes: `similarity` is a fused RRF score (small numbers, not cosine — so keep
`threshold` low); keyword hits for chunks filtered out by metadata never enter
fusion; fact embeddings are computed per search in one batched call.

## Request flow (`app.py`)

Per-request SQLite connections via dependency (thread-safe under uvicorn
workers). Requests are serialized on a process-wide lock because dependency
setup and endpoint bodies run on different worker threads — single-process
ceiling; HA needs a real DB server. Auth: env admin key, DB scoped/wildcard keys, revocation list;
401 for unknown credentials, 403 for out-of-scope writes, uniform 404 for
missing-or-forbidden reads. Dreaming + ingest run inline in the request path
(no background queue yet — see ceilings in `SECURITY.md`).

## Clients

`client.py` (Python), `clients/ts` (TS), `clients/opencode/memoratum.js`
(recall-on-prompt + store-on-write), `mcp.py` (stdio JSON-RPC
`remember`/`recall`, local-trust only). SDK/client code is MIT (`LICENSE-MIT`);
everything else AGPL-3.0.
