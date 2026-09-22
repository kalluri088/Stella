import pytest

from stella.audio_output import (
    MAX_SPEECH_TEXT_CHARS,
    SpeechArtifact,
    SpeechOutput,
    SpeechProvider,
)
from stella.brain import Brain, Decision, DecisionKind
from stella.context import Context
from stella.llm import LLMClient, Message
from stella.memory import InMemoryMemory
from stella.stella import Stella, StellaResult
from stella.tools import EchoTool


class ResponseBrain(Brain):
    answer_content_is_final = True

    def decide(self, context: Context) -> Decision:
        return Decision(DecisionKind.ANSWER, content="Your request is complete.")


class UnusedLLM(LLMClient):
    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        raise AssertionError("the final response should not call the LLM")


class FakeSpeechProvider(SpeechProvider):
    def __init__(self) -> None:
        self.outputs: list[SpeechOutput] = []

    def speak(self, output: SpeechOutput) -> SpeechArtifact:
        self.outputs.append(output)
        return SpeechArtifact("fake://speech/1")


def test_speech_output_is_bounded_and_provider_neutral() -> None:
    output = SpeechOutput("Hello aloud.")

    assert output.text == "Hello aloud."
    with pytest.raises(ValueError, match="output bound"):
        SpeechOutput("x" * (MAX_SPEECH_TEXT_CHARS + 1))


def test_stella_response_can_be_rendered_without_changing_decision() -> None:
    brain = ResponseBrain()
    stella = Stella(brain, UnusedLLM(), EchoTool(), InMemoryMemory())
    result = stella.process(Context(user_input="Finish the task."))
    provider = FakeSpeechProvider()

    artifact = stella.speak(result, provider)

    assert result.decision.kind is DecisionKind.ANSWER
    assert result.response == "Your request is complete."
    assert artifact == SpeechArtifact("fake://speech/1")
    assert provider.outputs == [SpeechOutput("Your request is complete.")]


def test_speech_provider_receives_no_authority_or_non_response_context() -> None:
    provider = FakeSpeechProvider()
    result = StellaResult(
        decision=Decision(
            DecisionKind.ANSWER,
            content="safe response",
            capability="filesystem_delete",
        ),
        response="safe response",
    )

    Stella.speak(result, provider)

    assert provider.outputs[0] == SpeechOutput("safe response")
    assert not hasattr(provider.outputs[0], "capability")
    assert not hasattr(provider.outputs[0], "memory_write")


def test_non_final_result_cannot_be_sent_to_speech() -> None:
    provider = FakeSpeechProvider()
    result = StellaResult(decision=Decision(DecisionKind.DO_NOTHING))

    with pytest.raises(ValueError, match="final response"):
        Stella.speak(result, provider)

    assert provider.outputs == []
