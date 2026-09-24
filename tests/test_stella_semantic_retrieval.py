"""Focused tests for fused, keyword-dominant semantic recall in Stella.process()."""

from stella.brain import Brain, Decision, DecisionKind
from stella.context import Context
from stella.llm import LLMClient, Message
from stella.memory import (
    InMemoryMemory,
    MemoryItem,
    MemoryScope,
    MemoryType,
    MemoryWriteRequest,
    relevance_score,
)
from stella.semantic_memory import (
    LocalHashEmbeddingProvider,
    SemanticMatch,
    SemanticRetriever,
    SQLiteSemanticIndex,
)
from stella.stella import MAX_SEMANTIC_SUPPLEMENT, Stella
from stella.tools import MemoryAction, Tool, ToolDispatcher, ToolResult
from stella.trace import MemoryIndexSyncEvent


class ScriptedBrain(Brain):
    """Answers from a script and records every decision-time Context."""

    answer_content_is_final = True

    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = list(decisions)
        self.contexts: list[Context] = []

    def decide(
        self,
        context: Context,
        should_cancel=None,
    ) -> Decision:
        self.contexts.append(context)
        return self.decisions.pop(0)


class RecordingLLM(LLMClient):
    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        return "answer"


class StubRetriever:
    """Return a fixed match list (ignoring its own limit) and record calls."""

    def __init__(self, matches: list[SemanticMatch]) -> None:
        self.matches = matches
        self.index = _StubIndex()
        self.calls: list[tuple[str, int | None]] = []
        self.indexed: list[MemoryItem] = []

    def retrieve(
        self,
        query: str,
        memory_type: MemoryType | None = None,
        limit: int | None = None,
    ) -> list[SemanticMatch]:
        self.calls.append((query, limit))
        return list(self.matches)

    def index_memory(self, item: MemoryItem) -> bool:
        self.indexed.append(item)
        return True


class _StubIndex:
    scope = MemoryScope.USER

    def __init__(self) -> None:
        self.clears = 0

    def clear(self) -> bool:
        self.clears += 1
        return True


class SilentRetriever(StubRetriever):
    """Never matches, so fusion cannot interfere with sync-hook tests."""


class FailingIndex:
    scope = MemoryScope.USER

    def __init__(self) -> None:
        self.clears = 0

    def clear(self) -> bool:
        self.clears += 1
        return False


class MemoryActionTool(Tool):
    """A registered tool whose result reports one memory-store transition."""

    name = "memory_action"
    description = "Test tool reporting a memory action."

    def __init__(self, action: str, success: bool = True) -> None:
        self._action = action
        self._success = success

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return True

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        return ToolResult(
            success=self._success,
            output="memory step",
            memory_action=MemoryAction(
                action=self._action, count=1, memory_id=1
            ),
        )


def stored_keyword(content: str) -> tuple[InMemoryMemory, MemoryItem]:
    memory = InMemoryMemory()
    memory.store(MemoryItem(content=content))
    (item,) = memory.retrieve()
    return memory, item


def make_local_retriever(
    tmp_path, contents: list[str]
) -> SemanticRetriever:
    index = SQLiteSemanticIndex(tmp_path / "semantic.db", MemoryScope.USER)
    retriever = SemanticRetriever(LocalHashEmbeddingProvider(), index)
    for position, content in enumerate(contents, start=1):
        retriever.index_memory(
            MemoryItem(content=content, id=position, scope=MemoryScope.USER)
        )
    return retriever


def make_stella(memory, brain, tools=None, semantic_retriever=None) -> Stella:
    return Stella(
        brain,
        RecordingLLM(),
        tools if tools is not None else ToolDispatcher([]),
        memory,
        max_tool_steps=2,
        semantic_retriever=semantic_retriever,
    )


# ------------------------------------------------------- fusion behavior


def test_semantic_supplements_follow_keyword_matches_in_order(tmp_path) -> None:
    memory, keyword = stored_keyword("The user prefers tea in the morning.")
    extra = SemanticMatch(
        item=MemoryItem(content="The user likes green tea.", id=10),
        score=0.61,
    )
    stub = StubRetriever([extra])
    stella = make_stella(memory, ScriptedBrain([Decision(DecisionKind.ANSWER)]),
                         semantic_retriever=stub)

    result = stella.process(Context(user_input="tea in the morning"))

    # Keyword-dominant: the lexical hit keeps first place and the
    # supplement can only fill remaining space, never overtake.
    assert result.retrieved_memories == [keyword, extra.item]


