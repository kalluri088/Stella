"""Tests for the trusted memory-management tools and their wiring."""

from stella.brain import Brain, Decision, DecisionKind
from stella.cli import format_trace, run_cli
from stella.context import Context
from stella.llm import LLMClient
from stella.memory import InMemoryMemory, MemoryItem, MemoryWriteRequest
from stella.stella import Stella, StellaResult
from stella.tools import (
    ApprovalRequest,
    MemoryAction,
    MemoryForgetTool,
    MemoryListTool,
    MemoryUpdateTool,
    MemoryWriteTool,
    RiskLevel,
    ToolApproval,
    ToolDispatcher,
)
from stella.trace import InteractionTrace, MemoryActionEvent

TEA = "The user prefers jasmine tea in the evening."
WIFI = "The user's home wifi network name is HomeNet-5G."


def make_memory() -> InMemoryMemory:
    memory = InMemoryMemory()
    memory.store(MemoryItem(content=TEA))
    memory.store(MemoryItem(content=WIFI))
    return memory


def test_memory_list_reports_stored_contents_and_read_action() -> None:
    result = MemoryListTool(make_memory()).execute({})

    assert result.success
    assert result.memory_action == MemoryAction(action="read", count=2)
    assert TEA in result.output and WIFI in result.output
    assert MemoryListTool(InMemoryMemory()).execute({}).output == (
        "No stored memories."
    )


def test_memory_list_rejects_any_arguments() -> None:
    assert MemoryListTool(InMemoryMemory()).validate_arguments({})
    assert not MemoryListTool(InMemoryMemory()).validate_arguments(
        {"query": "tea"}
    )


def test_memory_write_stores_a_new_fact_and_reports_a_write_action() -> None:
    # Dogfood blocker: the memory_write answer-field route failed ~3/4 of the
    # time with the local 4B model (wrong tool or a silent do_nothing on an
    # explicit "remember" command). The tool route gives the model the same
    # reliable reach it has for file and reminder tools.
    memory = InMemoryMemory()

    result = MemoryWriteTool(memory).execute({"content": TEA})

    assert result.success
    assert result.output == "Stored the memory."
    assert result.memory_action == MemoryAction(action="write", count=1)
    assert [item.content for item in memory.retrieve()] == [TEA]


def test_memory_write_requires_exact_non_empty_content() -> None:
    tool = MemoryWriteTool(InMemoryMemory())

    assert tool.validate_arguments({"content": "a fact"})
    assert not tool.validate_arguments({"content": ""})
    assert not tool.validate_arguments({"content": "   "})
    assert not tool.validate_arguments({})
    assert not tool.validate_arguments({"query": "a", "content": "b"})
    assert not tool.validate_arguments({"content": 7})
    assert tool.risk_level is RiskLevel.DANGEROUS


def test_memory_write_requires_approval_and_denial_stores_nothing() -> None:
    memory = InMemoryMemory()
    dispatcher = ToolDispatcher([MemoryWriteTool(memory)])
    arguments = {"content": TEA}

    assert dispatcher.requires_approval("memory_write") is True

    refused = dispatcher.execute("memory_write", arguments)
    assert refused.success is False
    assert refused.output == "Approval required."
    assert memory.retrieve() == []

    approval = ToolApproval(
        ApprovalRequest("memory_write", dict(arguments)), True
    )
    granted = dispatcher.execute("memory_write", arguments, approval)
    assert granted.success is True
    assert [item.content for item in memory.retrieve()] == [TEA]


def test_memory_update_replaces_only_the_best_match() -> None:
    memory = make_memory()
    target = memory.retrieve("jasmine tea")[0]

    result = MemoryUpdateTool(memory).execute(
        {"query": "jasmine tea", "content": "The user prefers green tea."}
    )

    assert result.success
    assert result.memory_action == MemoryAction(
        action="update", count=1, memory_id=target.id
    )
    contents = [item.content for item in memory.retrieve()]
    assert "The user prefers green tea." in contents
    assert TEA not in contents
    assert WIFI in contents


