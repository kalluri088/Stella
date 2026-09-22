from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from stella.llm import LLMToolDefinition, Message, ToolUseMode
from stella.openai_client import OpenAILLMClient


def test_openai_client_configures_sdk_and_returns_response(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = MagicMock()
    response.choices[0].message.content = "hello"

    with patch("stella.openai_client.OpenAI") as openai:
        openai.return_value.chat.completions.create.return_value = response
        client = OpenAILLMClient(model="test-model", base_url="https://example.test")

        result = client.chat([Message(role="user", content="hi")])

    openai.assert_called_once_with(
        api_key="test-key",
        base_url="https://example.test",
    )
    openai.return_value.chat.completions.create.assert_called_once_with(
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result == "hello"


def test_openai_client_forwards_llm_brain_routing_contract(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    with patch("stella.openai_client.OpenAI") as openai:
        openai.return_value.chat.completions.create.return_value.choices[
            0
        ].message.content = '{"kind":"tool"}'
        client = OpenAILLMClient(model="test-model")
        system_prompt = (
            "Final routing check: if the user's requested result requires "
            "one of the tools listed immediately above, the decision MUST "
            "be kind=tool."
        )

        client.chat(
            [
                Message(role="system", content=system_prompt),
                Message(role="user", content='{"user_input":"What time is it?"}'),
            ]
        )

    request = openai.return_value.chat.completions.create.call_args.kwargs
    assert request["messages"] == [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": '{"user_input":"What time is it?"}'},
    ]


def test_openai_client_preserves_normal_conversation_messages(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    with patch("stella.openai_client.OpenAI") as openai:
        openai.return_value.chat.completions.create.return_value.choices[0].message.content = "hello"
        client = OpenAILLMClient(model="test-model")

        client.chat([{"role": "user", "content": "hi"}])

    openai.return_value.chat.completions.create.assert_called_once_with(
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
    )


def test_openai_client_uses_native_tools_and_normalizes_tool_call(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = MagicMock()
    response.output_text = None
    response.output = [
        SimpleNamespace(
            type="function_call",
            name="system_info",
            arguments='{"kind":"hostname"}',
        )
    ]

    with patch("stella.openai_client.OpenAI") as openai:
        openai.return_value.responses.create.return_value = response
        client = OpenAILLMClient(model="test-model")

        result = client.chat_with_tools(
            [Message(role="user", content="What is the hostname?")],
            [
                LLMToolDefinition(
                    name="system_info",
                    description="Reads host information.",
                    arguments={"kind": "hostname|platform|cpu"},
                )
            ],
        )

    request = openai.return_value.responses.create.call_args.kwargs
    assert request["model"] == "test-model"
    assert request["input"] == [
        {"role": "user", "content": "What is the hostname?"}
    ]
    assert request["tool_choice"] == "auto"
    assert request["tools"] == [
        {
            "type": "function",
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
            "strict": True,
        }
    ]
    assert result.content is None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "system_info"
    assert result.tool_calls[0].arguments == {"kind": "hostname"}


def test_openai_client_maps_required_tool_choice(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = MagicMock(output=[], output_text='{"kind":"ask"}')

    with patch("stella.openai_client.OpenAI") as openai:
        openai.return_value.responses.create.return_value = response
        client = OpenAILLMClient(model="test-model")
        client.chat_with_tools(
            [],
            [],
            tool_choice=ToolUseMode.REQUIRED,
        )

    request = openai.return_value.responses.create.call_args.kwargs
    assert request["tool_choice"] == "required"


def test_openai_client_malformed_native_arguments_fail_closed(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = MagicMock()
    response.output_text = None
    response.output = [
        SimpleNamespace(
            type="function_call", name="echo", arguments="not-json"
        )
    ]

    with patch("stella.openai_client.OpenAI") as openai:
        openai.return_value.responses.create.return_value = response
        client = OpenAILLMClient(model="test-model")
        result = client.chat_with_tools([], [])

    assert result.tool_calls[0].name == "echo"
    assert result.tool_calls[0].arguments == {}
