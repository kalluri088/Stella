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


@dataclass(frozen=True)
class UsageSnapshot:
    """What a client has observed of its provider's token counts.

    ``requests`` counts completed calls, so the two token totals are an
    average only in the arithmetic sense: a provider that reports nothing
    contributes a request and no tokens, which is honest about the gap
    rather than guessing a count. ``largest_prompt`` is the single biggest
    prompt this session sent — the number that says whether the context
    window is being clipped.
    """

    requests: int
    prompt_tokens: int
    completion_tokens: int
    largest_prompt: int


class UsageRecorder:
    """A session-scoped tally of the token counts a provider reports.

    Nothing is persisted and nothing is estimated: only the numbers the
    provider itself returned for a finished request are added. A cancelled
    or failed call records nothing, so the totals describe work the model
    actually did, not work the application threw away. The lock exists
    because the UI thread reads while the worker thread writes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._largest_prompt = 0

    def record(
        self, prompt_tokens: int | None, completion_tokens: int | None
    ) -> None:
        """Tally one completed request; a missing count adds nothing."""

        with self._lock:
            self._requests += 1
            if isinstance(prompt_tokens, int) and prompt_tokens >= 0:
                self._prompt_tokens += prompt_tokens
                self._largest_prompt = max(
                    self._largest_prompt, prompt_tokens
                )
            if (
                isinstance(completion_tokens, int)
                and completion_tokens >= 0
            ):
                self._completion_tokens += completion_tokens

    def snapshot(self) -> UsageSnapshot:
        with self._lock:
            return UsageSnapshot(
                requests=self._requests,
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._completion_tokens,
                largest_prompt=self._largest_prompt,
            )


def openai_usage_counts(response: object) -> tuple[int | None, int | None]:
    """(prompt, completion) token counts from an OpenAI-shaped response."""

    usage = getattr(response, "usage", None)
    return (
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
    )


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

    ``usage`` is an optional :class:`UsageRecorder` the client tallies
    into. It defaults to None on the class, so a client that never
    attaches one — and a provider that reports no counts — simply
    reports nothing rather than an invented number.
    """

    usage: UsageRecorder | None = None

    def _note_usage(
        self, prompt_tokens: int | None, completion_tokens: int | None
    ) -> None:
        """Tally one finished request when this client carries a recorder."""

        if self.usage is not None:
            self.usage.record(prompt_tokens, completion_tokens)

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
