"""Tests for the opt-in native /api/chat mode of OllamaLLMClient."""

import json
from types import SimpleNamespace
from typing import Self
from unittest.mock import patch

import pytest

from stella.llm import LLMToolDefinition, Message
from stella.ollama_client import OllamaLLMClient, native_chat_url


class FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def urlopen_stub():
    """Capture native requests and replay a canned /api/chat reply."""

    captured: list[SimpleNamespace] = []

    def install(message: dict) -> None:
        def urlopen(request, timeout=None):
            captured.append(
                SimpleNamespace(request=request, timeout=timeout)
            )
            body = json.dumps({"message": message}).encode("utf-8")
            return FakeHTTPResponse(body)

        patcher = patch(
            "stella.ollama_client.urllib.request.urlopen", urlopen
        )
        patcher.start()
        install._patchers.append(patcher)  # type: ignore[attr-defined]

    install.requests = captured  # type: ignore[attr-defined]
    install._patchers = []  # type: ignore[attr-defined]
    yield install
    for patcher in install._patchers:  # type: ignore[attr-defined]
        patcher.stop()


def sent_payload(request) -> dict:
    return json.loads(request.data.decode("utf-8"))


def make_client(**kwargs) -> OllamaLLMClient:
    with patch("stella.openai_client.OpenAI"):
        return OllamaLLMClient(model="qwen3:4b", **kwargs)


def test_native_chat_url_strips_compatibility_suffix() -> None:
    assert (
        native_chat_url("http://127.0.0.1:11434/v1")
        == "http://127.0.0.1:11434/api/chat"
    )
    assert (
        native_chat_url("http://host:11434/v1/")
        == "http://host:11434/api/chat"
    )


def test_native_chat_sends_num_ctx_option(urlopen_stub) -> None:
    urlopen_stub({"role": "assistant", "content": "4", "tool_calls": []})
    client = make_client(native=True, num_ctx=4096)

    result = client.chat([Message(role="user", content="What is 2+2?")])

    assert result == "4"
    request = urlopen_stub.requests[-1].request
    assert request.full_url == "http://127.0.0.1:11434/api/chat"
    payload = sent_payload(request)
    assert payload["options"] == {"num_ctx": 4096}
    assert payload["stream"] is False
    assert payload["messages"] == [
        {"role": "user", "content": "What is 2+2?"}
    ]


def test_native_chat_with_tools_sends_num_ctx_and_parses_call(
    urlopen_stub,
) -> None:
    urlopen_stub(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "system_info",
                        "arguments": {"kind": "hostname"},
                    }
                }
            ],
        }
    )
    client = make_client(native=True, num_ctx=4096)

    result = client.chat_with_tools(
        [Message(role="user", content="hostname?")],
        [
            LLMToolDefinition(
                name="system_info",
                description="Reads host information.",
                arguments={"kind": "hostname|platform|cpu"},
            )
        ],
    )

    payload = sent_payload(urlopen_stub.requests[-1].request)
    assert payload["options"] == {"num_ctx": 4096}
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["function"]["name"] == "system_info"
    assert result.content is None
    assert result.tool_calls[0].name == "system_info"
    assert result.tool_calls[0].arguments == {"kind": "hostname"}


def test_native_tool_arguments_may_arrive_as_json_string(
    urlopen_stub,
) -> None:
    urlopen_stub(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "echo",
                        "arguments": '{"text":"hi"}',
                    }
                }
            ],
        }
    )
    client = make_client(native=True, num_ctx=4096)

    result = client.chat_with_tools(
        [], [LLMToolDefinition(name="echo", description="", arguments={})]
    )

    assert result.tool_calls[0].arguments == {"text": "hi"}


def test_native_malformed_tool_arguments_fail_closed(
    urlopen_stub,
) -> None:
    urlopen_stub(
        {"role": "assistant", "tool_calls": [{"function": {"name": "echo", "arguments": "not-json"}}]}
    )
    client = make_client(native=True)

    result = client.chat_with_tools(
        [], [LLMToolDefinition(name="echo", description="", arguments={})]
    )

    assert result.tool_calls[0].arguments == {}


def test_native_omits_options_without_num_ctx(urlopen_stub) -> None:
    urlopen_stub({"role": "assistant", "content": "ok"})
    client = make_client(native=True)

    client.chat([Message(role="user", content="hi")])

    payload = sent_payload(urlopen_stub.requests[-1].request)
    assert "options" not in payload


def test_compat_mode_remains_default_and_untouched() -> None:
    with patch("stella.openai_client.OpenAI") as openai:
        openai.return_value.chat.completions.create.return_value = (
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="hello", tool_calls=None
                        )
                    )
                ]
            )
        )
        client = OllamaLLMClient(model="qwen3:4b")

        assert client.native is False
        result = client.chat([Message(role="user", content="hi")])

    openai.return_value.chat.completions.create.assert_called_once_with(
        model="qwen3:4b",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result == "hello"
