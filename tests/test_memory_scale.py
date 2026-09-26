"""B3 memory-scale policies: bounded recall window, dedupe guidance, gate.

The retrieval window bounds how many candidates a per-turn recall may
carry at all; dedupe guidance advises (never vetoes) on a repeated fact;
and the forgetting tool's approval gate is re-proved, not assumed.
"""

from __future__ import annotations

from stella.brain import Brain, Decision, DecisionKind
from stella.context import MAX_RECALL_WINDOW, MAX_RETRIEVED_MEMORIES, Context
from stella.llm import LLMClient, Message
from stella.memory import InMemoryMemory, MemoryItem
from stella.stella import Stella
from stella.tools import (
    ApprovalRequest,
    MemoryForgetTool,
    MemoryWriteTool,
    ToolApproval,
    ToolDispatcher,
)


class RecordingBrain(Brain):
    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        self.contexts: list[Context] = []

    def decide(self, context: Context, should_cancel=None) -> Decision:
        self.contexts.append(context)
        return self.decision


class SilentLLM(LLMClient):
    def chat(
        self, messages: list[Message | dict[str, str]], should_cancel=None
    ) -> str:
        return "ok"


def approve(dispatcher: ToolDispatcher, capability: str, arguments: dict):
    return ToolApproval(ApprovalRequest(capability, dict(arguments)), True)


class TestRecallWindow:
    def test_recall_carries_only_the_window_from_a_large_store(self) -> None:
        memory = InMemoryMemory()
        total = MAX_RECALL_WINDOW * 2
        for index in range(total):
            memory.store(
                MemoryItem(content=f"tea kettle note number {index + 1}")
            )
        brain = RecordingBrain(Decision(DecisionKind.ANSWER))
        stella = Stella(brain, SilentLLM(), ToolDispatcher([]), memory)

        stella.process(Context(user_input="which tea kettle note"))

        passed = brain.contexts[0].retrieved_memories
        assert 0 < len(passed) <= MAX_RETRIEVED_MEMORIES
        # retrieve() sorts newest-first on relevance ties, so the window
        # is the newest MAX_RECALL_WINDOW ids; nothing older may appear.
        oldest_allowed = total - MAX_RECALL_WINDOW + 1
        contents = {item.content for item in passed}
        for content in contents:
            number = int(content.rsplit(" ", 1)[1])
            assert number >= oldest_allowed, content


class TestDedupeGuidance:
    def test_first_write_is_plain(self) -> None:
        dispatcher = ToolDispatcher([MemoryWriteTool(InMemoryMemory())])
        arguments = {"content": "User drinks black coffee in the morning"}
        result = dispatcher.execute(
            "memory_write", arguments, approve(dispatcher, "memory_write", arguments)
        )
        assert result.success
        assert result.output == "Stored the memory."

    def test_repeated_fact_stores_but_advises(self) -> None:
        memory = InMemoryMemory()
        dispatcher = ToolDispatcher([MemoryWriteTool(memory)])
        arguments = {"content": "User drinks black coffee in the morning"}
        first = approve(dispatcher, "memory_write", arguments)
        dispatcher.execute("memory_write", arguments, first)
        second_args = {"content": "User drinks black coffee in the morning."}
        result = dispatcher.execute(
            "memory_write",
            second_args,
            approve(dispatcher, "memory_write", second_args),
        )
        assert result.success
        # Guidance, not a veto: the item still landed, and the note names
        # the existing copy and the honest remedy.
        assert len(memory.retrieve()) == 2
        assert "NOTE:" in result.output
        assert "memory 1" in result.output
        assert "memory_update" in result.output

    def test_different_facts_are_never_flagged(self) -> None:
        memory = InMemoryMemory()
        dispatcher = ToolDispatcher([MemoryWriteTool(memory)])
        for content in (
            "User drinks black coffee in the morning",
            "User eats toast in the morning",
        ):
            arguments = {"content": content}
            result = dispatcher.execute(
                "memory_write",
                arguments,
                approve(dispatcher, "memory_write", arguments),
            )
            assert result.output == "Stored the memory."

    def test_single_shared_word_cannot_suppress(self) -> None:
        memory = InMemoryMemory()
        dispatcher = ToolDispatcher([MemoryWriteTool(memory)])
        for content in ("Favorite color is rust", "Favorite metal is rust"):
            arguments = {"content": content}
            result = dispatcher.execute(
                "memory_write",
                arguments,
                approve(dispatcher, "memory_write", arguments),
            )
            assert "NOTE:" not in result.output


class TestForgetGateStays:
    def test_memory_forget_requires_approval(self) -> None:
        memory = InMemoryMemory()
        memory.store(MemoryItem(content="User moved to Bengaluru"))
        dispatcher = ToolDispatcher([MemoryForgetTool(memory)])
        arguments = {"query": "user bengaluru plans"}
        assert dispatcher.requires_approval("memory_forget", arguments)
        refused = dispatcher.execute("memory_forget", arguments, None)
        assert not refused.success
        assert refused.output == "Approval required."
        assert len(memory.retrieve()) == 1
        result = dispatcher.execute(
            "memory_forget", arguments, approve(dispatcher, "memory_forget", arguments)
        )
        assert result.success
        assert memory.retrieve() == []
