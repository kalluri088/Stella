"""Ollama implementation of the LLM client interface via its OpenAI-compatible API."""

import http.client
import json
import urllib.parse
from typing import Any

from stella.llm import (
    CancelCheck,
    LLMResponse,
    LLMToolCall,
    LLMToolDefinition,
    MessageInput,
    ProviderRequestCancelled,
    ToolUseMode,
    run_cancellable,
)
from stella.openai_client import OpenAILLMClient

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
NATIVE_REQUEST_TIMEOUT_SECONDS = 600
NATIVE_CANCEL_POLL_SECONDS = 0.25


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

    ``think`` maps to Ollama's top-level hybrid-reasoning switch (``None``
    never sends it; ``False`` is what report 35 target 1 wants for the
    qwen3 stack — measured on Ollama 0.33.3, non-thinking models accept
    ``think: false`` harmlessly). The inherited per-call-kind token budgets
    reach the native endpoint as ``options.num_predict``, with the
    decision budget on tool calls and the answer budget on plain chat.
    """

    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        api_key: str = "ollama",
        *,
        native: bool = False,
        num_ctx: int | None = None,
        answer_max_output_tokens: int | None = None,
        decision_max_output_tokens: int | None = None,
        think: bool | None = None,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            api_key=api_key,
            answer_max_output_tokens=answer_max_output_tokens,
            decision_max_output_tokens=decision_max_output_tokens,
        )
        self.native = native
        self.num_ctx = num_ctx
        self.think = think
        self.native_url = native_chat_url(base_url)

    def chat(
        self,
        messages: list[MessageInput],
        should_cancel: CancelCheck | None = None,
    ) -> str:
        if self.native:
            return (
                self._native_chat(
                    messages,
                    should_cancel=should_cancel,
                    budget=self.answer_max_output_tokens,
                ).get("content")
                or ""
            )
        return super().chat(messages, should_cancel=should_cancel)

    def chat_with_tools(
        self,
        messages: list[MessageInput],
        tools: list[LLMToolDefinition],
        tool_choice: ToolUseMode = ToolUseMode.AUTO,
        should_cancel: CancelCheck | None = None,
    ) -> LLMResponse:
        if self.native:
            message = self._native_chat(
                messages,
                tools,
                should_cancel=should_cancel,
                budget=self.decision_max_output_tokens,
            )
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
        if self.decision_max_output_tokens is not None:
            # Compatibility endpoint: max_tokens is a standard completions
            # field (unlike num_ctx, which 0.33.x ignores here).
            request["max_tokens"] = self.decision_max_output_tokens
        if tools:
            request["tools"] = [
                self._chat_tool_definition(tool) for tool in tools
            ]
            # Ollama's compatibility API does not enforce "required"; the
            # LLMBrain answer guard remains the trusted enforcement point.
            request["tool_choice"] = "auto"
        response = run_cancellable(
            lambda: self.client.chat.completions.create(**request),
            should_cancel,
        )
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
        should_cancel: CancelCheck | None = None,
        *,
        budget: int | None = None,
    ) -> dict[str, Any]:
        """POST one non-streaming request to Ollama's native /api/chat.

        ``budget`` is the caller-kind's output-token cap (decision calls
        pass theirs, plain chat passes the answer's): the caller knows
        the call kind, ``_native_chat`` stays transport-only.
        """

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
        if self.think is not None:
            request["think"] = self.think
        options: dict[str, object] = {}
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        if budget is not None:
            options["num_predict"] = budget
        if options:
            request["options"] = options
        body = json.dumps(request).encode("utf-8")
        url = urllib.parse.urlsplit(self.native_url)
        secure = url.scheme == "https"
        connection_class = (
            http.client.HTTPSConnection
            if secure
            else http.client.HTTPConnection
        )
        connection = connection_class(
            url.hostname or "127.0.0.1",
            url.port or (443 if secure else 80),
            timeout=NATIVE_REQUEST_TIMEOUT_SECONDS,
        )
        path = url.path or "/api/chat"
        if url.query:
            path = f"{path}?{url.query}"
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            if should_cancel is None:
                response = connection.getresponse()
            else:
                response = self._await_native_response(
                    connection, should_cancel
                )
            if response.status >= 400:
                raise OSError(
                    "Ollama native chat request failed: "
                    f"HTTP {response.status}"
                )
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        message = payload.get("message")
        return message if isinstance(message, dict) else {}

    @staticmethod
    def _await_native_response(
        connection: http.client.HTTPConnection,
        should_cancel: CancelCheck,
    ) -> http.client.HTTPResponse:
        """Wait for response headers, checking cancellation between polls.

        Short socket timeouts keep every read on the calling thread, so
        a cancel can abandon the wait from the thread that owns the
        connection (a cross-thread close would not wake a blocked
        read). Closing releases our side promptly; whether Ollama stops
        the abandoned generation is server behavior we do not rely on.
        """

        sock = connection.sock
        if sock is None:  # request() always connects first
            return connection.getresponse()
        sock.settimeout(NATIVE_CANCEL_POLL_SECONDS)
        try:
            while True:
                try:
                    return connection.getresponse()
                except TimeoutError:
                    if should_cancel():
                        raise ProviderRequestCancelled(
                            "the Ollama request was cancelled "
                            "while in flight"
                        ) from None
        finally:
            if connection.sock is not None:
                connection.sock.settimeout(NATIVE_REQUEST_TIMEOUT_SECONDS)

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
