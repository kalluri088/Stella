"""Provider-neutral, non-authoritative interaction tracing."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class InputReceivedEvent:
    """Bounded metadata about the context entering Stella."""

    user_input_chars: int
    conversation_messages: int
    input_parts: tuple[str, ...]
    existing_observations: int


@dataclass(frozen=True)
class MemoryRetrievedEvent:
    """Metadata about memory retrieval without storing memory content."""

    count: int
    content_lengths: tuple[int, ...]


@dataclass(frozen=True)
class DecisionEvent:
    """A redacted Brain decision summary."""

    kind: str
    capability: str | None
    argument_keys: tuple[str, ...]
    content_chars: int
    memory_write_proposed: bool


@dataclass(frozen=True)
class ApprovalEvent:
    """The application approval outcome for one dangerous attempt."""

    capability: str | None
    approved: bool | None


@dataclass(frozen=True)
class ToolResultEvent:
    """A redacted tool dispatch result."""

    capability: str | None
    argument_keys: tuple[str, ...]
    success: bool
    output_chars: int


@dataclass(frozen=True)
class FinalResponseEvent:
    """Metadata about the response returned to the caller."""

    decision_kind: str
    response_present: bool
    response_chars: int
    needs_more_information: bool
    max_steps_reached: bool


@dataclass(frozen=True)
class MemoryWriteEvent:
    """The outcome of the interaction's explicit memory-write proposal."""

    proposed: bool
    written: bool
    content_chars: int


@dataclass(frozen=True)
class MemoryActionEvent:
    """A memory list/update/delete performed by a trusted memory tool."""

    action: str
    count: int
    memory_id: int | None = None


@dataclass(frozen=True)
class ActionReceiptEvent:
    """A bounded receipt for one mutation attempt and its verification."""

    capability: str | None
    action: str
    status: str
    size_bytes: int | None = None


TraceEvent = (
    InputReceivedEvent
    | MemoryRetrievedEvent
    | DecisionEvent
    | ApprovalEvent
    | ToolResultEvent
    | FinalResponseEvent
    | MemoryWriteEvent
    | MemoryActionEvent
    | ActionReceiptEvent
)


@dataclass
class InteractionTrace:
    """An ordered, metadata-only trace that cannot control Stella."""

    interaction_id: str = "interaction"
    _events: list[TraceEvent] = field(default_factory=list, repr=False)

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        """Return an immutable view of the ordered trace."""

        return tuple(self._events)

    def record(self, event: TraceEvent) -> None:
        """Append one trusted runtime observation to the trace."""

        self._events.append(event)

    def record_memory_write(self, event: MemoryWriteEvent) -> None:
        """Record the single memory outcome without duplicating it."""

        if not any(isinstance(item, MemoryWriteEvent) for item in self._events):
            self.record(event)
