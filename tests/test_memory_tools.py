"""Tests for the trusted memory-management tools and their wiring."""

from stella.brain import Brain, Decision, DecisionKind
from stella.cli import format_trace, run_cli
from stella.context import Context
from stella.llm import LLMClient
from stella.memory import InMemoryMemory, MemoryItem, MemoryWriteRequest
from stella.stella import Stella, StellaResult
from stella.tools import (
    MemoryAction,
    MemoryForgetTool,
    MemoryListTool,
    MemoryUpdateTool,
    RiskLevel,
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


def test_memory_update_requires_exact_non_empty_arguments() -> None:
    tool = MemoryUpdateTool(InMemoryMemory())

    assert tool.validate_arguments({"query": "tea", "content": "new"})
    assert not tool.validate_arguments({"query": "tea"})
    assert not tool.validate_arguments({"query": "", "content": "new"})
    assert not tool.validate_arguments(
        {"query": "tea", "content": "new", "extra": 1}
    )


def test_memory_update_without_a_match_fails() -> None:
    result = MemoryUpdateTool(make_memory()).execute(
        {"query": "piano practice", "content": "The user plays piano."}
    )

    assert not result.success
    assert result.memory_action == MemoryAction(action="update", count=0)


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
