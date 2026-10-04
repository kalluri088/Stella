"""OpenAI-backed implementation of the LLM client interface."""

import json
import os
from typing import Any

from openai import OpenAI

from stella.llm import (
    CancelCheck,
    LLMClient,
    LLMResponse,
    LLMToolCall,
    LLMToolDefinition,
    Message,
    MessageInput,
    ToolUseMode,
    UsageRecorder,
    openai_usage_counts,
    run_cancellable,
)


class OpenAILLMClient(LLMClient):
    """Minimal chat client backed by the official OpenAI SDK.

    The two output-token budgets are per-call-kind decode caps (report 35
    target 1): ``answer_max_output_tokens`` bounds plain ``chat`` text and
    ``decision_max_output_tokens`` bounds tool-call decisions — on either
    tool dialect. A budget of ``None`` (the default) sends no cap at all,
    so an unconfigured client behaves exactly as before.

    ``tool_dialect`` records which tool-calling API the endpoint answers:
    only OpenAI itself serves the Responses API; every other
    OpenAI-compatible provider (Claude, Grok, Groq, OpenRouter, Gemini,
    an arbitrary gateway) speaks ``chat/completions``. Plain ``chat``
    works on both, so only the tool path branches.
    """

    TOOL_DIALECTS = ("responses", "chat")

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        tool_dialect: str = "responses",
        answer_max_output_tokens: int | None = None,
        decision_max_output_tokens: int | None = None,
        usage: UsageRecorder | None = None,
    ) -> None:
        if tool_dialect not in self.TOOL_DIALECTS:
            raise ValueError("tool_dialect must be 'responses' or 'chat'")
        self.model = model
        self.tool_dialect = tool_dialect
        self.answer_max_output_tokens = answer_max_output_tokens
        self.decision_max_output_tokens = decision_max_output_tokens
        self.usage = usage
        self.client = OpenAI(
            api_key=api_key if api_key is not None else os.environ["OPENAI_API_KEY"],
            base_url=base_url,
        )

    def chat(
        self,
        messages: list[MessageInput],
        should_cancel: CancelCheck | None = None,
    ) -> str:
        request: dict[str, object] = {
            "model": self.model,
            "messages": self._messages(messages),
        }
        if self.answer_max_output_tokens is not None:
            request["max_tokens"] = self.answer_max_output_tokens
        response = run_cancellable(
            lambda: self.client.chat.completions.create(**request),
            should_cancel,
        )
        self._note_usage(*openai_usage_counts(response))
        return response.choices[0].message.content or ""

    def chat_with_tools(
        self,
        messages: list[MessageInput],
        tools: list[LLMToolDefinition],
        tool_choice: ToolUseMode = ToolUseMode.AUTO,
        should_cancel: CancelCheck | None = None,
    ) -> LLMResponse:
        """Native tool calls with provider-neutral normalization."""

        if self.tool_dialect == "chat":
            return self._chat_completions_tools(
                messages, tools, tool_choice, should_cancel
            )
        request: dict[str, object] = {
            "model": self.model,
            "input": self._messages(messages),
            "tools": [
                self._responses_tool_definition(tool) for tool in tools
            ],
            "tool_choice": tool_choice.value,
        }
        if self.decision_max_output_tokens is not None:
            request["max_output_tokens"] = self.decision_max_output_tokens
        response = run_cancellable(
            lambda: self.client.responses.create(**request),
            should_cancel,
        )
        self._note_usage(*openai_usage_counts(response))
        calls = tuple(
            self._responses_tool_call(item)
            for item in (getattr(response, "output", None) or [])
            if getattr(item, "type", None) == "function_call"
        )
        return LLMResponse(
            content=getattr(response, "output_text", None) or None,
            tool_calls=calls,
        )

    def _chat_completions_tools(
        self,
        messages: list[MessageInput],
        tools: list[LLMToolDefinition],
        tool_choice: ToolUseMode,
        should_cancel: CancelCheck | None,
    ) -> LLMResponse:
        request: dict[str, object] = {
            "model": self.model,
            "messages": self._messages(messages),
            "tools": [
                self._chat_tool_definition(tool) for tool in tools
            ],
            "tool_choice": tool_choice.value,
        }
        if self.decision_max_output_tokens is not None:
            request["max_tokens"] = self.decision_max_output_tokens
        response = run_cancellable(
            lambda: self.client.chat.completions.create(**request),
            should_cancel,
        )
        self._note_usage(*openai_usage_counts(response))
        message = response.choices[0].message
        calls = tuple(
            self._chat_tool_call(call)
            for call in (getattr(message, "tool_calls", None) or [])
        )
        return LLMResponse(
            content=getattr(message, "content", None) or None,
            tool_calls=calls,
        )

    @staticmethod
    def _messages(messages: list[MessageInput]) -> list[dict[str, str]]:
        return [
            {"role": message.role, "content": message.content}
            if isinstance(message, Message)
            else message
            for message in messages
        ]

    @staticmethod
    def _parameters(tool: LLMToolDefinition) -> dict[str, object]:
        properties = {
            name: {"type": "string", "description": str(description)}
            for name, description in tool.arguments.items()
        }
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    @staticmethod
    def _responses_tool_definition(tool: LLMToolDefinition) -> dict[str, object]:
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": OpenAILLMClient._parameters(tool),
            "strict": True,
        }

    @staticmethod
    def _chat_tool_definition(tool: LLMToolDefinition) -> dict[str, object]:
        # No "strict" here: compatibility endpoints implement structured
        # outputs unevenly, and the runtime validates arguments anyway.
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": OpenAILLMClient._parameters(tool),
            },
        }

    @staticmethod
    def _parse_arguments(raw: object) -> dict[str, object]:
        try:
            arguments = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        return arguments

    @staticmethod
    def _responses_tool_call(tool_call: Any) -> LLMToolCall:
        return LLMToolCall(
            name=str(getattr(tool_call, "name", "")),
            arguments=OpenAILLMClient._parse_arguments(
                getattr(tool_call, "arguments", "{}")
            ),
        )

    @staticmethod
    def _chat_tool_call(tool_call: Any) -> LLMToolCall:
        function = getattr(tool_call, "function", None)
        return LLMToolCall(
            name=str(getattr(function, "name", "")),
            arguments=OpenAILLMClient._parse_arguments(
                getattr(function, "arguments", "{}")
            ),
        )
