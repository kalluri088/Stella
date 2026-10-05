"""Tests for using final ANSWER content directly after a tool observation."""

from stella.brain import Brain, Decision, DecisionKind
from stella.context import Context
from stella.llm import LLMClient
from stella.memory import InMemoryMemory
from stella.stella import Stella
from stella.tools import Tool, ToolDispatcher, ToolResult


class FinalContentBrain(Brain):
    answer_content_is_final = True

    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = list(decisions)

    def decide(self, context: Context) -> Decision:
        return self.decisions.pop(0)


class RecordingLLM(LLMClient):
    def __init__(self, response: str = "resynthesized") -> None:
        self.response = response
        self.chat_calls = 0

    def chat(self, messages) -> str:
        self.chat_calls += 1
        return self.response


class OkEcho(Tool):
    name = "echo"
    description = "Echoes."

    @property
    def argument_schema(self) -> dict[str, object]:
        return {"message": "string"}

    def validate_arguments(self, arguments) -> bool:
        return set(arguments) == {"message"}

    def execute(self, arguments) -> ToolResult:
        return ToolResult(success=True, output="observed value")


def build(brain: Brain, llm: RecordingLLM) -> Stella:
    tools = ToolDispatcher([OkEcho()])
    return Stella(brain, llm, tools, InMemoryMemory(), max_tool_steps=2)


def tool_then_answer(content: str | None) -> list[Decision]:
    return [
        Decision(
            DecisionKind.TOOL,
            capability="echo",
            arguments={"message": "go"},
        ),
        Decision(DecisionKind.ANSWER, content=content),
    ]


def test_answer_content_after_observation_is_used_directly() -> None:
    brain = FinalContentBrain(tool_then_answer("example-laptop"))
    llm = RecordingLLM()

    result = build(brain, llm).process(Context(user_input="hostname?"))

    assert result.response == "example-laptop"
    assert llm.chat_calls == 0  # no redundant synthesis call
    assert [s.decision.kind for s in result.step_trace] == [
        DecisionKind.TOOL,
        DecisionKind.ANSWER,
    ]


def test_empty_answer_content_after_observation_still_synthesizes() -> None:
    brain = FinalContentBrain(tool_then_answer(None))
    llm = RecordingLLM()

    result = build(brain, llm).process(Context(user_input="hostname?"))

    assert result.response == "resynthesized"
    assert llm.chat_calls == 1


def test_brains_without_final_content_still_synthesize() -> None:
    class OrdinaryBrain(FinalContentBrain):
        answer_content_is_final = False

    brain = OrdinaryBrain(tool_then_answer("draft note"))
    llm = RecordingLLM()

    result = build(brain, llm).process(Context(user_input="hostname?"))

    assert result.response == "resynthesized"
    assert llm.chat_calls == 1
