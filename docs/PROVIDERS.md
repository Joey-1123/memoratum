# Provider configuration

The default chat path is a dependency-free OpenAI-compatible HTTP adapter. Set
`MEMORATUM_LLM_PROVIDER=litellm` to use the optional provider gateway; install
its extra with `uv sync --extra llm`. When that package is unavailable, an
explicitly configured compatible endpoint is used as a safe fallback. Without a
model, dreaming and query expansion remain disabled.

| Variable | Purpose | Default |
|---|---|---|
| `MEMORATUM_LLM_PROVIDER` | `openai`, `ollama`, `vllm`, or `litellm` | `openai` |
| `MEMORATUM_LLM_ENDPOINT` | OpenAI-compatible base URL | empty |
| `MEMORATUM_LLM_MODEL` | Model identifier | empty |
| `MEMORATUM_LLM_KEY` | Provider credential, when required | empty |
| `MEMORATUM_EMBEDDINGS_PROVIDER` | `hash`, `api`, `litellm`, or `fastembed` | `hash` |
| `MEMORATUM_EMBEDDINGS_ENDPOINT` | OpenAI-compatible embeddings base URL | empty |
| `MEMORATUM_EMBEDDINGS_MODEL` | Embedding model identifier | empty |
| `MEMORATUM_EMBEDDINGS_DIMS` | Optional expected vector width | `0` (auto) |
| `MEMORATUM_EMBEDDINGS_KEY` | Embedding credential, when required | empty |

Install local embeddings with `uv sync --extra embeddings`. The local adapter
loads its model lazily on the first batch, so application startup does not
trigger a model download.

The built-in adapter sends only the model, system/user messages, and temperature
to the configured endpoint. Provider adapters share the same `complete` contract,
so dreaming, query expansion, and future provider additions do not change the
worker or API layers.

Implementation references:

- https://docs.litellm.ai/docs/completion/input
- https://docs.litellm.ai/docs/embedding/supported_embedding
- https://docs.litellm.ai/docs/providers/openai_compatible
- https://qdrant.github.io/fastembed/
