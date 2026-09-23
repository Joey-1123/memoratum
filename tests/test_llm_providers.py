"""Provider-neutral LLM adapter contract (RED)."""

import sys
import types


def test_openai_compatible_chat_uses_messages_shape() -> None:
    from memoratum.llm import OpenAICompatibleChat

    chat = OpenAICompatibleChat(endpoint="http://local/v1", model="m")
    assert hasattr(chat, "complete")


def test_litellm_adapter_passes_provider_options_and_parses_content() -> None:
    from memoratum.llm import LiteLLMChat

    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "answer"}}]}

    fake = types.SimpleNamespace(completion=completion)
    chat = LiteLLMChat(
        model="provider/model", client=fake, api_base="http://local", api_key="secret"
    )
    assert chat.complete("system", "user") == "answer"
    assert calls == [
        {
            "model": "provider/model",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "user"},
            ],
            "temperature": 0,
            "timeout": 60.0,
            "num_retries": 3,
            "api_base": "http://local",
            "api_key": "secret",
        }
    ]


def test_litellm_adapter_disables_provider_telemetry(monkeypatch) -> None:
    from memoratum.llm import LiteLLMChat

    fake = types.SimpleNamespace(completion=lambda **_: {}, telemetry=True)
    monkeypatch.setitem(sys.modules, "litellm", fake)
    LiteLLMChat(model="provider/model")._load_client()
    assert fake.telemetry is False


def test_litellm_factory_falls_back_to_openai_compatible_when_optional_module_is_missing(
    monkeypatch,
) -> None:
    from memoratum.llm import OpenAICompatibleChat, build_chat

    monkeypatch.setitem(sys.modules, "litellm", None)
    chat = build_chat(
        "litellm",
        endpoint="http://local/v1",
        model="m",
        api_key="",
    )
    assert isinstance(chat, OpenAICompatibleChat)


def test_openai_provider_without_endpoint_stays_disabled() -> None:
    from memoratum.llm import build_chat

    assert build_chat("openai", endpoint="", model="m") is None


def test_unknown_llm_provider_is_rejected() -> None:
    import pytest

    from memoratum.llm import build_chat

    with pytest.raises(ValueError, match="unknown LLM provider"):
        build_chat("mystery", endpoint="http://local/v1", model="m")
