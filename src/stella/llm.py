"""Minimal language-model client abstractions."""

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import cast


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


CancelCheck = Callable[[], bool]


class ProviderRequestCancelled(RuntimeError):
    """The application cancelled while a provider request was in flight."""


def run_cancellable[T](
    request: Callable[[], T],
    should_cancel: CancelCheck | None,
    *,
    poll_seconds: float = 0.25,
    on_cancel: Callable[[], None] | None = None,
) -> T:
    """Run one blocking provider call so a cancel returns promptly.

    With no check this is a plain call: identical threading, identical
    timing, identical exceptions. With a check the request runs on a
    daemon helper thread while the caller polls; a cancel abandons the
    call (its result or error is discarded by that thread forever — it
    never touches application state) and raises
    :class:`ProviderRequestCancelled`. A result that lands in the same
    instant as a cancel is kept: the turn then ends at the next safe
    point with the answer in hand rather than throwing away work.
    ``on_cancel`` is a best-effort transport hint (close a socket) and
    its failure never masks the cancellation.
    """

    if should_cancel is None:
        return request()
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["result"] = request()
        except BaseException as error:  # noqa: BLE001 - re-raised on the
            # caller's thread; swallowing SystemExit here would otherwise
            # leave the polling loop waiting on a dead thread.
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    while True:
        thread.join(poll_seconds)
        if not thread.is_alive():
            break
        if should_cancel():
            if on_cancel is not None:
                try:
                    on_cancel()
                except OSError:
                    pass
            raise ProviderRequestCancelled(
                "the provider request was cancelled while in flight"
            )
    if "error" in box:
        raise box["error"]
    return cast(T, box.get("result"))


class LLMClient(ABC):
    """Interface for clients that turn chat messages into text.

    ``should_cancel`` is an application-owned check a client may consult
    while a provider request is in flight; honouring it is a
    best-effort service (a client that cannot abort reports through
    the normal safe points instead), and raising
    :class:`ProviderRequestCancelled` is the only acceptable way to
    abandon one.
    """

    @abstractmethod
    def chat(
        self,
        messages: list[MessageInput],
        should_cancel: CancelCheck | None = None,
    ) -> str:
        """Return a response for the supplied chat messages."""

    def chat_with_tools(
        self,
        messages: list[MessageInput],
        tools: list[LLMToolDefinition],
        tool_choice: ToolUseMode = ToolUseMode.AUTO,
        should_cancel: CancelCheck | None = None,
    ) -> LLMResponse:
        """Return text or native calls, falling back to the text protocol."""

        del tools, tool_choice
        if should_cancel is not None:
            return LLMResponse(
                content=self.chat(messages, should_cancel=should_cancel)
            )
        return LLMResponse(content=self.chat(messages))

    def stream_chat(
        self,
        messages: list[MessageInput],
        on_delta: Callable[[str], None],
        should_cancel: CancelCheck | None = None,
    ) -> str | None:
        """Answer like :meth:`chat`, feeding ``on_delta`` as text arrives.

        An optional capability, mirroring how :meth:`chat_with_tools`
        falls back to :meth:`chat`: the base implementation reports
        ``None`` — "I cannot stream this, the caller must answer its own
        way" — and a client that can stream returns the same complete
        text :meth:`chat` would have, having handed it to ``on_delta`` in
        pieces first. The caller uses the return value verbatim and never
        reassembles it from the deltas, so a client that streams and one
        that does not leave identical history behind.

        ``on_delta`` receives plain response text only — no
        provider-specific event crosses this boundary — and only ever the
        final answer, never a decision, a tool call or reasoning. A
        client that streams must still honour ``should_cancel`` the way
        :meth:`chat` does.
        """

        del messages, on_delta, should_cancel
        return None


class FakeLLMClient(LLMClient):
    """Deterministic client for tests."""

    def __init__(self, response: str = "fake response") -> None:
        self.response = response

    def chat(
        self,
        messages: list[MessageInput],
        should_cancel: CancelCheck | None = None,
    ) -> str:
        del should_cancel
        return self.response
