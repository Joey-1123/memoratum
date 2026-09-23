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

The built-in adapter sends only the model, system/user messages, and temperature
to the configured endpoint. Provider adapters share the same `complete` contract,
so dreaming, query expansion, and future provider additions do not change the
worker or API layers.

Implementation references:

- https://docs.litellm.ai/docs/completion/input
- https://docs.litellm.ai/docs/providers/openai_compatible
