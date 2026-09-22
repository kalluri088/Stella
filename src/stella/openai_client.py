"""OpenAI-backed implementation of the LLM client interface."""

import json
import os
from typing import Any

from openai import OpenAI

from stella.llm import (
    LLMClient,
    LLMResponse,
    LLMToolCall,
    LLMToolDefinition,
    Message,
    MessageInput,
    ToolUseMode,
)


class OpenAILLMClient(LLMClient):
    """Minimal chat client backed by the official OpenAI SDK."""

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self.client = OpenAI(
            api_key=api_key if api_key is not None else os.environ["OPENAI_API_KEY"],
            base_url=base_url,
        )

    def chat(self, messages: list[MessageInput]) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": message.role, "content": message.content}
                if isinstance(message, Message)
                else message
                for message in messages
            ],
        )
        return response.choices[0].message.content or ""

    def chat_with_tools(
        self,
        messages: list[MessageInput],
        tools: list[LLMToolDefinition],
        tool_choice: ToolUseMode = ToolUseMode.AUTO,
    ) -> LLMResponse:
        """Use Responses API native calls with provider-neutral normalization."""

        response = self.client.responses.create(
            model=self.model,
            input=self._messages(messages),
            tools=[self._responses_tool_definition(tool) for tool in tools],
            tool_choice=tool_choice.value,
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
