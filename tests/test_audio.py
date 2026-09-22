from stella.audio import TranscriptionProvider, normalize_input
from stella.brain import Brain, Decision, DecisionKind
from stella.context import (
    Context,
    InputEnvelope,
    InputModality,
    InputPart,
    InputProvenance,
)
from stella.llm import LLMClient, Message
from stella.memory import InMemoryMemory
from stella.stella import Stella
from stella.tools import EchoTool


class RecordingBrain(Brain):
    answer_content_is_final = True

    def __init__(self) -> None:
        self.contexts: list[Context] = []

    def decide(self, context: Context) -> Decision:
        self.contexts.append(context)
        return Decision(DecisionKind.ANSWER, content="Handled the request.")


class RecordingLLM(LLMClient):
    def __init__(self) -> None:
        self.messages: list[list[Message | dict[str, str]]] = []

    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        self.messages.append(messages)
        return "unexpected model call"


class FakeTranscriber(TranscriptionProvider):
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript
        self.audio_parts: list[InputPart] = []

    def transcribe(self, audio: InputPart) -> str:
        self.audio_parts.append(audio)
        return self.transcript


def audio_envelope() -> InputEnvelope:
    return InputEnvelope(
        (
            InputPart(
                modality=InputModality.AUDIO,
                provenance=InputProvenance.USER,
                reference="attachment:audio:1",
                metadata={"format": "wav"},
            ),
        )
    )


def test_text_input_uses_existing_reasoning_path() -> None:
    brain = RecordingBrain()
    stella = Stella(brain, RecordingLLM(), EchoTool(), InMemoryMemory())

    result = stella.process_input(InputEnvelope.from_text("Hello Stella"))

    assert result.response == "Handled the request."
    assert brain.contexts[0].user_input == "Hello Stella"
    assert brain.contexts[0].input_envelope == InputEnvelope.from_text(
        "Hello Stella"
    )


def test_audio_is_transcribed_at_provider_boundary_and_provenance_is_preserved():
    transcriber = FakeTranscriber("What time is it?")
    normalized = normalize_input(audio_envelope(), transcriber)

    assert normalized.text == "What time is it?"
    assert transcriber.audio_parts[0].modality is InputModality.AUDIO
    assert normalized.envelope.parts[0].modality is InputModality.AUDIO
    assert normalized.envelope.parts[1] == InputPart(
        modality=InputModality.TEXT,
        provenance=InputProvenance.MODEL,
        content="What time is it?",
        metadata={"derived_from": "audio", "audio_part": "0"},
    )


def test_audio_input_reaches_existing_stella_reasoning_integration() -> None:
    brain = RecordingBrain()
    llm = RecordingLLM()
    transcriber = FakeTranscriber("Please answer from my spoken request.")
    stella = Stella(brain, llm, EchoTool(), InMemoryMemory())

    result = stella.process_input(audio_envelope(), transcriber)

    assert result.response == "Handled the request."
    assert brain.contexts[0].user_input == (
        "Please answer from my spoken request."
    )
    assert brain.contexts[0].input_envelope.parts[0].modality is (
        InputModality.AUDIO
    )
    assert brain.contexts[0].input_envelope.parts[1].provenance is (
        InputProvenance.MODEL
    )
    assert llm.messages == []


def test_audio_requires_a_provider_and_transcript_must_be_text() -> None:
    try:
        normalize_input(audio_envelope())
    except ValueError as error:
        assert str(error) == "an audio input requires a transcription provider"
    else:
        raise AssertionError("audio without a provider should fail")

    invalid = FakeTranscriber("")
    try:
        normalize_input(audio_envelope(), invalid)
    except ValueError as error:
        assert str(error) == "transcription must return non-empty text"
    else:
        raise AssertionError("empty transcription should fail")
