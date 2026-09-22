"""Ollama implementation of the LLM client interface via its OpenAI-compatible API."""

import json
from typing import Any

from stella.llm import (
    LLMResponse,
    LLMToolCall,
    LLMToolDefinition,
    MessageInput,
    ToolUseMode,
)
from stella.openai_client import OpenAILLMClient

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"


class OllamaLLMClient(OpenAILLMClient):
    """Reuses chat completions for native tool calls because the Ollama
    compatibility endpoint does not serve the Responses API."""

    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        api_key: str = "ollama",
    ) -> None:
        super().__init__(model=model, base_url=base_url, api_key=api_key)

    def chat_with_tools(
        self,
        messages: list[MessageInput],
        tools: list[LLMToolDefinition],
        tool_choice: ToolUseMode = ToolUseMode.AUTO,
    ) -> LLMResponse:
        request: dict[str, object] = {
            "model": self.model,
            "messages": self._messages(messages),
        }
        if tools:
            request["tools"] = [
                self._chat_tool_definition(tool) for tool in tools
            ]
            # Ollama's compatibility API does not enforce "required"; the
            # LLMBrain answer guard remains the trusted enforcement point.
            request["tool_choice"] = "auto"
        response = self.client.chat.completions.create(**request)
        message = response.choices[0].message
        content = getattr(message, "content", None)
        return LLMResponse(
            content=content or None,
            tool_calls=tuple(
                self._chat_tool_call(call)
                for call in getattr(message, "tool_calls", None) or []
            ),
        )

    @staticmethod
    def _chat_tool_definition(tool: LLMToolDefinition) -> dict[str, object]:
        properties = {
            name: {"type": "string", "description": str(description)}
            for name, description in tool.arguments.items()
        }
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
            },
        }

    @staticmethod
    def _chat_tool_call(call: Any) -> LLMToolCall:
        function = getattr(call, "function", call)
        raw_arguments = getattr(function, "arguments", "{}")
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        return LLMToolCall(
            name=str(getattr(function, "name", "")),
            arguments=arguments,
        )
