"""Report 33 W5: a call that cannot execute must never be an "approve?".

The burn-in pty log showed the approval prompt rendering a wrapped
{"arguments":…,"function":…} envelope and an empty-args filesystem
call: the user approved a label, the dispatcher then rejected the call,
and nothing happened. The gate in Stella._execute_tool validates the
exact way the dispatcher will before any prompt; these tests pin both
the no-prompt behavior and the regression that *valid* dangerous calls
still ask.
"""

from stella.brain import Decision, DecisionKind
from stella.context import Context
from stella.memory import InMemoryMemory
from stella.stella import Stella
from stella.tools import (
    ApprovalRequest,
    RiskLevel,
    Tool,
    ToolApproval,
    ToolDispatcher,
    ToolResult,
)


class StubBrain:
    def __init__(self, decision: Decision) -> None:
        self._decision = decision

    def decide(self, *args, **kwargs) -> Decision:
        return self._decision


class StubLLM:
    def complete(self, *args, **kwargs) -> str:
        return "understood"

    def chat(self, messages, **kwargs) -> str:
        return "understood"


class DangerousWriteTool(Tool):
    """A DANGEROUS floor tool with a real argument schema, like
    filesystem_write: {"path": str} and nothing else."""

    name = "write_thing"
    description = "Writes one thing."

    def __init__(self) -> None:
        self.executed: list[dict[str, object]] = []

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.DANGEROUS

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return (
            isinstance(arguments, dict)
            and set(arguments) == {"path"}
            and isinstance(arguments["path"], str)
            and bool(arguments["path"].strip())
        )

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        self.executed.append(dict(arguments))
        return ToolResult(success=True, output="written")


def make(
    arguments: dict[str, object],
    prompts: list[ApprovalRequest],
) -> tuple[Stella, DangerousWriteTool]:
    tool = DangerousWriteTool()

    def provider(request: ApprovalRequest, preview=None) -> ToolApproval:
        prompts.append(request)
        return ToolApproval(request=request, approved=True)

    stella = Stella(
        StubBrain(
            Decision(DecisionKind.TOOL, arguments=arguments, capability="write_thing")
        ),
        StubLLM(),
        ToolDispatcher([tool]),
        InMemoryMemory(),
        approval_provider=provider,
    )
    return stella, tool


def test_empty_arguments_never_reach_the_approval_prompt() -> None:
    prompts: list[ApprovalRequest] = []
    stella, tool = make({}, prompts)

    result = stella.process(Context(user_input="write the thing"))

    assert prompts == []  # W5's core: no "approve?" for a no-op call
    assert tool.executed == []
    assert result.tool_result is not None
    assert not result.tool_result.success
    assert result.tool_result.output == "Invalid tool arguments."


def test_wrapped_function_envelope_never_reaches_the_prompt() -> None:
    prompts: list[ApprovalRequest] = []
    stella, tool = make(
        {
            "arguments": {"path": "notes.txt"},
            "function": "write_thing",
        },
        prompts,
    )

    result = stella.process(Context(user_input="write the thing"))

    assert prompts == []
    assert tool.executed == []
    assert result.tool_result is not None
    assert result.tool_result.output == "Invalid tool arguments."


def test_wrong_shaped_arguments_never_reach_the_prompt() -> None:
    prompts: list[ApprovalRequest] = []
    stella, _ = make({"path": ""}, prompts)

    result = stella.process(Context(user_input="write the thing"))

    assert prompts == []
    assert result.tool_result is not None
    assert not result.tool_result.success


def test_valid_dangerous_call_still_prompts_and_executes() -> None:
    # The gate must not soften the approval wall for real calls.
    prompts: list[ApprovalRequest] = []
    stella, tool = make({"path": "notes.txt"}, prompts)

    result = stella.process(Context(user_input="write the thing"))

    assert [request.capability for request in prompts] == ["write_thing"]
    assert tool.executed == [{"path": "notes.txt"}]
    assert result.tool_result is not None
    assert result.tool_result.success


def test_rejected_call_still_lands_in_the_audit_trail() -> None:
    # Rule 10: skipping the prompt must not skip the record — the
    # dispatcher's rejection stays consultable.
    prompts: list[ApprovalRequest] = []
    stella, _tool = make({}, prompts)
    dispatcher = stella.tools

    stella.process(Context(user_input="write the thing"))

    record = dispatcher.audit_records[-1]
    assert record.capability == "write_thing"
    assert not record.execution_success
    assert record.approval_required is False or record.approval_granted is None
