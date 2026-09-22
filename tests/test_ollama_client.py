from types import SimpleNamespace
from unittest.mock import patch

import pytest

from stella.cli import create_stella_from_environment
from stella.llm import LLMToolDefinition, Message, ToolUseMode
from stella.ollama_client import DEFAULT_OLLAMA_BASE_URL, OllamaLLMClient


def make_message(response_client, message):
    response_client.return_value.chat.completions.create.return_value = (
        SimpleNamespace(choices=[SimpleNamespace(message=message)])
    )


def test_ollama_client_uses_local_compatibility_defaults_without_openai_key(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with patch("stella.openai_client.OpenAI") as openai:
        client = OllamaLLMClient(model="qwen3:4b")

    openai.assert_called_once_with(
        api_key="ollama",
        base_url=DEFAULT_OLLAMA_BASE_URL,
    )
    assert client.model == "qwen3:4b"


def test_ollama_client_chat_uses_chat_completions() -> None:
    with patch("stella.openai_client.OpenAI") as openai:
        make_message(
            openai, SimpleNamespace(content="hello", tool_calls=None)
        )
        client = OllamaLLMClient(model="qwen3:4b")

        result = client.chat([Message(role="user", content="hi")])

    openai.return_value.chat.completions.create.assert_called_once_with(
        model="qwen3:4b",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result == "hello"


def test_ollama_client_normalizes_native_tool_call() -> None:
    with patch("stella.openai_client.OpenAI") as openai:
        make_message(
            openai,
            SimpleNamespace(
                content=None,
                tool_calls=[
                    SimpleNamespace(
                        function=SimpleNamespace(
                            name="system_info",
                            arguments='{"kind":"hostname"}',
                        )
                    )
                ],
            ),
        )
        client = OllamaLLMClient(model="qwen3:4b")

        result = client.chat_with_tools(
            [Message(role="user", content="What is the hostname?")],
            [
                LLMToolDefinition(
                    name="system_info",
                    description="Reads host information.",
                    arguments={"kind": "hostname|platform|cpu"},
                )
            ],
            tool_choice=ToolUseMode.REQUIRED,
        )

    request = openai.return_value.chat.completions.create.call_args.kwargs
    assert request["messages"] == [
        {"role": "user", "content": "What is the hostname?"}
    ]
    assert request["tool_choice"] == "auto"
    assert request["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "system_info",
                "description": "Reads host information.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "description": "hostname|platform|cpu",
                        }
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    assert result.content is None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "system_info"
    assert result.tool_calls[0].arguments == {"kind": "hostname"}


def test_ollama_client_text_response_keeps_decision_protocol() -> None:
    with patch("stella.openai_client.OpenAI") as openai:
        make_message(
            openai,
            SimpleNamespace(content='{"kind":"answer","content":"hi"}'),
        )
        client = OllamaLLMClient(model="qwen3:4b")

        result = client.chat_with_tools(
            [], [LLMToolDefinition(name="echo", description="", arguments={})]
        )

    assert result.content == '{"kind":"answer","content":"hi"}'
    assert result.tool_calls == ()


def test_ollama_client_malformed_tool_arguments_fail_closed() -> None:
    with patch("stella.openai_client.OpenAI") as openai:
        make_message(
            openai,
            SimpleNamespace(
                content=None,
                tool_calls=[
                    SimpleNamespace(
                        function=SimpleNamespace(
                            name="echo", arguments="not-json"
                        )
                    )
                ],
            ),
        )
        client = OllamaLLMClient(model="qwen3:4b")

        result = client.chat_with_tools(
            [], [LLMToolDefinition(name="echo", description="", arguments={})]
        )

    assert result.tool_calls[0].name == "echo"
    assert result.tool_calls[0].arguments == {}


def test_ollama_client_omits_tool_protocol_when_no_tools() -> None:
    with patch("stella.openai_client.OpenAI") as openai:
        make_message(openai, SimpleNamespace(content="plain", tool_calls=None))
        client = OllamaLLMClient(model="qwen3:4b")

        result = client.chat_with_tools([], [])

    request = openai.return_value.chat.completions.create.call_args.kwargs
    assert "tools" not in request
    assert "tool_choice" not in request
    assert result.content == "plain"


def test_cli_selects_ollama_provider(monkeypatch) -> None:
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("STELLA_MODEL", "qwen3:4b")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with (
        patch("stella.cli.SQLiteMemory"),
        patch("stella.cli.OllamaLLMClient") as ollama_client,
    ):
        stella = create_stella_from_environment()

    ollama_client.assert_called_once_with(
        model="qwen3:4b",
        base_url=DEFAULT_OLLAMA_BASE_URL,
        native=True,
        num_ctx=4096,
    )
    assert stella.brain.llm is ollama_client.return_value


def test_cli_honours_custom_ollama_base_url(monkeypatch) -> None:
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("STELLA_MODEL", "qwen3:4b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://192.0.2.1:11434/v1")

    with (
        patch("stella.cli.SQLiteMemory"),
        patch("stella.cli.OllamaLLMClient") as ollama_client,
    ):
        create_stella_from_environment()

    ollama_client.assert_called_once_with(
        model="qwen3:4b",
        base_url="http://192.0.2.1:11434/v1",
        native=True,
        num_ctx=4096,
    )


def test_cli_rejects_unknown_llm_provider(monkeypatch) -> None:
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("STELLA_MODEL", "some-model")

    with pytest.raises(SystemExit, match="STELLA_LLM_PROVIDER"):
        create_stella_from_environment()