def test_memory_update_no_match_points_a_new_fact_at_memory_write() -> None:
    # Dogfood finding: models routed first-time facts to memory_update and
    # the bare no-match message gave them no way out of the retry loop.
    result = MemoryUpdateTool(InMemoryMemory()).execute(
        {"query": "jasmine tea", "content": "The user prefers jasmine tea."}
    )

    assert result.success is False
    assert "No stored memory matches" in result.output
    assert "memory_write" in result.output


def test_memory_update_requires_exact_non_empty_arguments() -> None:
    tool = MemoryUpdateTool(InMemoryMemory())

    assert tool.validate_arguments({"query": "tea", "content": "new"})
    assert not tool.validate_arguments({"query": "tea"})
    assert not tool.validate_arguments({"query": "", "content": "new"})
    assert not tool.validate_arguments(
        {"query": "tea", "content": "new", "extra": 1}
    )


def test_memory_update_requires_dispatcher_approval() -> None:
    memory = make_memory()
    dispatcher = ToolDispatcher([MemoryUpdateTool(memory)])
    arguments = {"query": "jasmine tea", "content": "green tea"}

    assert dispatcher.risk_level("memory_update") is RiskLevel.DANGEROUS
    assert dispatcher.requires_approval("memory_update") is True

    refused = dispatcher.execute("memory_update", arguments)
    assert refused.success is False
    assert refused.output == "Approval required."
    assert [item.content for item in memory.retrieve()] == [TEA, WIFI]

    approval = ToolApproval(
        ApprovalRequest("memory_update", dict(arguments)), True
    )
    granted = dispatcher.execute("memory_update", arguments, approval)
    assert granted.success is True
    assert "green tea" in [item.content for item in memory.retrieve()]


def test_memory_update_without_a_match_fails() -> None:
    result = MemoryUpdateTool(make_memory()).execute(
        {"query": "piano practice", "content": "The user plays piano."}
    )

    assert not result.success
    assert result.memory_action == MemoryAction(action="update", count=0)


def test_memory_tool_outputs_omit_internal_database_ids() -> None:
    memory = make_memory()
    first_id, second_id = (item.id for item in memory.retrieve())

    listed = MemoryListTool(memory).execute({})
    assert listed.output == f"{TEA}\n{WIFI}"

    updated = MemoryUpdateTool(memory).execute(
        {"query": "jasmine tea", "content": "The user prefers green tea."}
    )
    assert updated.output == "Updated the matching memory."
    # The trusted audit channel still records which row changed.
    assert updated.memory_action.memory_id == first_id
    assert str(second_id) not in listed.output


def test_memory_update_discloses_ambiguous_matches() -> None:
    memory = InMemoryMemory()
    memory.store(MemoryItem(content="The user prefers jasmine tea at night."))
    memory.store(MemoryItem(content="The user prefers tea with honey."))

    result = MemoryUpdateTool(memory).execute(
        {"query": "prefers tea", "content": "The user prefers green tea."}
    )

    assert result.success
    assert result.memory_action.count == 1
    assert result.output == (
        "Updated the best match among 2 matching memories."
    )


def test_memory_forget_is_dangerous_and_deletes_only_matches() -> None:
    memory = make_memory()
    tool = MemoryForgetTool(memory)

    assert tool.risk_level is RiskLevel.DANGEROUS
    result = tool.execute({"query": "jasmine tea"})

    assert result.success
    assert result.memory_action == MemoryAction(action="delete", count=1)
    assert [item.content for item in memory.retrieve()] == [WIFI]
    assert not tool.execute({"query": "jasmine tea"}).success


class ScriptedBrain(Brain):
    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = decisions

    def decide(self, context: Context) -> Decision:
        return self.decisions.pop(0)


