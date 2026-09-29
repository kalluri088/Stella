import sys

import pytest

from stella import cli
from stella.app import StellaSession
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
from stella.persona import TranscriptRecorder
from stella.stella import Stella, StellaResult
from stella.tools import (
    ActionPreview,
    ApprovalRequest,
    PersonaEditTool,
    RiskLevel,
    Tool,
    ToolApproval,
    ToolDispatcher,
    ToolResult,
)
from stella.trace import (
    ActionReceiptEvent,
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

    def process(self, context: Context, **options) -> StellaResult:
        self.contexts.append(context)
        return StellaResult(
            decision=Decision(DecisionKind.ANSWER),
            response=f"response to {context.user_input}",
        )


class NarratingStella(RecordingStella):
    """Replays the activity sequence a tool turn actually produces."""

    def process(self, context: Context, **options) -> StellaResult:
        result = super().process(context, **options)
        activity = options.get("on_activity")
        if activity is not None:
            for kind in (
                "thinking",
                "working",
                "calling:reminder_list",
                "thinking",
                "answering",
            ):
                activity(kind)
        return result


def test_cli_tool_turn_narrates_the_call_and_the_answer() -> None:
    # Report 35 target 3: no more dead silence across the multi-second
    # tool+synthesis stretch — the status line names what is running.
    stella = NarratingStella()
    statuses: list[str] = []
    inputs = iter(["what is on my list", "exit"])

    run_cli(
        stella,
        input_fn=lambda _: next(inputs),
        output_fn=lambda _: None,
        status_fn=statuses.append,
    )

    assert statuses == [
        "Stella is thinking...",
        "Stella is calling reminder_list...",
        "Stella is composing the answer...",
    ]


def test_cli_plain_answer_turn_stays_quiet() -> None:
    # The "composing" line only earns its place after a tool ran; a
    # direct answer keeps the single thinking line.
    class AnsweringStella(RecordingStella):
        def process(self, context: Context, **options) -> StellaResult:
            result = super().process(context, **options)
            activity = options.get("on_activity")
            if activity is not None:
                activity("thinking")
                activity("answering")
            return result

    statuses: list[str] = []
    inputs = iter(["hello there", "exit"])

    run_cli(
        AnsweringStella(),
        input_fn=lambda _: next(inputs),
        output_fn=lambda _: None,
        status_fn=statuses.append,
    )

    assert statuses == ["Stella is thinking..."]


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

    def process(self, context: Context, **options) -> StellaResult:
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

    def process(self, context: Context, **options) -> StellaResult:
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

    def process(self, context: Context, **options) -> StellaResult:
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


def test_run_cli_records_through_the_application_session(tmp_path) -> None:
    # The CLI must use the application's session, not a private one, or
    # the opt-in transcript never sees a turn (live-pass regression).
    stella = RecordingStella()
    session = StellaSession(
        stella, transcripts=TranscriptRecorder(tmp_path / "transcript.db")
    )
    inputs = iter(["hello", "exit"])

    run_cli(
        stella,
        input_fn=lambda _: next(inputs),
        output_fn=lambda _: None,
        session=session,
    )

    rows = session.transcripts.rows_since()
    assert [(row.role, row.text) for row in rows] == [
        ("user", "hello"),
        ("assistant", "response to hello"),
    ]


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


def test_cli_approval_provider_prints_preview_without_changing_the_answer() -> None:
    outputs: list[str] = []
    provider = cli_approval_provider(
        input_fn=lambda _: "yes",
        output_fn=outputs.append,
    )
    request = ApprovalRequest("approval_test", {"value": "x"})
    preview = ActionPreview(detail_lines=("- old", "+ new"), truncated=True)

    result = provider(request, preview)

    assert result == ToolApproval(request=request, approved=True)
    assert outputs[0] == (
        "Stella would like to use the 'approval_test' tool with "
        'arguments {"value": "x"}. '
        "Type 'yes' to allow this; anything else will skip it."
    )
    assert "    - old" in outputs
    assert "    + new" in outputs
    assert "    [preview truncated]" in outputs


@pytest.mark.parametrize(
    ("capability", "arguments", "summary"),
    [
        (
            "filesystem_write",
            {"path": "notes.txt", "content": "hi"},
            "create a new text file \"notes.txt\" in your Stella workspace",
        ),
        (
            "filesystem_edit",
            {"path": "notes.txt", "content": "hi"},
            "replace the contents of \"notes.txt\" in your Stella workspace",
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
        (
            "memory_write",
            {"content": "The user prefers tea"},
            "remember this as a permanent fact: \"The user prefers tea\"",
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

    def process(self, context: Context, **options) -> StellaResult:
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


def test_format_trace_renders_action_receipts() -> None:
    result = StellaResult(
        decision=Decision(DecisionKind.ANSWER, content="done"),
        response="done",
        interaction_trace=make_trace(
            ActionReceiptEvent("filesystem_write", "create", "verified", 12),
            ActionReceiptEvent("filesystem_delete", "delete", "unverified"),
            ActionReceiptEvent("filesystem_edit", "edit", "missing"),
        ),
    )

    assert format_trace(result) == [
        "  action    create verified (12 bytes)",
        "  action    delete unverified",
        "  action    edit missing",
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
        reminders=SimpleNamespace(database_path="/tmp/reminders.db"),
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
        "reminders db: /tmp/reminders.db",
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
        "reminders: disabled",
        "workspace: not configured",
    ]


def test_cli_main_points_unconfigured_users_at_the_setup_window(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # First-run regression: the CLI never dumps a traceback or requires
    # environment variables; it names the setup window instead.
    from stella import cli

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("STELLA_MODEL", raising=False)
    with pytest.raises(SystemExit) as raised:
        cli.main([])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "stella-ui" in captured.err
    assert "STELLA_MODEL" in captured.err


def test_cli_main_uses_saved_configuration_without_environment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Startup regression: a saved configuration alone is enough to reach
    # build_application (patched here; no LLM or display is started).
    from stella import cli
    from stella import config as stella_config

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("STELLA_MODEL", raising=False)
    stella_config.save_configuration(
        stella_config.StellaSettings(
            provider="ollama", model="saved-cli-model"
        )
    )
    seen: list = []

    def fake_build(settings):
        seen.append(settings)
        raise SystemExit(0)

    monkeypatch.setattr(cli, "build_application", fake_build)
    with pytest.raises(SystemExit) as raised:
        cli.main([])
    assert raised.value.code == 0
    assert seen[0].model == "saved-cli-model"


# ---------------------------------------------------------------------------
# Persona CLI: presets, editor, and first-run onboarding (all paths are
# forced into tmp_path via STELLA_PERSONA_DIR; nothing here may touch the
# real ~/.config/stella).
# ---------------------------------------------------------------------------


@pytest.fixture()
def persona_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    directory = tmp_path / "persona-config"
    monkeypatch.setenv("STELLA_PERSONA_DIR", str(directory))
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    return directory


def script_input(answers: list[str]):
    iterator = iter(answers)

    def fake_input(prompt: str = "") -> str:
        return next(iterator)

    return fake_input


class CollectingOutput:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        self.lines.append(message)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class DraftLLM:
    def __init__(self, reply: str = "", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[list] = []

    def chat(self, messages) -> str:
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        return self.reply


def make_onboarding_stella(llm):
    from types import SimpleNamespace

    return SimpleNamespace(llm=llm)


def test_persona_preset_writes_template_and_refuses_to_clobber(
    persona_dir,
) -> None:
    assert cli.apply_persona_preset("snark") == 0
    written = (persona_dir / "persona.md").read_text(encoding="utf-8")
    for section in ("## BACKSTORY", "## VOICE", "## STANCE", "## EXAMPLES"):
        assert section in written
    # A second preset without --force keeps the existing persona untouched.
    assert cli.apply_persona_preset("terse") == 1
    assert (
        "The user's clock is the only schedule that matters."
        not in (persona_dir / "persona.md").read_text(encoding="utf-8")
    )
    assert cli.apply_persona_preset("terse", force=True) == 0
    assert "personal insult to waste" in (
        persona_dir / "persona.md"
    ).read_text(encoding="utf-8")


def test_main_persona_preset_subcommand_writes_file(persona_dir) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["persona", "preset", "warm"])
    assert raised.value.code == 0
    written = (persona_dir / "persona.md").read_text(encoding="utf-8")
    assert "## BACKSTORY" in written
    assert "impossible to fluster" in written


def test_main_persona_opens_editor_on_skeleton(
    persona_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    monkeypatch.setenv("EDITOR", "myeditor -w")
    launched: list[list[str]] = []

    def fake_run(command, check):
        launched.append(list(command))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as raised:
        cli.main(["persona"])
    assert raised.value.code == 0
    # The commented skeleton was created and handed to the split command.
    skeleton = (persona_dir / "persona.md").read_text(encoding="utf-8")
    assert "## VOICE" in skeleton and skeleton.lstrip().startswith("#")
    assert launched == [["myeditor", "-w", str(persona_dir / "persona.md")]]
    # An existing persona is opened as-is, never re-skeletonized.
    (persona_dir / "persona.md").write_text("mine", encoding="utf-8")
    assert cli.open_persona_editor() == 0
    assert (persona_dir / "persona.md").read_text(encoding="utf-8") == "mine"


def test_persona_editor_reports_an_unlaunchable_editor(
    persona_dir, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("EDITOR", "/nonexistent/stella-test-editor")
    assert cli.open_persona_editor() == 1
    captured = capsys.readouterr()
    assert "Could not start" in captured.out
    # The skeleton is still there for a manual edit.
    assert (persona_dir / "persona.md").exists()


def test_onboarding_accept_writes_draft(persona_dir) -> None:
    draft = "## BACKSTORY\nA dry wit.\n"
    llm = DraftLLM(reply=draft)
    output = CollectingOutput()
    cli.run_persona_onboarding(
        make_onboarding_stella(llm),
        input_fn=script_input(
            ["ex-colleague", "old friend", "never pads", "yes"]
        ),
        output_fn=output,
    )
    assert (persona_dir / "persona.md").read_text(encoding="utf-8") == draft
    assert not (persona_dir / cli.ONBOARDING_SKIP_MARKER).exists()
    # The three answers reached the model inside the drafting prompt.
    assert len(llm.calls) == 1
    prompt = llm.calls[0][1].content
    assert "ex-colleague" in prompt and "never pads" in prompt


def test_onboarding_edit_writes_draft_then_opens_editor(
    persona_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EDITOR", "myeditor")
    launched: list[list[str]] = []

    def fake_run(command, check):
        launched.append(list(command))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    llm = DraftLLM(reply="## VOICE\nshort\n")
    cli.run_persona_onboarding(
        make_onboarding_stella(llm),
        input_fn=script_input(["a", "b", "c", "edit"]),
        output_fn=CollectingOutput(),
    )
    assert "short" in (persona_dir / "persona.md").read_text(encoding="utf-8")
    assert launched == [["myeditor", str(persona_dir / "persona.md")]]


def test_onboarding_decline_saves_nothing_and_never_asks_again(
    persona_dir,
) -> None:
    llm = DraftLLM(reply="## BACKSTORY\nx\n")
    cli.run_persona_onboarding(
        make_onboarding_stella(llm),
        input_fn=script_input(["a", "b", "c", "no thanks"]),
        output_fn=CollectingOutput(),
    )
    assert not (persona_dir / "persona.md").exists()
    assert (persona_dir / cli.ONBOARDING_SKIP_MARKER).exists()
    # A second session sees the marker and stays quiet.
    def must_not_ask(prompt: str = "") -> str:
        raise AssertionError("onboarding asked again after a decline")

    cli.run_persona_onboarding(
        make_onboarding_stella(DraftLLM()),
        input_fn=must_not_ask,
        output_fn=CollectingOutput(),
    )


def test_onboarding_first_empty_answer_skips_without_a_model_call(
    persona_dir,
) -> None:
    llm = DraftLLM(reply="unused")
    cli.run_persona_onboarding(
        make_onboarding_stella(llm),
        input_fn=script_input([""]),
        output_fn=CollectingOutput(),
    )
    assert llm.calls == []
    assert not (persona_dir / "persona.md").exists()
    assert (persona_dir / cli.ONBOARDING_SKIP_MARKER).exists()


def test_onboarding_draft_failure_writes_nothing_but_reports(
    persona_dir,
) -> None:
    output = CollectingOutput()
    llm = DraftLLM(error=RuntimeError("model offline"))
    cli.run_persona_onboarding(
        make_onboarding_stella(llm),
        input_fn=script_input(["a", "b", "c"]),
        output_fn=output,
    )
    assert not (persona_dir / "persona.md").exists()
    assert "could not draft" in output.text
    assert "model offline" in output.text


def test_onboarding_is_silent_when_a_persona_already_exists(
    persona_dir,
) -> None:
    persona_dir.mkdir(parents=True)
    (persona_dir / "persona.md").write_text("keep me", encoding="utf-8")

    def must_not_ask(prompt: str = "") -> str:
        raise AssertionError("onboarding ran despite an existing persona")

    cli.run_persona_onboarding(
        make_onboarding_stella(DraftLLM()),
        input_fn=must_not_ask,
        output_fn=CollectingOutput(),
    )
    assert (persona_dir / "persona.md").read_text(encoding="utf-8") == "keep me"


# ----------------------------------------------------------- stella reflect


def test_reflect_reports_transcripts_off_without_building_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace as dataclass_replace

    from stella.app import StellaSettings

    output = CollectingOutput()
    monkeypatch.setattr(
        cli,
        "resolve_settings",
        lambda: dataclass_replace(
            StellaSettings(provider="ollama", model="m"),
            transcripts_enabled=False,
        ),
    )

    def must_not_build(settings):
        raise AssertionError("reflect must not build with transcripts off")

    monkeypatch.setattr(cli, "build_application", must_not_build)
    assert cli.run_persona_reflection(output_fn=output) == 0
    assert "Transcript recording is off" in output.text
    assert "Nothing was written" not in output.text


def test_main_reflect_subcommand_routes_to_the_reflection_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        cli, "run_persona_reflection", lambda: calls.append(1) or 0
    )
    with pytest.raises(SystemExit) as raised:
        cli.main(["reflect"])
    assert raised.value.code == 0
    assert calls == [1]


def test_run_cli_drains_persona_proposals_after_approvals_exist(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stella.persona import PersonaPaths, ReflectionStore

    paths = PersonaPaths(tmp_path)
    store = ReflectionStore(tmp_path / "t.db")
    store.queue_proposal(
        {
            "path": str(paths.addons),
            "content": "- drier replies\n",
            "summary": "reflection: drier",
        },
        evidence_lines=1,
    )
    dispatcher = ToolDispatcher([PersonaEditTool(tmp_path)])
    stella = Stella(
        brain=FixedToolBrain(),
        llm=RecordingLLM(),
        tool=dispatcher,
        memory=InMemoryMemory(),
    )
    # Answer "yes" to the single approval prompt the drain raises.
    answers = iter(["yes"])
    run_cli(
        stella,
        input_fn=lambda prompt="": next(answers, "exit"),
        output_fn=CollectingOutput(),
        persona_proposals=store,
    )
    assert paths.addons.read_text(encoding="utf-8") == "- drier replies\n"
    assert store.pending() == []
    store.close()


# ---------------------------------------------------------------------------
# Persona snapshots and 'stella persona revert' (same tmp-dir discipline:
# everything below runs against STELLA_PERSONA_DIR, never ~/.config/stella).
# ---------------------------------------------------------------------------


def test_preset_snapshots_the_persona_it_replaces(persona_dir) -> None:
    from stella.persona import (
        PersonaPaths,
        list_persona_snapshots,
        read_persona_snapshot,
    )

    assert cli.apply_persona_preset("snark") == 0
    # A first create has nothing to keep: history stays uncreated.
    assert not (persona_dir / "history").exists()
    old = (persona_dir / "persona.md").read_bytes()
    assert cli.apply_persona_preset("terse", force=True) == 0
    paths = PersonaPaths(persona_dir)
    snapshots = list_persona_snapshots(paths)
    assert [s.source for s in snapshots] == ["preset"]
    # Labels record the write that replaced the kept version.
    assert snapshots[0].summary == "terse preset"
    assert read_persona_snapshot(paths, snapshots[0]) == old


def test_editor_session_snapshots_before_opening(
    persona_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    persona_dir.mkdir(parents=True, exist_ok=True)
    (persona_dir / "persona.md").write_bytes(b"hand written")
    monkeypatch.setattr(cli.subprocess, "run", lambda command, check: None)
    assert cli.open_persona_editor() == 0
    # An unchanged repeat visit dedupes: one snapshot, not two.
    assert cli.open_persona_editor() == 0
    files = list((persona_dir / "history").glob("persona.*.md"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"hand written"


def seed_two_replacements(persona_dir):
    from stella.persona import PersonaPaths, replace_persona_file

    paths = PersonaPaths(persona_dir)
    replace_persona_file(
        paths, "persona", b"one", source="preset", summary="first write"
    )
    replace_persona_file(
        paths, "persona", b"two", source="preset", summary="second write"
    )
    return paths


def test_revert_without_history_says_so(persona_dir) -> None:
    out = CollectingOutput()
    assert cli.run_persona_revert(output_fn=out) == 0
    assert "No persona history yet" in out.text
    assert cli.run_persona_revert(3, output_fn=out) == 2
    assert "There is no snapshot number 3." in out.text


def test_revert_lists_snapshots_newest_first(persona_dir) -> None:
    seed_two_replacements(persona_dir)
    out = CollectingOutput()
    assert cli.run_persona_revert(output_fn=out) == 0
    rows = [line for line in out.lines if line.startswith("[")]
    assert [row.split()[0] for row in rows] == ["[1]"]
    assert "second write" in rows[0]
    assert "undoable" in out.text


def test_revert_restores_a_snapshot_and_is_itself_undoable(persona_dir) -> None:
    from stella.persona import list_persona_snapshots

    paths = seed_two_replacements(persona_dir)
    out = CollectingOutput()
    assert cli.run_persona_revert(1, yes=True, output_fn=out) == 0
    assert (persona_dir / "persona.md").read_bytes() == b"one"
    assert "snapshot to restore" in out.text  # the diff was shown
    assert "bytes verified" in out.text
    # The replaced "two" is snapshotted first, so a revert-of-the-revert
    # is already in history.
    snapshots = list_persona_snapshots(paths)
    assert snapshots[0].source == "revert"
    assert "restored snapshot" in (snapshots[0].summary or "")


def test_revert_declined_or_eof_restores_nothing(persona_dir) -> None:
    seed_two_replacements(persona_dir)
    out = CollectingOutput()
    assert (
        cli.run_persona_revert(
            1, output_fn=out, input_fn=lambda prompt: "no"
        )
        == 1
    )
    assert (persona_dir / "persona.md").read_bytes() == b"two"

    def eof(prompt: str = "") -> str:
        raise EOFError

    assert cli.run_persona_revert(1, output_fn=out, input_fn=eof) == 1
    assert out.text.count("Nothing was restored.") == 2


def test_revert_of_an_identical_snapshot_is_a_noop(persona_dir) -> None:
    from stella.persona import replace_persona_file

    paths = seed_two_replacements(persona_dir)
    replace_persona_file(
        paths, "persona", b"one", source="preset", summary="third write"
    )
    # Snapshot 2 holds b"one" — the same bytes the file has now.
    out = CollectingOutput()
    assert cli.run_persona_revert(2, yes=True, output_fn=out) == 0
    assert "identical to the current" in out.text
    assert (persona_dir / "persona.md").read_bytes() == b"one"


def test_revert_refuses_an_oversized_snapshot(persona_dir) -> None:
    from stella.persona import (
        MAX_PERSONA_BYTES,
        PersonaPaths,
        replace_persona_file,
    )

    replace_persona_file(
        PersonaPaths(persona_dir), "persona", b"small", source="preset"
    )
    history = persona_dir / "history"
    history.mkdir(exist_ok=True)
    (history / "persona.20260101T120000123456Z.7.1.md").write_bytes(
        b"x" * (MAX_PERSONA_BYTES + 1)
    )
    out = CollectingOutput()
    assert cli.run_persona_revert(1, yes=True, output_fn=out) == 1
    assert "over the" in out.text
    assert (persona_dir / "persona.md").read_bytes() == b"small"


def test_revert_refuses_a_symlinked_snapshot(persona_dir) -> None:
    from stella.persona import PersonaPaths, replace_persona_file

    replace_persona_file(
        PersonaPaths(persona_dir), "persona", b"current", source="preset"
    )
    history = persona_dir / "history"
    history.mkdir(exist_ok=True)
    outside = persona_dir.parent / "outside.md"
    outside.write_bytes(b"evil")
    (history / "persona.20260101T120000123456Z.7.1.md").symlink_to(outside)
    out = CollectingOutput()
    assert cli.run_persona_revert(1, yes=True, output_fn=out) == 1
    assert "escaped the history directory" in out.text
    assert (persona_dir / "persona.md").read_bytes() == b"current"
    assert outside.read_bytes() == b"evil"


def test_revert_of_addons_leaves_persona_untouched(persona_dir) -> None:
    from stella.persona import PersonaPaths, replace_persona_file

    paths = PersonaPaths(persona_dir)
    replace_persona_file(
        paths, "persona", b"persona stays", source="preset"
    )
    replace_persona_file(
        paths, "addons", b"- short notes\n", source="approved edit"
    )
    replace_persona_file(
        paths, "addons", b"- terse notes\n", source="approved edit"
    )
    out = CollectingOutput()
    assert cli.run_persona_revert(1, yes=True, output_fn=out) == 0
    assert (persona_dir / "persona.addons.md").read_bytes() == (
        b"- short notes\n"
    )
    assert (persona_dir / "persona.md").read_bytes() == b"persona stays"


def test_main_persona_revert_subcommand_exit_codes(persona_dir) -> None:
    seed_two_replacements(persona_dir)
    with pytest.raises(SystemExit) as raised:
        cli.main(["persona", "revert"])
    assert raised.value.code == 0
    with pytest.raises(SystemExit) as raised:
        cli.main(["persona", "revert", "1", "--yes"])
    assert raised.value.code == 0
    assert (persona_dir / "persona.md").read_bytes() == b"one"
    with pytest.raises(SystemExit) as raised:
        cli.main(["persona", "revert", "42"])
    assert raised.value.code == 2
