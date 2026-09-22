"""Provider-neutral one-shot speech-output boundary."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

MAX_SPEECH_TEXT_CHARS = 4000


@dataclass(frozen=True)
class SpeechOutput:
    """Bounded final response text prepared for speech rendering."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("speech output requires non-empty text")
        if len(self.text) > MAX_SPEECH_TEXT_CHARS:
            raise ValueError("speech output exceeds the output bound")


@dataclass(frozen=True)
class SpeechArtifact:
    """Provider-neutral reference to rendered speech."""

    reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.reference, str) or not self.reference.strip():
            raise ValueError("speech artifact requires a reference")


class SpeechProvider(ABC):
    """Interface for rendering one final response as speech."""

    @abstractmethod
    def speak(self, output: SpeechOutput) -> SpeechArtifact:
        """Render the supplied response text and return its artifact."""
