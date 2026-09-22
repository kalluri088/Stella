"""Tests for user-facing handling of tool outcomes.

Covers the three outcomes a turn can end with after a tool:
success answers from the observation, failure is reported honestly,
and an explicit approval denial states the action was not performed.
"""

from stella.brain import Brain, Decision, DecisionKind, LLMBrain, ToolUsePolicy
from stella.context import Context, ToolObservation
from stella.llm import LLMClient, LLMResponse, LLMToolDefinition, ToolUseMode
from stella.memory import InMemoryMemory
from stella.stella import Stella
from stella.tools import (
    ApprovalRequest,
    EchoTool,
    RiskLevel,
    Tool,
    ToolApproval,
    ToolDispatcher,
    ToolResult,
)
from stella.trace import ApprovalEvent


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


class UnusedLLM(LLMClient):
    def chat(self, messages) -> str:
        raise AssertionError("no LLM call is expected")


def build(brain, tool, llm=None, **kwargs) -> Stella:
    kwargs.setdefault("max_tool_steps", 2)
    return Stella(
        brain,
        llm or UnusedLLM(),
        ToolDispatcher([tool, EchoTool()]),
        InMemoryMemory(),
        **kwargs,
    )


def tool_decision(capability: str) -> Decision:
    return Decision(DecisionKind.TOOL, capability=capability, arguments={})


def test_successful_tool_answers_from_the_observation() -> None:
    brain = SequenceBrain(
        [
            tool_decision("record"),
            Decision(
                DecisionKind.ANSWER,
                content="The tool reported: tool output",
            ),
        ]
    )
    tool = RecordingTool()

    result = build(brain, tool).process(Context(user_input="do the thing"))

    assert tool.arguments == [{}]
    assert result.response == "The tool reported: tool output"
    assert result.tool_result == ToolResult(True, "tool output")


def test_failed_tool_keeps_an_honest_final_answer() -> None:
    brain = SequenceBrain(
        [
            tool_decision("record"),
            Decision(
                DecisionKind.ANSWER,
                content="The action failed: disk full.",
            ),
        ]
    )
    tool = RecordingTool(result=ToolResult(False, "Disk is full."))

    result = build(brain, tool).process(Context(user_input="do the thing"))

    assert result.response == "The action failed: disk full."
    assert result.tool_result == ToolResult(False, "Disk is full.")


def test_policy_allows_honest_answers_after_a_failed_observation() -> None:
    class AnswerLLM(LLMClient):
        tool_choice = None

        def chat(self, messages) -> str:
            raise AssertionError("decision must use the tool path")

        def chat_with_tools(self, messages, tools, tool_choice=None):
            self.tool_choice = tool_choice
            return LLMResponse(
                content=(
                    '{"kind": "answer",'
                    ' "content": "The clock lookup failed."}'
                )
            )

    llm = AnswerLLM()
    decision = LLMBrain(llm).decide(
        Context(
            user_input="What is the current time?",
            tool_observations=[
                ToolObservation(
                    capability="datetime",
                    arguments={"kind": "time"},
                    success=False,
                    output="Date/time unavailable.",
                )
            ],
        )
    )

    assert llm.tool_choice is ToolUseMode.AUTO
    assert decision.kind is DecisionKind.ANSWER
    assert decision.content == "The clock lookup failed."


def test_policy_still_requires_inspection_without_observations() -> None:
    tools = [
        LLMToolDefinition(
            name="datetime",
            description="Read the current local date or time.",
            arguments={"kind": "string"},
        )
    ]

    assert (
        ToolUsePolicy.choose(
            Context(user_input="What is the current time now?"), tools
        )
        is ToolUseMode.REQUIRED
    )


def test_denied_approval_states_the_action_was_not_performed() -> None:
    brain = SequenceBrain([tool_decision("dangerous_action")])
    tool = DangerousTool()
    stella = build(brain, tool)
    stella.approval_provider = lambda request: ToolApproval(
        request=request, approved=False
    )

    result = stella.process(
        Context(user_input="perform the dangerous action")
    )

    assert tool.arguments == []
    assert result.response == (
        "The action was not approved, so it was not performed. "
        "Nothing was changed."
    )
    assert result.tool_result == ToolResult(False, "Approval denied.")
    assert result.decision.kind is DecisionKind.TOOL
    assert [step.decision.kind for step in result.step_trace] == [
        DecisionKind.TOOL
    ]
    approval_events = [
        event
        for event in result.interaction_trace.events
        if isinstance(event, ApprovalEvent)
    ]
    assert [event.approved for event in approval_events] == [False]
    assert brain.decisions == []  # the brain was not re-consulted


def test_mismatched_approval_fails_closed_but_is_not_a_user_denial() -> None:
    brain = SequenceBrain(
        [
            tool_decision("dangerous_action"),
            Decision(
                DecisionKind.ANSWER,
                content="The action was cancelled.",
            ),
        ]
    )
    tool = DangerousTool()
    stella = build(brain, tool)

    def mismatched(request: ApprovalRequest) -> ToolApproval:
        return ToolApproval(
            request=ApprovalRequest("dangerous_action", {"other": 1}),
            approved=True,
        )

    stella.approval_provider = mismatched

    result = stella.process(
        Context(user_input="perform the dangerous action")
    )

    assert tool.arguments == []
    assert result.tool_result == ToolResult(False, "Invalid approval.")
    assert result.response == "The action was cancelled."
