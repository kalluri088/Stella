"""Provider-neutral one-shot video observation boundary."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from stella.context import InputPart

MAX_VIDEO_SAMPLES = 4
MAX_VIDEO_DURATION_MS = 300_000


@dataclass(frozen=True)
class VideoSampling:
    """Bounded sampling instructions for one user-supplied video clip."""

    start_ms: int
    end_ms: int
    sample_count: int
    mode: str = "uniform"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.start_ms, int)
            or isinstance(self.start_ms, bool)
            or self.start_ms < 0
        ):
            raise ValueError("video sampling start must be a non-negative integer")
        if (
            not isinstance(self.end_ms, int)
            or isinstance(self.end_ms, bool)
            or self.end_ms <= self.start_ms
        ):
            raise ValueError("video sampling end must be after its start")
        if self.end_ms - self.start_ms > MAX_VIDEO_DURATION_MS:
            raise ValueError("video sampling duration exceeds the input bound")
        if (
            not isinstance(self.sample_count, int)
            or isinstance(self.sample_count, bool)
            or not 1 <= self.sample_count <= MAX_VIDEO_SAMPLES
        ):
            raise ValueError("video sample count exceeds the input bound")
        if not isinstance(self.mode, str) or not self.mode.strip():
            raise ValueError("video sampling mode must be non-empty text")
        if len(self.mode) > 256:
            raise ValueError("video sampling mode exceeds the input bound")


@dataclass(frozen=True)
class VideoObservation:
    """One bounded, temporally located observation derived from a clip."""

    text: str
    start_ms: int
    end_ms: int
    sample_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("video observation requires non-empty text")
        if (
            not isinstance(self.start_ms, int)
            or isinstance(self.start_ms, bool)
            or not isinstance(self.end_ms, int)
            or isinstance(self.end_ms, bool)
            or self.start_ms < 0
            or self.end_ms <= self.start_ms
        ):
            raise ValueError("video observation requires a valid time range")
        if (
            not isinstance(self.sample_index, int)
            or isinstance(self.sample_index, bool)
            or self.sample_index < 0
        ):
            raise ValueError("video observation index must be non-negative")


class VideoProvider(ABC):
    """Interface for one bounded analysis of one explicit video clip."""

    @abstractmethod
    def analyze(
        self,
        video: InputPart,
        sampling: VideoSampling,
    ) -> Sequence[VideoObservation]:
        """Return ordered observations for the requested sampling window."""
