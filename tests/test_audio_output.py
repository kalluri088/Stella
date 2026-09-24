import pytest

from stella.audio_output import (
    MAX_SPEECH_TEXT_CHARS,
    MIN_SPEECH_CHUNK_CHARS,
    SpeechArtifact,
    SpeechOutput,
    SpeechProvider,
    sentence_chunks,
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


# ------------------------------------------------------- sentence chunking


def test_chunks_split_on_terminal_punctuation_and_keep_delimiters() -> None:
    assert sentence_chunks(
        "Here is the plan. It has two steps! Want me to run them?"
    ) == ["Here is the plan.", "It has two steps!", "Want me to run them?"]


def test_chunks_never_split_abbreviations_initials_or_decimals() -> None:
    chunks = sentence_chunks(
        "Call Dr. Rao about the 3.5 build. J. Smith agreed etc. "
        "The U.S. reply is final. Nothing else remains."
    )

    assert chunks == [
        "Call Dr. Rao about the 3.5 build.",
        "J. Smith agreed etc. The U.S. reply is final.",
        "Nothing else remains.",
    ]


def test_digits_ending_a_period_like_run_do_not_split() -> None:
    # "exactly 5." reads like a decimal to a naive splitter and stays
    # unsplit; the conservative cost is one longer chunk, never bad text.
    assert sentence_chunks(
        "We saw exactly 5. Nothing more happened after that."
    ) == ["We saw exactly 5. Nothing more happened after that."]


def test_blank_lines_break_paragraphs_and_wrapping_does_not_matter() -> None:
    assert sentence_chunks(
        "First paragraph line.\nWrapped line here.\n\n"
        "Second paragraph text."
    ) == [
        "First paragraph line.",
        "Wrapped line here.",
        "Second paragraph text.",
    ]


def test_tiny_fragments_absorb_into_their_neighbour() -> None:
    assert sentence_chunks("Hi. Let me check the rest of this for you.") == [
        "Hi. Let me check the rest of this for you."
    ]
    assert sentence_chunks(
        "Call Dr. Rao about the 3.5 build. That is honest. Really. "
        "Nothing else remains."
    ) == [
        "Call Dr. Rao about the 3.5 build.",
        "That is honest. Really.",
        "Nothing else remains.",
    ]


def test_a_reply_without_a_split_point_is_exactly_one_chunk() -> None:
    assert sentence_chunks("the action completed") == ["the action completed"]
    assert sentence_chunks("   ") == []
    assert sentence_chunks("") == []


def test_chunks_conserve_the_reply_modulo_whitespace_and_stay_bounded() -> (
    None
):
    text = (
        "A whole sentence here. A second one follows!\n\n"
        "Third paragraph here with a closing quote.\" "
        + "x" * MIN_SPEECH_CHUNK_CHARS
    )
    chunks = sentence_chunks(text)

    assert " ".join(chunks).split() == text.split()
    assert all(len(chunk) <= MAX_SPEECH_TEXT_CHARS for chunk in chunks)
