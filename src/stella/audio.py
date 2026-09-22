"""Provider-neutral one-shot audio transcription boundary."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from stella.context import InputEnvelope, InputModality, InputPart, InputProvenance
from stella.video import VideoObservation, VideoProvider, VideoSampling
from stella.vision import VisionProvider


class TranscriptionProvider(ABC):
    """Interface for one bounded audio-to-text transcription request."""

    @abstractmethod
    def transcribe(self, audio: InputPart) -> str:
        """Return text for one audio input part."""


@dataclass(frozen=True)
class NormalizedInput:
    """Text for current reasoning plus the preserved typed input envelope."""

    text: str
    envelope: InputEnvelope


def normalize_input(
    envelope: InputEnvelope,
    transcriber: TranscriptionProvider | None = None,
    vision_provider: VisionProvider | None = None,
    video_provider: VideoProvider | None = None,
    video_sampling: VideoSampling | None = None,
) -> NormalizedInput:
    """Convert supported audio, image, and one-shot video parts to text."""

    text_parts = [
        part.content
        for part in envelope.parts
        if part.modality is InputModality.TEXT and part.content
    ]
    derived_parts: list[InputPart] = []
    audio_parts = [
        part for part in envelope.parts if part.modality is InputModality.AUDIO
    ]
    image_parts = [
        part for part in envelope.parts if part.modality is InputModality.IMAGE
    ]
    video_parts = [
        part for part in envelope.parts if part.modality is InputModality.VIDEO
    ]
    if audio_parts and transcriber is None:
        raise ValueError("an audio input requires a transcription provider")
    if image_parts and vision_provider is None:
        raise ValueError("an image input requires a vision provider")
    if video_parts:
        if len(video_parts) > 1:
            raise ValueError("only one video clip is supported")
        if video_provider is None:
            raise ValueError("a video input requires a video provider")
        if video_sampling is None:
            raise ValueError("a video input requires sampling instructions")
        if video_parts[0].reference is None or video_parts[0].content is not None:
            raise ValueError("video input must use a reference")

    for index, audio in enumerate(audio_parts):
        transcript = transcriber.transcribe(audio)  # type: ignore[union-attr]
        if not isinstance(transcript, str) or not transcript.strip():
            raise ValueError("transcription must return non-empty text")
        text_parts.append(transcript)
        derived_parts.append(
            InputPart(
                modality=InputModality.TEXT,
                provenance=InputProvenance.MODEL,
                content=transcript,
                metadata={
                    "derived_from": "audio",
                    "audio_part": str(index),
                },
            )
        )

    for index, image in enumerate(image_parts):
        description = vision_provider.describe(image)  # type: ignore[union-attr]
        if not isinstance(description, str) or not description.strip():
            raise ValueError("vision provider must return non-empty text")
        text_parts.append(description)
        derived_parts.append(
            InputPart(
                modality=InputModality.TEXT,
                provenance=InputProvenance.MODEL,
                content=description,
                metadata={
                    "derived_from": "image",
                    "image_part": str(index),
                },
            )
        )

    if video_parts:
        observations = video_provider.analyze(  # type: ignore[union-attr]
            video_parts[0], video_sampling
        )
        if not isinstance(observations, Sequence) or isinstance(
            observations, (str, bytes)
        ):
            raise ValueError("video provider must return a sequence of observations")
        if not observations or len(observations) > video_sampling.sample_count:  # type: ignore[union-attr]
            raise ValueError("video provider returned an invalid sample count")
        for index, observation in enumerate(observations):
            if not isinstance(observation, VideoObservation):
                raise TypeError("video provider returned an invalid observation")
            if (
                observation.sample_index != index
                or observation.sample_index >= video_sampling.sample_count  # type: ignore[union-attr]
                or observation.start_ms < video_sampling.start_ms  # type: ignore[union-attr]
                or observation.end_ms > video_sampling.end_ms  # type: ignore[union-attr]
            ):
                raise ValueError("video observation has invalid sampling provenance")
            text_parts.append(observation.text)
            derived_parts.append(
                InputPart(
                    modality=InputModality.TEXT,
                    provenance=InputProvenance.MODEL,
                    content=observation.text,
                    metadata={
                        "derived_from": "video",
                        "sample_index": str(observation.sample_index),
                        "start_ms": str(observation.start_ms),
                        "end_ms": str(observation.end_ms),
                        "sampling_mode": video_sampling.mode,  # type: ignore[union-attr]
                        "sample_count": str(video_sampling.sample_count),  # type: ignore[union-attr]
                    },
                )
            )

    if not text_parts:
        raise ValueError("input does not contain text or derived observations")

    return NormalizedInput(
        text="\n".join(text_parts),
        envelope=InputEnvelope((*envelope.parts, *derived_parts)),
    )
