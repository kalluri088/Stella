import sys

import pytest

from stella.brain import Brain, Decision, DecisionKind
from stella.cli import (
    cli_approval_provider,
    format_startup,
    format_trace,
    run_cli,
)
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
from stella.trace import (
    ApprovalEvent,
    DecisionEvent,
    FinalResponseEvent,
    InputReceivedEvent,
    InteractionTrace,
    MemoryRetrievedEvent,
    MemoryWriteEvent,
    ToolResultEvent,
)


def make_trace(*events) -> InteractionTrace:
    trace = InteractionTrace()
    for event in events:
        trace.record(event)
    return trace


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


def test_cli_shows_thinking_feedback_without_touching_transcript() -> None:
    stella = RecordingStella()
    outputs: list[str] = []
    statuses: list[str] = []
    inputs = iter(["hello", "exit"])

    run_cli(
        stella,
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        status_fn=statuses.append,
    )

    assert statuses == ["Stella is thinking..."]
    assert outputs == ["Stella: response to hello", "Goodbye!"]


def test_cli_import_loads_readline_for_line_editing() -> None:
    # Importing the CLI module must register GNU readline so that the
    # default input() prompt supports arrow keys, backspace/delete,
    # and up/down history. Platform builds without readline are skipped.
    import importlib

    try:
        import readline  # noqa: F401
    except ImportError:
        pytest.skip("this platform's Python build has no readline")

    import stella.cli

    sys.modules.pop("readline", None)
    try:
        importlib.reload(stella.cli)
        assert "readline" in sys.modules
    finally:
        sys.modules.pop("readline", None)
        importlib.reload(stella.cli)


def test_cli_empty_and_whitespace_input_is_harmless() -> None:
    stella = RecordingStella()
    outputs: list[str] = []
    statuses: list[str] = []
    inputs = iter(["", "   ", "\t ", "hello", "exit"])

    run_cli(
        stella,
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        status_fn=statuses.append,
    )

    assert [context.user_input for context in stella.contexts] == ["hello"]
    assert statuses == ["Stella is thinking..."]
    assert outputs == ["Stella: response to hello", "Goodbye!"]


class SilentStella:
    """Returns one deliberate do-nothing result without any response."""

    def __init__(self) -> None:
        self.contexts: list[Context] = []

    def process(self, context: Context) -> StellaResult:
        self.contexts.append(context)
        return StellaResult(decision=Decision(DecisionKind.DO_NOTHING))


def test_cli_silent_turn_gets_status_feedback_not_stdout() -> None:
    stella = SilentStella()
    outputs: list[str] = []
    statuses: list[str] = []
    inputs = iter(["ok", "exit"])

    run_cli(
        stella,
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        status_fn=statuses.append,
    )

    assert statuses == ["Stella is thinking...", "Stella has nothing to add."]
    assert outputs == ["Goodbye!"]


def test_cli_keyboard_interrupt_at_prompt_exits_cleanly() -> None:
    stella = RecordingStella()
    outputs: list[str] = []

    def interrupt(_prompt: str) -> str:
        raise KeyboardInterrupt

    run_cli(stella, input_fn=interrupt, output_fn=outputs.append)

    assert stella.contexts == []
    assert outputs == ["Goodbye!"]


class InterruptingStella:
    """Raises KeyboardInterrupt once, then behaves like RecordingStella."""

    def __init__(self) -> None:
        self.contexts: list[Context] = []

    def process(self, context: Context) -> StellaResult:
        self.contexts.append(context)
        if len(self.contexts) == 1:
            raise KeyboardInterrupt
        return StellaResult(
            decision=Decision(DecisionKind.ANSWER),
            response=f"response to {context.user_input}",
        )


def test_cli_keyboard_interrupt_during_turn_is_concise_and_recoverable() -> None:
    stella = InterruptingStella()
    outputs: list[str] = []
    inputs = iter(["first", "second", "exit"])

    run_cli(stella, input_fn=lambda _: next(inputs), output_fn=outputs.append)

    assert outputs == [
        "Stella stopped that request. Nothing was changed.",
        "Stella: response to second",
        "Goodbye!",
    ]
    assert stella.contexts[1].conversation_history == []


