import pytest

from stella.brain import Brain, Decision, DecisionKind
from stella.cli import cli_approval_provider, run_cli
from stella.context import Context
from stella.llm import LLMClient
from stella.memory import InMemoryMemory
from stella.stella import Stella, StellaResult
from stella.tools import (
    ApprovalRequest,
    RiskLevel,
    Tool,
    ToolApproval,
    ToolDispatcher,
    ToolResult,
)


class RecordingStella:
    def __init__(self) -> None:
        self.contexts: list[Context] = []

    def process(self, context: Context) -> StellaResult:
        self.contexts.append(context)
        return StellaResult(
            decision=Decision(DecisionKind.ANSWER),
            response=f"response to {context.user_input}",
        )


class FixedToolBrain(Brain):
    def decide(self, context: Context) -> Decision:
        return Decision(
            DecisionKind.TOOL,
            capability="approval_test",
            arguments={"value": "x"},
        )


class RecordingLLM(LLMClient):
    def chat(self, messages) -> str:
        return "The approved action completed."


class ApprovalTool(Tool):
    name = "approval_test"
    description = "Test-only dangerous action."

    def __init__(self) -> None:
        self.executions: list[dict[str, object]] = []

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.DANGEROUS

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return arguments == {"value": "x"}

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        self.executions.append(arguments)
        return ToolResult(success=True, output="executed")


def test_cli_passes_input_to_stella_and_displays_response() -> None:
    stella = RecordingStella()
    outputs: list[str] = []
    inputs = iter(["hello", "exit"])

    run_cli(stella, input_fn=lambda _: next(inputs), output_fn=outputs.append)

    assert [context.user_input for context in stella.contexts] == ["hello"]
    assert "Stella: response to hello" in outputs


def test_cli_maintains_conversation_history() -> None:
    stella = RecordingStella()
    inputs = iter(["first", "second", "quit"])

    run_cli(stella, input_fn=lambda _: next(inputs), output_fn=lambda _: None)

    assert stella.contexts[0].conversation_history == []
    assert stella.contexts[1].conversation_history[0].content == "first"
    assert stella.contexts[1].conversation_history[0].role == "user"
    assert stella.contexts[1].conversation_history[1].content == "response to first"
    assert stella.contexts[1].conversation_history[1].role == "assistant"


def test_cli_exit_stops_without_processing_input() -> None:
    stella = RecordingStella()
    outputs: list[str] = []

    run_cli(stella, input_fn=lambda _: "exit", output_fn=outputs.append)

    assert stella.contexts == []
    assert outputs == ["Goodbye!"]


def test_cli_debug_inspects_structured_decision_without_changing_output() -> None:
    stella = RecordingStella()
    outputs: list[str] = []
    debug_outputs: list[str] = []
    inputs = iter(["hello", "exit"])

    run_cli(
        stella,
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        debug=True,
        debug_fn=debug_outputs.append,
    )

    assert outputs == ["Stella: response to hello", "Goodbye!"]
    assert debug_outputs == [
        (
            'Decision: {"arguments": null, "capability": null, '
            '"content": null, "kind": "answer", "memory_write": null}'
        )
    ]


@pytest.mark.parametrize(
    ("answer", "approved"),
    [("yes", True), ("approve", True), ("no", False), ("later", False)],
)
def test_cli_approval_provider_requires_explicit_confirmation(
    answer: str, approved: bool
) -> None:
    outputs: list[str] = []
    provider = cli_approval_provider(
        input_fn=lambda _: answer,
        output_fn=outputs.append,
    )

    request = ApprovalRequest("approval_test", {"value": "x"})
    result = provider(request)

    assert result == ToolApproval(request=request, approved=approved)
    assert outputs == [
        (
            "Approval required for action: "
            "capability='approval_test', arguments={\"value\": \"x\"}"
        )
    ]


def test_cli_approval_executes_dangerous_tool_only_after_yes() -> None:
    tool = ApprovalTool()
    stella = Stella(
        FixedToolBrain(),
        RecordingLLM(),
        ToolDispatcher([tool]),
        InMemoryMemory(),
    )
    outputs: list[str] = []
    inputs = iter(["run the dangerous test action", "yes", "exit"])

    run_cli(stella, input_fn=lambda _: next(inputs), output_fn=outputs.append)

    assert tool.executions == [{"value": "x"}]
    assert any(
        output.startswith("Approval required for action:")
        for output in outputs
    )
    assert "Stella: The approved action completed." in outputs
