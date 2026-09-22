"""Ollama implementation of the LLM client interface via its OpenAI-compatible API."""

import json
import urllib.request
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
NATIVE_REQUEST_TIMEOUT_SECONDS = 600


def native_chat_url(base_url: str) -> str:
    """Derive the native /api/chat endpoint from a compatibility base URL."""

    trimmed = base_url.rstrip("/")
    return f"{trimmed.removesuffix('/v1')}/api/chat"


class OllamaLLMClient(OpenAILLMClient):
    """Reuses chat completions for native tool calls because the Ollama
    compatibility endpoint does not serve the Responses API.

    ``native=True`` switches to Ollama's own /api/chat endpoint, which is
    the only way to set per-request options such as ``num_ctx``: the
    compatibility endpoint silently ignores them on Ollama 0.33.x.
    """

    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        api_key: str = "ollama",
        *,
        native: bool = False,
        num_ctx: int | None = None,
    ) -> None:
        super().__init__(model=model, base_url=base_url, api_key=api_key)
        self.native = native
        self.num_ctx = num_ctx
        self.native_url = native_chat_url(base_url)

    def chat(self, messages: list[MessageInput]) -> str:
        if self.native:
            return self._native_chat(messages).get("content") or ""
        return super().chat(messages)

    def chat_with_tools(
        self,
        messages: list[MessageInput],
        tools: list[LLMToolDefinition],
        tool_choice: ToolUseMode = ToolUseMode.AUTO,
    ) -> LLMResponse:
        if self.native:
            message = self._native_chat(messages, tools)
            return LLMResponse(
                content=message.get("content") or None,
                tool_calls=tuple(
                    self._native_tool_call(call)
                    for call in message.get("tool_calls") or []
                ),
            )
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

    def _native_chat(
        self,
        messages: list[MessageInput],
        tools: list[LLMToolDefinition] | None = None,
    ) -> dict[str, Any]:
        """POST one non-streaming request to Ollama's native /api/chat."""

        request: dict[str, object] = {
            "model": self.model,
            "messages": self._messages(messages),
            "stream": False,
        }
        if tools:
            request["tools"] = [
                self._chat_tool_definition(tool) for tool in tools
            ]
            request["tool_choice"] = "auto"
        if self.num_ctx is not None:
            request["options"] = {"num_ctx": self.num_ctx}
        body = json.dumps(request).encode("utf-8")
        http_request = urllib.request.Request(
            self.native_url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(
            http_request, timeout=NATIVE_REQUEST_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        message = payload.get("message")
        return message if isinstance(message, dict) else {}

    @staticmethod
    def _native_tool_call(call: Any) -> LLMToolCall:
        function = call.get("function", {}) if isinstance(call, dict) else {}
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        return LLMToolCall(
            name=str(function.get("name", "")),
            arguments=arguments,
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