def test_cli_approval_provider_reports_thinking_only_after_approval() -> None:
    request = ApprovalRequest("approval_test", {"value": "x"})

    approved_statuses: list[str] = []
    approve = cli_approval_provider(
        input_fn=lambda _: "yes",
        output_fn=lambda _: None,
        status_fn=approved_statuses.append,
    )
    assert approve(request).approved is True
    assert approved_statuses == ["Stella is thinking..."]

    denied_statuses: list[str] = []
    deny = cli_approval_provider(
        input_fn=lambda _: "no",
        output_fn=lambda _: None,
        status_fn=denied_statuses.append,
    )
    assert deny(request).approved is False
    assert denied_statuses == []


class FlakyStella:
    """Fails the first turn, then behaves like RecordingStella."""

    def __init__(self) -> None:
        self.contexts: list[Context] = []

    def process(self, context: Context) -> StellaResult:
        self.contexts.append(context)
        if len(self.contexts) == 1:
            raise ConnectionError("   connection refused\n  details here")
        return StellaResult(
            decision=Decision(DecisionKind.ANSWER),
            response=f"response to {context.user_input}",
        )


def test_cli_recovers_from_turn_errors_without_losing_the_session() -> None:
    stella = FlakyStella()
    outputs: list[str] = []
    inputs = iter(["first", "second", "exit"])

    run_cli(stella, input_fn=lambda _: next(inputs), output_fn=outputs.append)

    assert outputs == [
        (
            "Stella could not finish that request (connection refused "
            "details here). Nothing was changed; try again or type "
            "'exit' to quit."
        ),
        "Stella: response to second",
        "Goodbye!",
    ]
    assert stella.contexts[1].conversation_history == []


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
            "Stella would like to use the 'approval_test' tool with "
            'arguments {"value": "x"}. '
            "Type 'yes' to allow this; anything else will skip it."
        )
    ]


@pytest.mark.parametrize(
    ("capability", "arguments", "summary"),
    [
        (
            "filesystem_write",
            {"path": "notes.txt", "content": "hi"},
            "create a new text file \"notes.txt\" in your Stella workspace",
        ),
        (
            "filesystem_delete",
            {"path": "notes.txt"},
            (
                "delete the file \"notes.txt\" from your Stella workspace "
                "(this cannot be undone)"
            ),
        ),
        (
            "network_read",
            {"url": "https://example.com/a.txt"},
            (
                "fetch text from this public web address: "
                "\"https://example.com/a.txt\""
            ),
        ),
        (
            "memory_forget",
            {"query": "jasmine tea"},
            (
                "delete stored memories matching \"jasmine tea\" "
                "(this cannot be undone)"
            ),
        ),
        (
            "memory_update",
            {"query": "tea", "content": "green tea"},
            "change the memory matching \"tea\" to \"green tea\"",
        ),
        ("memory_list", {}, "show everything it has remembered about you"),
    ],
)
def test_cli_approval_prompts_describe_actions_in_plain_language(
    capability: str, arguments: dict[str, object], summary: str
) -> None:
    outputs: list[str] = []
    provider = cli_approval_provider(
        input_fn=lambda _: "no",
        output_fn=outputs.append,
    )

    provider(ApprovalRequest(capability, arguments))

    assert outputs == [
        (
            f"Stella would like to {summary}. "
            "Type 'yes' to allow this; anything else will skip it."
        )
    ]


