from stella.brain import Brain, Decision, DecisionKind
from stella.context import Context
from stella.llm import LLMClient, Message
from stella.memory import InMemoryMemory, MemoryItem, MemoryWriteRequest
from stella.stella import Stella
from stella.tools import RiskLevel, Tool, ToolDispatcher, ToolResult
from stella.trace import (
    ApprovalEvent,
    DecisionEvent,
    FinalResponseEvent,
    InputReceivedEvent,
    MemoryRetrievedEvent,
    MemoryWriteEvent,
    ToolResultEvent,
)


class RecordingLLM(LLMClient):
    def __init__(self, response: str = "final response") -> None:
        self.response = response
        self.messages: list[list[Message | dict[str, str]]] = []

    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        self.messages.append(messages)
        return self.response


class SequenceBrain(Brain):
    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = list(decisions)

    def decide(self, context: Context) -> Decision:
        del context
        return self.decisions.pop(0)


class FixedBrain(Brain):
    answer_content_is_final = True

    def __init__(self, decision: Decision) -> None:
        self.decision = decision

    def decide(self, context: Context) -> Decision:
        del context
        return self.decision


class TraceTool(Tool):
    def __init__(self, name: str = "trace_tool", success: bool = True) -> None:
        self._name = name
        self.success = success
        self.calls: list[dict[str, object]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "A deterministic trace test tool."

    @property
    def argument_schema(self) -> dict[str, object]:
        return {"value": "string"}

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return set(arguments) == {"value"} and isinstance(
            arguments["value"], str
        )

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        self.calls.append(dict(arguments))
        return ToolResult(self.success, "trace tool output")


class DangerousTraceTool(TraceTool):
    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.DANGEROUS


def _event_names(result) -> list[str]:
    return [type(event).__name__ for event in result.interaction_trace.events]


def test_trace_records_context_retrieval_decision_response_in_order() -> None:
    memory = InMemoryMemory()
    memory.store(
        MemoryItem("The user prefers concise technical explanations.")
    )
    result = Stella(
        FixedBrain(Decision(DecisionKind.ANSWER, content="Concise.")),
        RecordingLLM(),
        ToolDispatcher([]),
        memory,
    ).process(
        Context(
            user_input=(
                "What do you know about my technical explanation preference?"
            )
        )
    )

    trace = result.interaction_trace
    assert trace is not None
    assert _event_names(result) == [
        "InputReceivedEvent",
        "MemoryRetrievedEvent",
        "DecisionEvent",
        "FinalResponseEvent",
        "MemoryWriteEvent",
    ]
    assert isinstance(trace.events[0], InputReceivedEvent)
    assert isinstance(trace.events[1], MemoryRetrievedEvent)
    assert trace.events[1].count == 1
    assert isinstance(trace.events[2], DecisionEvent)
    assert trace.events[2].kind == "answer"
    assert isinstance(trace.events[3], FinalResponseEvent)
    assert trace.events[3].response_chars == len("Concise.")


def test_trace_records_failed_tool_result_without_exposing_output() -> None:
    tool = TraceTool(success=False)
    result = Stella(
        SequenceBrain(
            [
                Decision(
                    DecisionKind.TOOL,
                    capability="trace_tool",
                    arguments={"value": "secret argument"},
                ),
                Decision(DecisionKind.ANSWER, content="It failed."),
            ]
        ),
        RecordingLLM(),
        ToolDispatcher([tool]),
        InMemoryMemory(),
        max_tool_steps=2,
    ).process(Context(user_input="Run the tool."))

    trace = result.interaction_trace
    assert trace is not None
    tool_event = next(
        event for event in trace.events if isinstance(event, ToolResultEvent)
    )
    assert tool_event.success is False
    assert tool_event.output_chars == len("trace tool output")
    assert tool_event.argument_keys == ("value",)
    assert "secret argument" not in repr(trace)


def test_trace_preserves_order_across_multiple_tool_steps() -> None:
    first = TraceTool(name="first")
    second = TraceTool(name="second")
    result = Stella(
        SequenceBrain(
            [
                Decision(
                    DecisionKind.TOOL,
                    capability="first",
                    arguments={"value": "one"},
                ),
                Decision(
                    DecisionKind.TOOL,
                    capability="second",
                    arguments={"value": "two"},
                ),
                Decision(DecisionKind.ANSWER, content="Complete."),
            ]
        ),
        RecordingLLM(),
        ToolDispatcher([first, second]),
        InMemoryMemory(),
        max_tool_steps=3,
    ).process(Context(user_input="Run both tools."))

    trace = result.interaction_trace
    assert trace is not None
    assert _event_names(result) == [
        "InputReceivedEvent",
        "MemoryRetrievedEvent",
        "DecisionEvent",
        "ToolResultEvent",
        "DecisionEvent",
        "ToolResultEvent",
        "DecisionEvent",
        "FinalResponseEvent",
        "MemoryWriteEvent",
    ]
    assert [
        event.capability
        for event in trace.events
        if isinstance(event, DecisionEvent)
    ] == ["first", "second", None]


def test_trace_records_approval_decision_and_failed_dispatch() -> None:
    tool = DangerousTraceTool()
    result = Stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                capability="trace_tool",
                arguments={"value": "dangerous"},
            )
        ),
        RecordingLLM(),
        ToolDispatcher([tool]),
        InMemoryMemory(),
        max_tool_steps=1,
    ).process(Context(user_input="Run the dangerous tool."))

    trace = result.interaction_trace
    assert trace is not None
    approval = next(
        event for event in trace.events if isinstance(event, ApprovalEvent)
    )
    tool_result = next(
        event for event in trace.events if isinstance(event, ToolResultEvent)
    )
    assert approval.approved is False
    assert tool_result.success is False
    assert tool.calls == []


def test_trace_records_successful_memory_write_without_content() -> None:
    memory = InMemoryMemory()
    memory_request = MemoryWriteRequest(MemoryItem("User prefers concise output."))
    result = Stella(
        FixedBrain(
            Decision(
                DecisionKind.ANSWER,
                content="I will remember that.",
                memory_write=memory_request,
            )
        ),
        RecordingLLM(),
        ToolDispatcher([]),
        memory,
    ).process(Context(user_input="Remember that I prefer concise output."))

    trace = result.interaction_trace
    assert trace is not None
    write = next(
        event for event in trace.events if isinstance(event, MemoryWriteEvent)
    )
    assert isinstance(write, MemoryWriteEvent)
    assert write.proposed is True
    assert write.written is True
    assert write.content_chars == len(memory_request.item.content)
    assert memory.retrieve() == [memory_request.item]
    assert memory_request.item.content not in repr(trace)
    assert trace.events.index(write) < len(trace.events) - 1
