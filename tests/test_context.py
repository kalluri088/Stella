from stella.context import (
    MAX_CONVERSATION_HISTORY,
    MAX_RETRIEVED_MEMORIES,
    MAX_TOOL_OBSERVATIONS,
    MAX_TOOL_OUTPUT_CHARS,
    Context,
    ToolObservation,
    select_conversation_history,
    select_retrieved_memories,
    select_tool_observations,
)
from stella.llm import Message
from stella.memory import MemoryItem, MemoryScope, MemoryType


def test_context_stores_user_input_and_conversation_history() -> None:
    history = [Message(role="user", content="Earlier question")]

    context = Context(user_input="Current question", conversation_history=history)

    assert context.user_input == "Current question"
    assert context.conversation_history == history


def test_context_defaults_to_empty_conversation_history() -> None:
    first = Context(user_input="First")
    second = Context(user_input="Second")

    assert first.conversation_history == []
    assert second.conversation_history == []
    assert first.conversation_history is not second.conversation_history


def test_context_selects_recent_history_with_stable_limit() -> None:
    history = [Message(role="user", content=str(index)) for index in range(25)]

    selected = select_conversation_history(history)

    assert len(selected) == MAX_CONVERSATION_HISTORY
    assert [message.content for message in selected] == [
        str(index) for index in range(5, 25)
    ]


def test_context_selects_latest_and_recent_failures() -> None:
    observations = [
        ToolObservation("tool", {"step": index}, index != 1, f"output-{index}")
        for index in range(MAX_TOOL_OBSERVATIONS + 3)
    ]
    observations[7] = ToolObservation("tool", {"step": 7}, False, "failed")

    selected = select_tool_observations(observations)

    assert len(selected) == MAX_TOOL_OBSERVATIONS
    assert [observation.arguments["step"] for observation in selected] == [
        1,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
    ]
    assert selected[-1].arguments["step"] == 10
    assert any(not observation.success for observation in selected)


def test_context_truncates_tool_output_deterministically() -> None:
    output = "x" * (MAX_TOOL_OUTPUT_CHARS + 10)
    selected = select_tool_observations(
        [ToolObservation("tool", {}, True, output)]
    )

    assert len(selected[0].output) == MAX_TOOL_OUTPUT_CHARS
    assert selected[0].output.endswith("... [tool output truncated]")


def test_select_retrieved_memories_keeps_most_relevant_within_limit() -> None:
    query = "weather forecast tomorrow morning"
    strong = [
        MemoryItem(content=f"weather forecast tomorrow morning note {index}")
        for index in range(3)
    ]
    weak = [
        MemoryItem(content=f"unrelated weather tidbit {index}")
        for index in range(4)
    ]

    selected = select_retrieved_memories(
        [*weak[:2], *strong, *weak[2:]], query
    )

    assert len(selected) == MAX_RETRIEVED_MEMORIES
    assert selected[:3] == strong
    assert selected[3:] == weak[:2]


def test_select_retrieved_memories_preserves_existing_order_for_ties() -> None:
    items = [
        MemoryItem(content=f"recall memory {index}")
        for index in range(MAX_RETRIEVED_MEMORIES + 2)
    ]

    selected = select_retrieved_memories(items, "recall memories")

    assert selected == items[:MAX_RETRIEVED_MEMORIES]


def test_select_retrieved_memories_leaves_small_results_unchanged() -> None:
    items = [MemoryItem(content=f"memory {index}") for index in range(3)]

    assert select_retrieved_memories(items, "anything") == items


def test_select_retrieved_memories_preserves_memory_type_and_scope() -> None:
    items = [
        MemoryItem(
            content="jasmine tea calm", memory_type=MemoryType.EPISODIC
        ),
        MemoryItem(
            content="jasmine tea loud", scope=MemoryScope.STELLA
        ),
    ]

    selected = select_retrieved_memories(items, "jasmine tea")

    assert selected == items
    assert selected[0].memory_type is MemoryType.EPISODIC
    assert selected[1].scope is MemoryScope.STELLA