def test_cli_approval_falls_back_when_arguments_are_unusable() -> None:
    outputs: list[str] = []
    provider = cli_approval_provider(
        input_fn=lambda _: "no",
        output_fn=outputs.append,
    )

    provider(ApprovalRequest("filesystem_delete", {"path": 7}))

    assert outputs == [
        (
            "Stella would like to use the 'filesystem_delete' tool with "
            'arguments {"path": 7}. '
            "Type 'yes' to allow this; anything else will skip it."
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
        output.startswith("Stella would like to") for output in outputs
    )
    assert "Stella: The approved action completed." in outputs


class TraceStella:
    """Stand-in returning one prepared result with an interaction trace."""

    def __init__(self, result: StellaResult) -> None:
        self.result = result

    def process(self, context: Context) -> StellaResult:
        return self.result


def test_format_trace_renders_all_seven_event_types() -> None:
    result = StellaResult(
        decision=Decision(DecisionKind.ANSWER, "done"),
        response="done",
        interaction_trace=make_trace(
            InputReceivedEvent(24, 2, ("text:user",), 0),
            MemoryRetrievedEvent(1, (43,)),
            DecisionEvent("tool", "datetime", ("kind",), 0, False),
            ApprovalEvent("datetime", True),
            ToolResultEvent("datetime", ("kind",), True, 42),
            MemoryWriteEvent(True, True, 43),
            FinalResponseEvent("answer", True, 10, False, False),
        ),
    )

    assert format_trace(result) == [
        "  input     24 chars, 2 history messages",
        "  memory    retrieved 1",
        "  decision  TOOL -> datetime (kind)",
        "  approval  granted for datetime",
        "  tool      datetime success, 42 chars output",
        "  memory    written (43 chars)",
    ]


def test_format_trace_shows_denials_proposals_and_skips_noise() -> None:
    result = StellaResult(
        decision=Decision(DecisionKind.ASK, content="why?"),
        interaction_trace=make_trace(
            MemoryRetrievedEvent(0, ()),
            DecisionEvent("answer", None, (), 5, True),
            ApprovalEvent("filesystem_delete", False),
            MemoryWriteEvent(True, False, 20),
            FinalResponseEvent("ask", False, 0, True, False),
        ),
    )

    assert format_trace(result) == [
        "  decision  ANSWER +memory proposal",
        "  approval  denied for filesystem_delete",
        "  memory    proposed, not stored",
        "  final     needs more information",
    ]


def test_format_trace_handles_missing_trace_and_step_limit() -> None:
    assert format_trace(StellaResult(Decision(DecisionKind.ANSWER))) == []
    result = StellaResult(
        decision=Decision(DecisionKind.TOOL),
        interaction_trace=make_trace(
            ApprovalEvent(None, None),
            FinalResponseEvent("tool", False, 0, False, True),
        ),
    )
    assert format_trace(result) == [
        "  approval  not requested",
        "  final     stopped at step limit",
    ]


def test_cli_trace_renders_timeline_before_final_response() -> None:
    stella = TraceStella(
        StellaResult(
            decision=Decision(DecisionKind.ANSWER, "done"),
            response="done",
            interaction_trace=make_trace(
                DecisionEvent("answer", None, (), 4, False)
            ),
        )
    )
    outputs: list[str] = []

    inputs = iter(["hello", "exit"])
    run_cli(
        stella,
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        trace=True,
    )

    assert outputs == [
        "Stella did",
        "  decision  ANSWER",
        "",
        "Stella: done",
        "Goodbye!",
    ]


def test_cli_without_trace_shows_no_timeline() -> None:
    stella = TraceStella(
        StellaResult(
            decision=Decision(DecisionKind.ANSWER, "done"),
            response="done",
            interaction_trace=make_trace(
                DecisionEvent("answer", None, (), 4, False)
            ),
        )
    )
    outputs: list[str] = []

    inputs = iter(["hello", "exit"])
    run_cli(
        stella,
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
    )

    assert outputs == ["Stella: done", "Goodbye!"]


def test_format_startup_describes_configuration_without_secrets() -> None:
    from types import SimpleNamespace

    llm = SimpleNamespace(
        model="qwen3:4b",
        client=SimpleNamespace(
            base_url="http://127.0.0.1:11434/v1",
            api_key="SECRET-SENTINEL",
        ),
    )
    stella = SimpleNamespace(
        brain=SimpleNamespace(llm=llm),
        memory=SimpleNamespace(database_path="/tmp/stella.db"),
        tools=SimpleNamespace(
            _tools={"filesystem_read": SimpleNamespace(workspace="/tmp/ws")}
        ),
    )

    lines = format_startup(stella)

    assert lines == [
        "provider:  SimpleNamespace",
        "model:     qwen3:4b",
        "endpoint:  http://127.0.0.1:11434/v1",
        "memory db: /tmp/stella.db",
        "workspace: /tmp/ws",
    ]
    assert "SECRET-SENTINEL" not in "\n".join(lines)


def test_format_startup_degrades_for_minimal_stella() -> None:
    from types import SimpleNamespace

    stella = SimpleNamespace(
        brain=None,
        memory=SimpleNamespace(),
        tools=SimpleNamespace(_tools={}),
    )

    assert format_startup(stella) == [
        "provider:  unknown",
        "model:     unknown",
        "endpoint:  default",
        "memory db: in-memory",
        "workspace: not configured",
    ]
