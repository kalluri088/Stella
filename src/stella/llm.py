"""Minimal language-model client abstractions."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class Message:
    """A single conversation message."""

    role: str
    content: str


MessageInput = Message | dict[str, str]


class ToolUseMode(str, Enum):
    """Provider-neutral policy for whether a model must call a tool."""

    AUTO = "auto"
    REQUIRED = "required"


@dataclass(frozen=True)
class LLMToolDefinition:
    """Provider-neutral description of one capability offered to an LLM."""

    name: str
    description: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class LLMToolCall:
    """Provider-neutral tool call returned by an LLM adapter."""

    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class LLMResponse:
    """Normalized LLM response supporting text and optional tool calls."""

    content: str | None = None
    tool_calls: tuple[LLMToolCall, ...] = ()


class LLMClient(ABC):
    """Interface for clients that turn chat messages into text."""

    @abstractmethod
    def chat(self, messages: list[MessageInput]) -> str:
        """Return a response for the supplied chat messages."""

    def chat_with_tools(
        self,
        messages: list[MessageInput],
        tools: list[LLMToolDefinition],
        tool_choice: ToolUseMode = ToolUseMode.AUTO,
    ) -> LLMResponse:
        """Return text or native calls, falling back to the text protocol."""

        del tools, tool_choice
        return LLMResponse(content=self.chat(messages))


class FakeLLMClient(LLMClient):
    """Deterministic client for tests."""

    def __init__(self, response: str = "fake response") -> None:
        self.response = response

    def chat(self, messages: list[MessageInput]) -> str:
        return self.response
