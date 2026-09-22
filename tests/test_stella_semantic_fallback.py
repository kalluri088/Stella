"""Focused tests for lexical-first semantic fallback in Stella.process()."""

from stella.brain import Brain, Decision, DecisionKind
from stella.context import Context
from stella.llm import LLMClient, Message
from stella.memory import InMemoryMemory, MemoryItem, MemoryScope, MemoryType
from stella.semantic_memory import (
    LocalHashEmbeddingProvider,
    SemanticMatch,
    SemanticRetriever,
    SQLiteSemanticIndex,
)
from stella.stella import Stella
from stella.tools import Tool, ToolResult


class FixedBrain(Brain):
    def decide(self, context: Context) -> Decision:
        return Decision(DecisionKind.ANSWER)


class RecordingLLM(LLMClient):
    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        return "answer"


class RecordingTool(Tool):
    name = "record"
    description = "Test tool."

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return True

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        return ToolResult(success=True, output="tool output")


class CountingRetriever:
    """Wrap a real retriever and record fallback calls."""

    def __init__(self, inner: SemanticRetriever) -> None:
        self.inner = inner
        self.calls: list[tuple[str, int | None]] = []

    def retrieve(
        self, query: str, memory_type: MemoryType | None = None, limit: int | None = None
    ) -> list[SemanticMatch]:
        self.calls.append((query, limit))
        return self.inner.retrieve(query, memory_type, limit)


class StubRetriever:
    """Return a fixed match list and record the requested limit."""

    def __init__(self, matches: list[SemanticMatch]) -> None:
        self.matches = matches
        self.calls: list[tuple[str, int | None]] = []

    def retrieve(
        self, query: str, memory_type: MemoryType | None = None, limit: int | None = None
    ) -> list[SemanticMatch]:
        self.calls.append((query, limit))
        return list(self.matches)


def make_memory_with(content: str) -> tuple[InMemoryMemory, MemoryItem]:
    memory = InMemoryMemory()
    memory.store(MemoryItem(content=content))
    (item,) = memory.retrieve()
    return memory, item


def make_semantic_retriever(
    tmp_path, contents: list[str]
) -> SemanticRetriever:
    index = SQLiteSemanticIndex(tmp_path / "semantic.db", MemoryScope.USER)
    retriever = SemanticRetriever(LocalHashEmbeddingProvider(), index)
    for position, content in enumerate(contents, start=1):
        retriever.index_memory(
            MemoryItem(content=content, id=position, scope=MemoryScope.USER)
        )
    return retriever


def make_stella(memory, semantic_retriever=None) -> Stella:
    return Stella(
        FixedBrain(),
        RecordingLLM(),
        RecordingTool(),
        memory,
        semantic_retriever=semantic_retriever,
    )


def test_lexical_results_prevent_semantic_fallback(tmp_path) -> None:
    memory, item = make_memory_with("The user prefers tea in the morning.")
    inner = make_semantic_retriever(tmp_path, ["The user prefers tea."])
    retriever = CountingRetriever(inner)
    stella = make_stella(memory, retriever)

    try:
        result = stella.process(Context(user_input="tea in the morning"))
    finally:
        inner.index.close()

    assert result.retrieved_memories == [item]
    assert retriever.calls == []


def test_lexical_miss_triggers_semantic_fallback(tmp_path) -> None:
    memory = InMemoryMemory()
    inner = make_semantic_retriever(
        tmp_path, ["The user prefers concise explanations."]
    )
    stella = make_stella(memory, inner)

    try:
        result = stella.process(Context(user_input="Does the user like brief answers?"))
    finally:
        inner.index.close()

    assert len(result.retrieved_memories) == 1
    assert (
        result.retrieved_memories[0].content
        == "The user prefers concise explanations."
    )


def test_semantic_fallback_returns_at_most_one_memory(tmp_path) -> None:
    memory = InMemoryMemory()
    inner = make_semantic_retriever(
        tmp_path,
        [
            "The user prefers tea in the morning.",
            "The user prefers coffee in the morning.",
            "The user prefers tea in the evening.",
        ],
    )
    stella = make_stella(memory, inner)

    try:
        result = stella.process(
            Context(user_input="What does the user prefer in the morning?")
        )
    finally:
        inner.index.close()

    assert len(result.retrieved_memories) == 1


def test_semantic_cap_holds_even_if_retriever_returns_more() -> None:
    memory = InMemoryMemory()
    matches = [
        SemanticMatch(
            item=MemoryItem(content=f"Semantic candidate {i}", id=i),
            score=0.9 - i * 0.01,
        )
        for i in (1, 2, 3)
    ]
    stub = StubRetriever(matches)
    stella = make_stella(memory, stub)

    result = stella.process(Context(user_input="lexically unmatched query xyz"))

    assert stub.calls == [("lexically unmatched query xyz", 1)]
    assert len(result.retrieved_memories) == 1
    assert result.retrieved_memories[0].content == "Semantic candidate 1"


def test_no_semantic_result_leaves_retrieval_empty(tmp_path) -> None:
    memory = InMemoryMemory()
    inner = make_semantic_retriever(tmp_path, [])
    stella = make_stella(memory, inner)

    try:
        result = stella.process(Context(user_input="zzzqqq nomatch phrase"))
    finally:
        inner.index.close()

    # Empty semantic index yields no matches; fallback stays empty.
    assert result.retrieved_memories == []


def test_scope_and_type_isolation_remains_intact(tmp_path) -> None:
    memory = InMemoryMemory()
    index = SQLiteSemanticIndex(tmp_path / "scoped.db", MemoryScope.USER)
    retriever = SemanticRetriever(LocalHashEmbeddingProvider(), index)
    user_item = MemoryItem(
        content="The user prefers concise explanations.",
        id=1,
        memory_type=MemoryType.EPISODIC,
        scope=MemoryScope.USER,
    )
    assert retriever.index_memory(user_item) is True
    # Cross-scope items are rejected by the scoped index boundary.
    assert (
        retriever.index_memory(
            MemoryItem(content="Stella fact", id=2, scope=MemoryScope.STELLA)
        )
        is False
    )
    stella = make_stella(memory, retriever)

    try:
        result = stella.process(Context(user_input="Does the user like brief answers?"))
    finally:
        index.close()

    assert len(result.retrieved_memories) == 1
    assert result.retrieved_memories[0].scope is MemoryScope.USER
    assert result.retrieved_memories[0].memory_type is MemoryType.EPISODIC


def test_behavior_without_semantic_retriever_is_unchanged() -> None:
    memory, item = make_memory_with("The user prefers tea in the morning.")
    stella = make_stella(memory)

    assert stella.semantic_retriever is None
    assert stella.process(Context(user_input="tea in the morning")).retrieved_memories == [
        item
    ]
    assert stella.process(Context(user_input="zzzqqq nomatch phrase")).retrieved_memories == []
