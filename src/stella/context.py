"""Minimal response context and interface-neutral input representation."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from stella.llm import Message
from stella.memory import MemoryItem, relevance_score

MAX_CONVERSATION_HISTORY = 20
MAX_RETRIEVED_MEMORIES = 5
# B3 retrieval window: how many best-scoring candidates recall may carry
# at all. The brain-facing cap stays MAX_RETRIEVED_MEMORIES; this bounds
# the stage before it (fusion, provenance, the conversation-query merge)
# so a large store cannot make every turn scan an unbounded list.
MAX_RECALL_WINDOW = 16
MAX_TOOL_OBSERVATIONS = 8
# How many unhandled proposals one report may carry. A reply is
# model-shaped data, so the list of what it asked for is bounded like the
# list of what ran; the count beside it stays the true total, so being
# capped never means being hidden (#264).
MAX_DROPPED_PROPOSALS = 4
MAX_TOOL_OUTPUT_CHARS = 4000
MAX_INPUT_PARTS = 8
MAX_INPUT_CONTENT_CHARS = 4000
MAX_INPUT_REFERENCE_CHARS = 512
MAX_INPUT_METADATA_ITEMS = 8
MAX_INPUT_METADATA_CHARS = 256


class InputModality(str, Enum):
    """Interface-neutral modalities supported by the input contract."""

    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"
    ENVIRONMENT = "environment"


class InputProvenance(str, Enum):
    """Where an input part came from; provenance never grants authority."""

    USER = "user"
    TOOL = "tool"
    MODEL = "model"
    ENVIRONMENT = "environment"


@dataclass(frozen=True)
class InputPart:
    """One bounded input observation or reference supplied to the core."""

    modality: InputModality
    provenance: InputProvenance
    content: str | None = None
    reference: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.modality, InputModality):
            raise TypeError("modality must be an InputModality")
        if not isinstance(self.provenance, InputProvenance):
            raise TypeError("provenance must be an InputProvenance")
        if self.content is None and self.reference is None:
            raise ValueError("an input part needs content or a reference")
        if self.content is not None:
            if not isinstance(self.content, str):
                raise TypeError("content must be a string")
            if len(self.content) > MAX_INPUT_CONTENT_CHARS:
                raise ValueError("content exceeds the input bound")
        if self.reference is not None:
            if not isinstance(self.reference, str):
                raise TypeError("reference must be a string")
            if len(self.reference) > MAX_INPUT_REFERENCE_CHARS:
                raise ValueError("reference exceeds the input bound")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a string mapping")
        if len(self.metadata) > MAX_INPUT_METADATA_ITEMS:
            raise ValueError("metadata exceeds the input bound")
        normalized_metadata: dict[str, str] = {}
        for key, value in self.metadata.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("metadata keys and values must be strings")
            if (
                len(key) > MAX_INPUT_METADATA_CHARS
                or len(value) > MAX_INPUT_METADATA_CHARS
            ):
                raise ValueError("metadata exceeds the input bound")
            normalized_metadata[key] = value
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(normalized_metadata),
        )

    @classmethod
    def text(
        cls,
        content: str,
        provenance: InputProvenance = InputProvenance.USER,
    ) -> "InputPart":
        """Create the compatibility representation for current text input."""

        return cls(InputModality.TEXT, provenance, content=content)

    def to_payload(self) -> dict[str, object]:
        """Return a bounded provider-neutral representation for a Brain."""

        return {
            "modality": self.modality.value,
            "provenance": self.provenance.value,
            "content": self.content,
            "reference": self.reference,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class InputEnvelope:
    """A bounded collection of interface-neutral input parts."""

    parts: tuple[InputPart, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", tuple(self.parts))
        if len(self.parts) > MAX_INPUT_PARTS:
            raise ValueError("input parts exceed the input bound")
        if any(not isinstance(part, InputPart) for part in self.parts):
            raise TypeError("parts must contain InputPart values")

    @classmethod
    def from_text(cls, content: str) -> "InputEnvelope":
        return cls((InputPart.text(content),))

    def to_payload(self) -> list[dict[str, object]]:
        return [part.to_payload() for part in self.parts]

    def carries_audio(self) -> bool:
        """Whether any part of this input arrived as speech.

        One question with two answers today — the runtime's spoken-turn flag
        and the brain's voice style note — and both must mean the same thing,
        because an answer that will be read aloud is the only answer that has
        to work out loud.
        """

        return any(part.modality is InputModality.AUDIO for part in self.parts)


@dataclass(frozen=True)
class ToolObservation:
    """A structured result from one trusted tool dispatch."""

    capability: str | None
    arguments: dict[str, object]
    success: bool
    output: str


@dataclass(frozen=True)
class RetrievalSource:
    """How one memory entered the context, reported as data.

    ``method`` is "keyword" (deterministic term overlap),
    "local-hash-embedding" (deterministic word/trigram similarity — a
    weaker hint, not an understanding of meaning), or "none" (not
    recorded). The score is on that method's own scale; methods are
    never compared against each other.
    """

    memory_id: int
    method: str
    score: int | float


@dataclass(frozen=True)
class DroppedProposal:
    """A capability the model asked for in the same reply as a kept one.

    One step dispatches one capability, so a reply carrying several calls
    cannot run them all. Keeping the first and losing the rest without a word
    is #264: the run answered "No files or folders in this workspace dir" from
    the call that did run, while the call that held the owner's answer —
    ``outline_search`` in the same reply — was nowhere in front of her or of the
    next decision. This is the report of what was not run.

    It is *not* an observation. Nothing executed, so there is no output; the
    only fact here is that a proposal went unhandled, and the model still has
    to decide whether it wants it.
    """

    capability: str
    arguments: dict[str, object]


@dataclass
class Context:
    """Information currently needed to prepare a response."""

    user_input: str
    conversation_history: list[Message] = field(default_factory=list)
    retrieved_memories: list[MemoryItem] = field(default_factory=list)
    tool_observations: list[ToolObservation] = field(default_factory=list)
    # Proposals the previous reply carried and this runtime did not dispatch.
    # Reported, never executed on the model's behalf.
    dropped_proposals: list[DroppedProposal] = field(default_factory=list)
    input_envelope: InputEnvelope | None = None
    # How each retrieved memory was found, keyed by memory id. Populated
    # by the runtime during recall; provenance is reported, never an
    # authority. Empty means "retrieval method not recorded".
    retrieval_sources: Mapping[int, "RetrievalSource"] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if self.input_envelope is None:
            self.input_envelope = InputEnvelope.from_text(self.user_input)


def select_conversation_history(history: list[Message]) -> list[Message]:
    """Keep the most recent conversation messages in stable order."""

    return list(history[-MAX_CONVERSATION_HISTORY:])


def select_retrieved_memories(
    memories: list[MemoryItem],
    user_input: str,
) -> list[MemoryItem]:
    """Keep the most relevant memories, preserving order for ties."""

    return sorted(
        memories,
        key=lambda item: relevance_score(item.content, user_input),
        reverse=True,
    )[:MAX_RETRIEVED_MEMORIES]


def limit_tool_output(output: str) -> str:
    if len(output) <= MAX_TOOL_OUTPUT_CHARS:
        return output
    suffix = "... [tool output truncated]"
    return output[: MAX_TOOL_OUTPUT_CHARS - len(suffix)] + suffix


def select_tool_observations(
    observations: list[ToolObservation],
) -> list[ToolObservation]:
    """Keep a bounded, deterministic set of observations for the Brain."""

    if not observations:
        return []

    selected_indices = {len(observations) - 1}
    for index in range(len(observations) - 1, -1, -1):
        if len(selected_indices) >= MAX_TOOL_OBSERVATIONS:
            break
        if not observations[index].success:
            selected_indices.add(index)
    for index in range(len(observations) - 1, -1, -1):
        if len(selected_indices) >= MAX_TOOL_OBSERVATIONS:
            break
        selected_indices.add(index)

    return [
        ToolObservation(
            capability=observation.capability,
            arguments=dict(observation.arguments),
            success=observation.success,
            output=limit_tool_output(observation.output),
        )
        for index, observation in enumerate(observations)
        if index in selected_indices
    ]