def test_supplement_recall_runs_even_when_keywords_hit() -> None:
    # Fusion replaces the old fallback-only semantics: the retriever is
    # consulted on every turn it is enabled for, always bounded.
    memory, _ = stored_keyword("The user prefers tea in the morning.")
    stub = StubRetriever([])
    stella = make_stella(memory, ScriptedBrain([Decision(DecisionKind.ANSWER)]),
                         semantic_retriever=stub)

    stella.process(Context(user_input="tea in the morning"))

    assert stub.calls == [("tea in the morning", MAX_SEMANTIC_SUPPLEMENT)]


def test_supplements_are_capped_even_if_the_backend_returns_more() -> None:
    matches = [
        SemanticMatch(
            item=MemoryItem(content=f"Semantic candidate {i}", id=10 + i),
            score=0.9 - i * 0.01,
        )
        for i in (1, 2, 3)
    ]
    stub = StubRetriever(matches)
    stella = make_stella(
        InMemoryMemory(),
        ScriptedBrain([Decision(DecisionKind.ANSWER)]),
        semantic_retriever=stub,
    )

    result = stella.process(
        Context(user_input="lexically unmatched query xyz")
    )

    # The cap is enforced by Stella itself, not trusted from the backend.
    assert [item.id for item in result.retrieved_memories] == [11, 12]


def test_supplements_already_retrieved_by_keyword_are_not_duplicated() -> None:
    memory, keyword = stored_keyword("The user prefers tea in the morning.")
    same_id = SemanticMatch(
        item=MemoryItem(content="Reshaped copy.", id=keyword.id), score=0.7
    )
    exact_copy = SemanticMatch(item=keyword, score=0.6)
    stub = StubRetriever([same_id, exact_copy])
    stella = make_stella(memory, ScriptedBrain([Decision(DecisionKind.ANSWER)]),
                         semantic_retriever=stub)

    result = stella.process(Context(user_input="tea in the morning"))

    assert result.retrieved_memories == [keyword]


def test_fused_recall_reports_honest_provenance_per_memory() -> None:
    memory, keyword = stored_keyword("The user prefers tea in the morning.")
    supplement = SemanticMatch(
        item=MemoryItem(content="The user likes green tea.", id=10),
        score=0.614,
    )
    brain = ScriptedBrain([Decision(DecisionKind.ANSWER)])
    stub = StubRetriever([supplement])
    stella = make_stella(memory, brain, semantic_retriever=stub)

    query = "tea in the morning"
    stella.process(Context(user_input=query))

    sources = brain.contexts[0].retrieval_sources
    keyword_source = sources[keyword.id]
    assert keyword_source.method == "keyword"
    assert keyword_source.score == relevance_score(keyword.content, query)
    supplement_source = sources[10]
    assert supplement_source.method == "local-hash-embedding"
    assert supplement_source.score == round(0.614, 3)
    # The two scales are reported separately and never cross-compared.
    assert isinstance(keyword_source.score, int)
    assert isinstance(supplement_source.score, float)


def test_lexical_miss_is_fused_from_the_real_local_index(tmp_path) -> None:
    memory = InMemoryMemory()
    retriever = make_local_retriever(
        tmp_path, ["The user prefers concise explanations."]
    )
    brain = ScriptedBrain([Decision(DecisionKind.ANSWER)])
    stella = make_stella(memory, brain, semantic_retriever=retriever)

    try:
        result = stella.process(
            Context(user_input="Does the user like brief answers?")
        )
    finally:
        retriever.index.close()

    assert [item.content for item in result.retrieved_memories] == [
        "The user prefers concise explanations."
    ]
    sources = brain.contexts[0].retrieval_sources
    assert sources[result.retrieved_memories[0].id].method == (
        "local-hash-embedding"
    )


def test_scope_isolation_remains_intact(tmp_path) -> None:
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
    stella = make_stella(memory, ScriptedBrain([Decision(DecisionKind.ANSWER)]),
                         semantic_retriever=retriever)

    try:
        result = stella.process(
            Context(user_input="Does the user like brief answers?")
        )
    finally:
        index.close()

    assert len(result.retrieved_memories) == 1
    assert result.retrieved_memories[0].scope is MemoryScope.USER
    assert result.retrieved_memories[0].memory_type is MemoryType.EPISODIC


