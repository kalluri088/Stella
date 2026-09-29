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
    run_cancellable,
)


class OpenAILLMClient(LLMClient):
    """Minimal chat client backed by the official OpenAI SDK.

    The two output-token budgets are per-call-kind decode caps (report 35
    target 1): ``answer_max_output_tokens`` bounds plain ``chat`` text and
    ``decision_max_output_tokens`` bounds tool-call decisions. A budget of
    ``None`` (the default) sends no cap at all, so an unconfigured client
    behaves exactly as before.
    """

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        answer_max_output_tokens: int | None = None,
        decision_max_output_tokens: int | None = None,
    ) -> None:
        self.model = model
        self.answer_max_output_tokens = answer_max_output_tokens
        self.decision_max_output_tokens = decision_max_output_tokens
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
        return response.choices[0].message.content or ""

    def chat_with_tools(
        self,
        messages: list[MessageInput],
        tools: list[LLMToolDefinition],
        tool_choice: ToolUseMode = ToolUseMode.AUTO,
        should_cancel: CancelCheck | None = None,
    ) -> LLMResponse:
        """Use Responses API native calls with provider-neutral normalization."""

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
        calls = tuple(
            self._responses_tool_call(item)
            for item in (getattr(response, "output", None) or [])
            if getattr(item, "type", None) == "function_call"
        )
        return LLMResponse(
            content=getattr(response, "output_text", None) or None,
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
    def _responses_tool_definition(tool: LLMToolDefinition) -> dict[str, object]:
        properties = {
            name: {"type": "string", "description": str(description)}
            for name, description in tool.arguments.items()
        }
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
            "strict": True,
        }

    @staticmethod
    def _responses_tool_call(tool_call: Any) -> LLMToolCall:
        raw_arguments = getattr(tool_call, "arguments", "{}")
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        return LLMToolCall(
            name=str(getattr(tool_call, "name", "")),
            arguments=arguments,
        )
