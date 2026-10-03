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

    def stream_chat(
        self,
        messages: list[MessageInput],
        on_delta,
        should_cancel: CancelCheck | None = None,
    ) -> str | None:
        # Only the native endpoint can stream; the compatibility endpoint
        # and every hosted preset keep answering through ``chat``, so a
        # caller that offered a delta callback simply gets ``None`` back
        # and falls to its ordinary path.
        if not self.native:
            return None
        return self._native_stream_chat(
            messages,
            on_delta,
            should_cancel=should_cancel,
        )

    def _native_request(
        self,
        messages: list[MessageInput],
        tools: list[LLMToolDefinition] | None = None,
        *,
        budget: int | None = None,
        stream: bool,
    ) -> dict[str, object]:
        """Build one /api/chat body; ``stream`` is its only variable byte.

        The streaming answer and the ordinary answer are the same request
        with the same model, messages, tool set, ``think`` switch,
        ``num_ctx`` and ``num_predict`` — the single difference is the
        ``stream`` flag — which is what lets a streamed reply claim the
        model behaved identically to a non-streamed one.
        """

        request: dict[str, object] = {
            "model": self.model,
            "messages": self._messages(messages),
            "stream": stream,
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
        return request

    def _native_connection(self) -> http.client.HTTPConnection:
        """Open a connection to the native endpoint with a long timeout."""

        url = urllib.parse.urlsplit(self.native_url)
        secure = url.scheme == "https"
        connection_class = (
            http.client.HTTPSConnection
            if secure
            else http.client.HTTPConnection
        )
        return connection_class(
            url.hostname or "127.0.0.1",
            url.port or (443 if secure else 80),
            timeout=NATIVE_REQUEST_TIMEOUT_SECONDS,
        )

    def _native_send(
        self,
        connection: http.client.HTTPConnection,
        body: bytes,
        should_cancel: CancelCheck | None,
    ) -> http.client.HTTPResponse:
        """POST ``body``, return the response with its status checked.

        Leaves the connection open for the caller to drain and close; a
        non-2xx is reported as the same :class:`OSError` either way, so a
        streamed answer and a buffered one fail alike.
        """

        url = urllib.parse.urlsplit(self.native_url)
        path = url.path or "/api/chat"
        if url.query:
            path = f"{path}?{url.query}"
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = (
            connection.getresponse()
            if should_cancel is None
            else self._await_native_response(connection, should_cancel)
        )
        if response.status >= 400:
            status = response.status
            raise OSError(
                f"Ollama native chat request failed: HTTP {status}"
            )
        return response

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

        body = json.dumps(
            self._native_request(messages, tools, budget=budget, stream=False)
        ).encode("utf-8")
        connection = self._native_connection()
        try:
            response = self._native_send(connection, body, should_cancel)
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        message = payload.get("message")
        return message if isinstance(message, dict) else {}

    def _native_stream_chat(
        self,
        messages: list[MessageInput],
        on_delta,
        should_cancel: CancelCheck | None = None,
    ) -> str:
        """Stream one answer from /api/chat, feeding ``on_delta`` text.

        Reads the NDJSON body line by line on the calling thread and
        forwards only each ``message.content`` — thinking and any
        ``tool_calls`` delta are ignored, because a streamed reply is
        answer text only and decisions never stream. Returns the whole
        accumulated answer (the same string ``chat`` would have), not the
        trailing ``done`` payload, and raises :class:`OSError` on a
        malformed or truncated line so a partial answer is never
        fabricated into a shorter one.
        """

        body = json.dumps(
            self._native_request(
                messages,
                None,
                budget=self.answer_max_output_tokens,
                stream=True,
            )
        ).encode("utf-8")
        connection = self._native_connection()
        try:
            response = self._native_send(connection, body, should_cancel)
            return self._drain_native_stream(
                response, connection, on_delta, should_cancel
            )
        finally:
            connection.close()

    def _drain_native_stream(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        on_delta,
        should_cancel: CancelCheck | None,
    ) -> str:
        """Consume an open streamed response into one answer string.

        Cancellation has to be able to abort mid-generation, so while a
        ``should_cancel`` is present every read carries a short socket
        timeout on the owning thread: a stall becomes a cancel check, not
        a blocked wait (a cross-thread close would not wake a blocked
        read). The long request timeout is always restored afterwards.
        """

        polling = should_cancel is not None and connection.sock is not None
        if polling:
            connection.sock.settimeout(NATIVE_CANCEL_POLL_SECONDS)
        parts: list[str] = []
        try:
            while True:
                try:
                    line = response.readline()
                except TimeoutError:
                    if should_cancel is not None and should_cancel():
                        raise ProviderRequestCancelled(
                            "the Ollama stream was cancelled while "
                            "a reply was in flight"
                        ) from None
                    continue
                if not line:
                    break
                try:
                    chunk = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise OSError(
                        "Ollama native chat stream returned malformed data"
                    ) from exc
                if isinstance(chunk, dict) and chunk.get("error"):
                    raise OSError(
                        "Ollama native chat request failed: "
                        f"{chunk['error']}"
                    )
                message = (
                    chunk.get("message") if isinstance(chunk, dict) else None
                )
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        parts.append(content)
                        on_delta(content)
                if isinstance(chunk, dict) and chunk.get("done"):
                    return "".join(parts)
        finally:
            if polling and connection.sock is not None:
                connection.sock.settimeout(NATIVE_REQUEST_TIMEOUT_SECONDS)
        # End-of-stream without a done marker: the answer was truncated,
        # so report rather than return a silently shortened reply.
        raise OSError("Ollama native chat stream ended before completion")

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
