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


class TerminalTool(RecordingTool):
    """Display-ready read-only tool: output may be shown verbatim."""

    name = "clock"
    description = "Returns the time as user-facing text."
    terminal = True


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


def tool_decision(
    capability: str, final: bool, arguments: dict[str, object] | None = None
) -> Decision:
    return Decision(
        DecisionKind.TOOL,
        capability=capability,
        arguments=arguments or {},
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
            tool_decision("record", False, {"step": 1}),
            tool_decision("record", False, {"step": 2}),
            Decision(DecisionKind.ANSWER, content="enough"),
        ]
    )
    stella, tool, _llm = build(brain)

    result = run(stella)

    assert brain.decisions == []  # a fresh decision followed every tool
    # distinct calls to the same tool chained across steps
    assert tool.arguments == [{"step": 1}, {"step": 2}]
    assert [step.decision.kind for step in result.step_trace] == [
        DecisionKind.TOOL,
        DecisionKind.TOOL,
        DecisionKind.ANSWER,
    ]
    # re-decision answered with final content, so no synthesis call was spent
    assert result.response == "enough"


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
    # the runtime states the denial outcome directly; the model never
    # gets a chance to phrase (or fabricate) it
    assert len(llm.messages) == 0
    assert result.response == (
        "The action was not approved, so it was not performed. "
        "Nothing was changed."
    )


def test_duplicate_tool_call_in_one_turn_executes_once_and_synthesizes() -> (
    None
):
    brain = SequenceBrain(
        [
            tool_decision("record", False, {"text": "same"}),
            tool_decision("record", False, {"text": "same"}),
        ]
    )
    stella, tool, llm = build(brain)

    result = run(stella)

    # the repeat proposal must not re-execute or consume another decision
    assert tool.arguments == [{"text": "same"}]
    assert brain.decisions == []
    assert len(llm.messages) == 1  # synthesis from the existing observation
    assert result.response == "synthesized answer"
    assert result.decision.kind is DecisionKind.ANSWER
    assert [step.decision.kind for step in result.step_trace] == [
        DecisionKind.TOOL,
        DecisionKind.ANSWER,
    ]


def test_duplicate_dangerous_call_never_prompts_a_second_approval() -> None:
    calls: list[str] = []

    def provider(request):
        calls.append(request.capability)
        return ToolApproval(request, True)

    brain = SequenceBrain(
        [
            tool_decision("dangerous_action", False, {"x": 1}),
            tool_decision("dangerous_action", False, {"x": 1}),
        ]
    )
    tool = DangerousTool()
    stella, _tool, _llm = build(brain, tool=tool)
    stella.approval_provider = provider

    result = run(stella)

    # one action, one approval prompt; the verified success is not overwritten
    assert calls == ["dangerous_action"]
    assert tool.arguments == [{"x": 1}]
    assert result.tool_result is not None
    assert result.tool_result.success is True
    assert result.response == "synthesized answer"


def test_unregistered_capability_after_a_step_cannot_overwrite_the_outcome() -> (
    None
):
    # UI dogfood regression: a verified memory write followed by a hallucinated
    # second capability must not report the turn as "Tool capability
    # unavailable. ✗ failed" while the approved action actually succeeded.
    brain = SequenceBrain(
        [
            tool_decision("record", False, {"step": 1}),
            tool_decision("nonexistent_capability", False),
        ]
    )
    stella, tool, llm = build(brain)

    result = run(stella)

    # the unregistered proposal never reaches the dispatcher at all
    assert tool.arguments == [{"step": 1}]
    assert brain.decisions == []
    assert len(llm.messages) == 1  # synthesis from the real observation
    assert result.tool_result is not None
    assert result.tool_result.success is True
    assert result.response == "synthesized answer"


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


def test_reserved_tool_final_stripped_from_native_tool_calls() -> None:
    from stella.llm import LLMToolCall

    armed = LLMBrain._decision_from_tool_call(
        LLMToolCall(
            name="system_info",
            arguments={"kind": "hostname", "tool_final": True},
        )
    )
    assert armed.tool_final is True
    assert armed.arguments == {"kind": "hostname"}

    unarmed = LLMBrain._decision_from_tool_call(
        LLMToolCall(
            name="system_info",
            arguments={"kind": "hostname", "tool_final": "yes"},
        )
    )
    assert unarmed.tool_final is False
    assert unarmed.arguments == {"kind": "hostname"}

    plain = LLMBrain._decision_from_tool_call(
        LLMToolCall(name="echo", arguments={"text": "hi"})
    )
    assert plain.tool_final is False
    assert plain.arguments == {"text": "hi"}


def test_tool_call_channel_drives_fast_path_end_to_end() -> None:
    from stella.llm import LLMResponse, LLMToolCall

    class ToolCallLLM(LLMClient):
        def __init__(self):
            self.chat_calls = 0

        def chat(self, messages):
            self.chat_calls += 1
            return "synthesized from tool call"

        def chat_with_tools(self, messages, tools, tool_choice=None):
            return LLMResponse(
                tool_calls=(
                    LLMToolCall(
                        name="echo",
                        arguments={"message": "hi", "tool_final": True},
                    ),
                )
            )

    llm = ToolCallLLM()
    tools = ToolDispatcher([EchoTool()])
    brain = LLMBrain(llm, tools)
    stella = Stella(brain, llm, tools, InMemoryMemory(), max_tool_steps=2)

    result = stella.process(Context(user_input="echo hi"))

    assert result.response == "synthesized from tool call"
    assert llm.chat_calls == 1  # synthesis only; no re-decision happened
    assert result.decision.kind is DecisionKind.ANSWER
    # the reserved key never reached the tool's strict argument validation
    assert result.tool_result is not None
    assert result.tool_result.success is True


