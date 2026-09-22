"""Tests for the targeted tool fast path (decision -> tool -> synthesis)."""

import json

from stella.brain import Brain, Decision, DecisionKind, LLMBrain
from stella.context import Context
from stella.llm import LLMClient
from stella.memory import InMemoryMemory
from stella.stella import Stella
from stella.tools import (
    EchoTool,
    RiskLevel,
    Tool,
    ToolApproval,
    ToolDispatcher,
    ToolResult,
)


class RecordingTool(Tool):
    name = "record"
    description = "Records structured arguments."

    def __init__(self, result: ToolResult | None = None) -> None:
        self.arguments: list[dict[str, object]] = []
        self._result = result or ToolResult(success=True, output="tool output")

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return True

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        self.arguments.append(arguments)
        return self._result


class DangerousTool(RecordingTool):
    name = "dangerous_action"
    description = "Test-only approval-required action."

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.DANGEROUS


class SequenceBrain(Brain):
    answer_content_is_final = True

    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = list(decisions)

    def decide(self, context: Context) -> Decision:
        if not self.decisions:
            raise AssertionError("brain consulted more often than expected")
        return self.decisions.pop(0)


class RecordingLLM(LLMClient):
    def __init__(self, response: str = "synthesized answer") -> None:
        self.response = response
        self.messages: list[list] = []

    def chat(self, messages) -> str:
        self.messages.append(messages)
        return self.response


def build(brain, tool=None, llm=None, **kwargs) -> tuple[Stella, object, RecordingLLM]:
    tool = tool or RecordingTool()
    llm = llm or RecordingLLM()
    dispatcher = ToolDispatcher([tool, EchoTool()])
    kwargs.setdefault("max_tool_steps", 2)
    stella = Stella(brain, llm, dispatcher, InMemoryMemory(), **kwargs)
    return stella, tool, llm


def tool_decision(capability: str, final: bool) -> Decision:
    return Decision(
        DecisionKind.TOOL,
        capability=capability,
        arguments={},
        tool_final=final,
    )


def run(stella: Stella) -> object:
    return stella.process(Context(user_input="do the thing"))


def test_successful_single_tool_turn_skips_redecision() -> None:
    brain = SequenceBrain([tool_decision("record", True)])
    stella, tool, llm = build(brain)

    result = run(stella)

    assert tool.arguments == [{}]
    assert len(llm.messages) == 1  # synthesis only, no re-decision call
    assert result.response == "synthesized answer"
    assert result.decision.kind is DecisionKind.ANSWER
    assert [step.decision.kind for step in result.step_trace] == [
        DecisionKind.TOOL,
        DecisionKind.ANSWER,
    ]
    assert result.step_trace[0].decision.capability == "record"


def test_fast_path_synthesizes_from_the_observation() -> None:
    brain = SequenceBrain([tool_decision("record", True)])
    stella, _tool, llm = build(brain)

    run(stella)

    payload = json.loads(llm.messages[0][-1].content)
    observation = payload["tool_observations"][0]
    assert observation["capability"] == "record"
    assert observation["success"] is True
    assert observation["output"] == "tool output"


def test_failed_single_tool_turn_reports_honestly_without_redecision() -> None:
    brain = SequenceBrain([tool_decision("record", True)])
    failing = RecordingTool(
        result=ToolResult(success=False, output="File was not found.")
    )
    stella, _tool, llm = build(brain, tool=failing)

    result = run(stella)

    assert len(llm.messages) == 1
    assert result.tool_result is not None
    assert result.tool_result.success is False
    payload = json.loads(llm.messages[0][-1].content)
    observation = payload["tool_observations"][0]
    assert observation["success"] is False
    assert observation["output"] == "File was not found."
    assert result.decision.kind is DecisionKind.ANSWER


def test_fast_path_records_second_decision_trace_event() -> None:
    brain = SequenceBrain([tool_decision("record", True)])
    stella, _tool, _llm = build(brain)

    result = run(stella)

    kinds = [
        event.kind
        for event in result.interaction_trace.events
        if type(event).__name__ == "DecisionEvent"
    ]
    assert kinds == ["tool", "answer"]


def test_multi_step_chaining_still_redecides() -> None:
    brain = SequenceBrain(
        [
            tool_decision("record", False),
            tool_decision("record", False),
            Decision(DecisionKind.ANSWER, content="enough"),
        ]
    )
    stella, tool, _llm = build(brain)

    result = run(stella)

    assert brain.decisions == []  # a fresh decision followed every tool
    assert tool.arguments == [{}, {}]  # same tool chained across steps
    assert [step.decision.kind for step in result.step_trace] == [
        DecisionKind.TOOL,
        DecisionKind.TOOL,
        DecisionKind.ANSWER,
    ]
    assert result.response == "synthesized answer"


def test_ask_after_observation_still_works() -> None:
    brain = SequenceBrain(
        [
            tool_decision("record", False),
            Decision(DecisionKind.ASK, content="which file do you mean?"),
        ]
    )
    stella, _tool, llm = build(brain)

    result = run(stella)

    assert brain.decisions == []  # re-decision happened
    assert len(llm.messages) == 0  # never synthesized
    assert result.needs_more_information is True
    assert result.response == "which file do you mean?"


def test_fast_path_still_requires_approval_and_denial_blocks_execution() -> (
    None
):
    calls: list[str] = []

    def provider(request):
        calls.append(request.capability)
        return ToolApproval(request, False)

    brain = SequenceBrain([tool_decision("dangerous_action", True)])
    tool = DangerousTool()
    stella, _tool, llm = build(brain, tool=tool)
    stella.approval_provider = provider

    result = run(stella)

    assert calls == ["dangerous_action"]
    assert tool.arguments == []  # denial prevented execution
    assert result.tool_result is not None
    assert result.tool_result.success is False
    # the denial is still synthesized into an honest final response
    assert len(llm.messages) == 1


def test_llm_brain_parses_tool_final_flag() -> None:
    parse = LLMBrain._parse_decision
    decision = parse(
        '{"kind":"tool","capability":"datetime","arguments":'
        '{"kind":"time"},"tool_final":true}'
    )
    assert decision.kind is DecisionKind.TOOL
    assert decision.tool_final is True

    default = parse(
        '{"kind":"tool","capability":"datetime","arguments":'
        '{"kind":"time"}}'
    )
    assert default.tool_final is False

    non_tool = parse('{"kind":"answer","content":"hi","tool_final":true}')
    assert non_tool.tool_final is False

    junk = parse(
        '{"kind":"tool","capability":"datetime","arguments":{},"tool_final":"yes"}'
    )
    assert junk.kind is DecisionKind.DO_NOTHING


def test_fast_path_only_applies_to_the_first_tool_call() -> None:
    brain = SequenceBrain(
        [
            tool_decision("record", False),
            tool_decision("record", True),
            Decision(DecisionKind.ANSWER, content="after re-decision"),
        ]
    )
    stella, tool, _llm = build(brain, max_tool_steps=2)

    result = run(stella)

    # the second tool's tool_final must not skip the re-decision
    assert brain.decisions == []
    assert len(tool.arguments) == 2
    assert result.decision.kind is DecisionKind.ANSWER
    assert result.response == "synthesized answer"


def test_single_step_configuration_keeps_its_existing_path() -> None:
    brain = SequenceBrain([tool_decision("record", True)])
    stella, _tool, llm = build(brain, max_tool_steps=1)

    result = run(stella)

    # max_tool_steps==1 returns with the TOOL decision as metadata, as before
    assert result.decision.kind is DecisionKind.TOOL
    assert len(llm.messages) == 1