def test_empty_index_and_disabled_paths_stay_empty(tmp_path) -> None:
    empty = make_local_retriever(tmp_path, [])
    stella = make_stella(
        InMemoryMemory(),
        ScriptedBrain([Decision(DecisionKind.ANSWER)]),
        semantic_retriever=empty,
    )
    try:
        assert stella.process(
            Context(user_input="zzzqqq nomatch phrase")
        ).retrieved_memories == []
    finally:
        empty.index.close()

    disabled = make_stella(
        InMemoryMemory(),
        ScriptedBrain([Decision(DecisionKind.ANSWER)]),
    )
    assert disabled.semantic_retriever is None
    result = disabled.process(Context(user_input="zzzqqq nomatch phrase"))
    assert result.retrieved_memories == []
    assert disabled.brain.contexts[0].retrieval_sources == {}


# ------------------------------------------------------- index sync hooks


def test_mutation_memory_action_triggers_index_sync() -> None:
    for action in ("write", "update", "delete"):
        retriever = SilentRetriever([])
        stella = make_stella(
            InMemoryMemory(),
            ScriptedBrain(
                [
                    Decision(
                        DecisionKind.TOOL,
                        capability="memory_action",
                        arguments={},
                    ),
                    Decision(DecisionKind.ANSWER, content="Done."),
                ]
            ),
            tools=ToolDispatcher([MemoryActionTool(action)]),
            semantic_retriever=retriever,
        )

        result = stella.process(Context(user_input="change memory"))

        assert retriever.index.clears == 1, action
        events = [
            event
            for event in result.interaction_trace.events
            if isinstance(event, MemoryIndexSyncEvent)
        ]
        assert events == [MemoryIndexSyncEvent(ok=True)]
        assert result.response == "Done."


def test_read_or_failed_memory_action_does_not_touch_the_index() -> None:
    for action, success in (("list", True), ("write", False)):
        retriever = SilentRetriever([])
        stella = make_stella(
            InMemoryMemory(),
            ScriptedBrain(
                [
                    Decision(
                        DecisionKind.TOOL,
                        capability="memory_action",
                        arguments={},
                    ),
                    Decision(DecisionKind.ANSWER, content="Done."),
                ]
            ),
            tools=ToolDispatcher([MemoryActionTool(action, success)]),
            semantic_retriever=retriever,
        )

        stella.process(Context(user_input="touch memory"))

        assert retriever.index.clears == 0, (action, success)


def test_successful_memory_write_reconciles_the_index() -> None:
    item = MemoryItem(content="The user prefers rail travel")
    retriever = SilentRetriever([])
    memory = InMemoryMemory()
    stella = make_stella(
        memory,
        ScriptedBrain(
            [Decision(DecisionKind.ANSWER, memory_write=MemoryWriteRequest(item))]
        ),
        semantic_retriever=retriever,
    )

    result = stella.process(Context(user_input="remember my trip style"))

    assert result.memory_write is not None
    assert result.memory_write.written is True
    assert retriever.index.clears == 1
    assert [one.content for one in retriever.indexed] == [item.content]


def test_sync_failure_is_reported_honestly_without_changing_the_write() -> None:
    item = MemoryItem(content="The user prefers rail travel")
    retriever = SilentRetriever([])
    retriever.index = FailingIndex()
    stella = make_stella(
        InMemoryMemory(),
        ScriptedBrain(
            [Decision(DecisionKind.ANSWER, memory_write=MemoryWriteRequest(item))]
        ),
        semantic_retriever=retriever,
    )

    result = stella.process(Context(user_input="remember my trip style"))

    # The memory write itself succeeded; only the derived index is stale,
    # and that is stated instead of hidden.
    assert result.memory_write is not None
    assert result.memory_write.written is True
    assert "semantic index could not be refreshed" in result.response
    events = [
        event
        for event in result.interaction_trace.events
        if isinstance(event, MemoryIndexSyncEvent)
    ]
    assert events == [MemoryIndexSyncEvent(ok=False)]


def test_turn_without_memory_changes_never_reconciles() -> None:
    retriever = SilentRetriever([])
    stella = make_stella(
        InMemoryMemory(),
        ScriptedBrain([Decision(DecisionKind.ANSWER, content="Quiet.")]),
        semantic_retriever=retriever,
    )

    result = stella.process(Context(user_input="hello there"))

    assert retriever.index.clears == 0
    assert not any(
        isinstance(event, MemoryIndexSyncEvent)
        for event in result.interaction_trace.events
    )