def test_fast_path_only_applies_to_the_first_tool_call() -> None:
    brain = SequenceBrain(
        [
            tool_decision("record", False, {"step": 1}),
            tool_decision("record", True, {"step": 2}),
            Decision(DecisionKind.ANSWER, content="after re-decision"),
        ]
    )
    stella, tool, _llm = build(brain, max_tool_steps=2)

    result = run(stella)

    # the second tool's tool_final must not skip the re-decision
    assert brain.decisions == []
    assert len(tool.arguments) == 2
    assert result.decision.kind is DecisionKind.ANSWER
    assert result.response == "after re-decision"


def test_single_step_configuration_keeps_its_existing_path() -> None:
    brain = SequenceBrain([tool_decision("record", True)])
    stella, _tool, llm = build(brain, max_tool_steps=1)

    result = run(stella)

    # max_tool_steps==1 returns with the TOOL decision as metadata, as before
    assert result.decision.kind is DecisionKind.TOOL
    assert len(llm.messages) == 1


# ---------------------------------------------------------------------------
# terminal-tool direct render (report 35 target 2): the tool's own output is
# the response, so the second LLM call leaves the turn entirely
# ---------------------------------------------------------------------------


def test_terminal_tool_final_turn_makes_no_second_llm_call() -> None:
    brain = SequenceBrain([tool_decision("clock", True)])
    stella, tool, llm = build(brain, tool=TerminalTool())

    result = run(stella)

    assert tool.arguments == [{}]
    assert llm.messages == []  # no synthesis, no re-decision: one call total
    assert result.response == "tool output"  # verbatim, not rephrased
    assert result.decision.kind is DecisionKind.ANSWER
    assert [step.decision.kind for step in result.step_trace] == [
        DecisionKind.TOOL,
        DecisionKind.ANSWER,
    ]
    kinds = [
        event.kind
        for event in result.interaction_trace.events
        if type(event).__name__ == "DecisionEvent"
    ]
    assert kinds == ["tool", "answer"]


def test_terminal_tool_final_works_with_a_single_step_budget() -> None:
    brain = SequenceBrain([tool_decision("clock", True)])
    stella, _tool, llm = build(brain, tool=TerminalTool(), max_tool_steps=1)

    result = run(stella)

    assert llm.messages == []
    assert result.decision.kind is DecisionKind.ANSWER
    assert result.response == "tool output"


def test_failed_terminal_observation_still_synthesizes_honestly() -> None:
    brain = SequenceBrain([tool_decision("clock", True)])
    failing = TerminalTool(result=ToolResult(success=False, output="clock dead"))
    stella, _tool, llm = build(brain, tool=failing)

    result = run(stella)

    # verbatim rendering is only for successes; failures keep the synthesis
    # path so the model reports them with its full context
    assert len(llm.messages) == 1
    payload = json.loads(llm.messages[0][-1].content)
    assert payload["tool_observations"][0]["success"] is False
    assert result.decision.kind is DecisionKind.ANSWER


def test_terminal_tool_without_tool_final_keeps_the_normal_flow() -> None:
    brain = SequenceBrain(
        [
            tool_decision("clock", False),
            Decision(DecisionKind.ANSWER, content="it is late"),
        ]
    )
    stella, _tool, llm = build(brain, tool=TerminalTool())

    result = run(stella)

    assert brain.decisions == []  # re-decision happened as usual
    assert llm.messages == []
    assert result.response == "it is late"


def test_non_terminal_tool_final_still_synthesizes() -> None:
    # The registry-side guarantee: only tools that opted into `terminal`
    # get verbatim rendering; everything else keeps the synthesis path.
    brain = SequenceBrain([tool_decision("echo", True)])
    stella, _tool, llm = build(brain, tool=RecordingTool())

    run(stella)

    assert len(llm.messages) == 1


def test_dispatcher_reports_terminal_capabilities() -> None:
    dispatcher = ToolDispatcher([TerminalTool(), RecordingTool(), EchoTool()])
    assert dispatcher.is_terminal("clock") is True
    assert dispatcher.is_terminal("record") is False
    assert dispatcher.is_terminal("echo") is False
    assert dispatcher.is_terminal("nonexistent") is False
    assert dispatcher.is_terminal(None) is False


def test_real_display_tools_declare_themselves_terminal() -> None:
    from stella.reminders import InMemoryReminderStore
    from stella.tools import (
        DateTimeTool,
        ReminderListTool,
        SystemInfoTool,
    )

    dispatcher = ToolDispatcher(
        [
            DateTimeTool(),
            SystemInfoTool(),
            ReminderListTool(InMemoryReminderStore()),
        ]
    )
    assert [dispatcher.is_terminal(name) for name in
            ("datetime", "system_info", "reminder_list")] == [True] * 3
    # the default is opt-in: no tool is terminal unless it says so
    from stella.tools import MemoryListTool

    assert MemoryListTool(InMemoryMemory()).terminal is False