class FinalLLM(LLMClient):
    def chat(self, messages) -> str:
        return "done"


def build_stella(
    memory: InMemoryMemory, brain: Brain
) -> Stella:
    tools = ToolDispatcher(
        [
            MemoryListTool(memory),
            MemoryWriteTool(memory),
            MemoryUpdateTool(memory),
            MemoryForgetTool(memory),
        ]
    )
    return Stella(brain, FinalLLM(), tools, memory, max_tool_steps=2)


def test_process_records_memory_action_trace_event() -> None:
    memory = make_memory()
    stella = build_stella(
        memory,
        ScriptedBrain(
            [
                Decision(DecisionKind.TOOL, capability="memory_list"),
                Decision(DecisionKind.ANSWER, content="listed"),
            ]
        ),
    )

    result = stella.process(Context(user_input="what do you remember?"))

    events = [
        event
        for event in result.interaction_trace.events
        if isinstance(event, MemoryActionEvent)
    ]
    assert events == [MemoryActionEvent(action="read", count=2)]


def test_memory_tool_result_cannot_also_trigger_outcome_memory_write() -> None:
    memory = make_memory()
    stella = build_stella(
        memory,
        ScriptedBrain(
            [
                Decision(
                    DecisionKind.TOOL,
                    capability="memory_list",
                    memory_write=MemoryWriteRequest(
                        item=MemoryItem(content="duplicate fact")
                    ),
                ),
                Decision(DecisionKind.ANSWER, content="listed"),
            ]
        ),
    )

    result = stella.process(Context(user_input="what do you remember?"))

    assert result.memory_write is None
    assert [item.content for item in memory.retrieve()] == [TEA, WIFI]


def test_cli_trace_renders_memory_action_lines() -> None:
    trace = InteractionTrace()
    trace.record(MemoryActionEvent(action="delete", count=1))
    result = StellaResult(
        decision=Decision(DecisionKind.ANSWER, "gone"),
        response="gone",
        interaction_trace=trace,
    )

    assert format_trace(result) == ["  memory    delete 1"]


def test_cli_forget_flow_deletes_after_approval() -> None:
    memory = make_memory()
    stella = build_stella(
        memory,
        ScriptedBrain(
            [
                Decision(
                    DecisionKind.TOOL,
                    capability="memory_forget",
                    arguments={"query": "jasmine tea"},
                ),
                Decision(DecisionKind.ANSWER, content="forgotten"),
            ]
        ),
    )
    outputs: list[str] = []
    inputs = iter(["forget the tea preference", "yes", "exit"])

    run_cli(
        stella,
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        trace=True,
    )

    assert [item.content for item in memory.retrieve()] == [WIFI]
    assert any(
        output.startswith("Stella would like to") for output in outputs
    )
    assert "  memory    delete 1" in outputs
    assert "Stella: done" in outputs


def test_cli_remember_flow_stores_after_approval() -> None:
    # Release-blocker regression: explicit "remember" commands previously
    # depended only on the answer-field route, which the local 4B model
    # dropped about three quarters of the time. The tool route must store
    # the fact through the same approval broker as every dangerous action.
    memory = InMemoryMemory()
    stella = build_stella(
        memory,
        ScriptedBrain(
            [
                Decision(
                    DecisionKind.TOOL,
                    capability="memory_write",
                    arguments={"content": TEA},
                ),
                Decision(DecisionKind.ANSWER, content="remembered"),
            ]
        ),
    )
    outputs: list[str] = []
    inputs = iter(["remember the tea preference", "yes", "exit"])

    run_cli(
        stella,
        input_fn=lambda _: next(inputs),
        output_fn=outputs.append,
        trace=True,
    )

    assert [item.content for item in memory.retrieve()] == [TEA]
    assert any(
        "remember this as a permanent fact" in output for output in outputs
    )
    assert "  memory    write 1" in outputs
    assert "Stella: done" in outputs
