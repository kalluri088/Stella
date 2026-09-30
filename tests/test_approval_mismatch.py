"""The approval dialog must flag actions the user's own turn never mentioned.

Mismatch warnings (mismatch.py) are the display-side prompt-injection
nudge: neither the capability's verb family nor any distinctive target
token appears in the current-turn text, so the preview carries one
advisory line. These tests pin the pure function and both integration
paths (tool approval, post-observation memory write), including the
invariants that keep it honest: unknown capabilities and textless turns
never warn, and a warning changes nothing about what an answer
authorizes.
"""

from __future__ import annotations

from pathlib import Path

from stella.brain import Brain, Decision, DecisionKind
from stella.context import Context
from stella.llm import LLMClient, Message
from stella.memory import InMemoryMemory, MemoryItem, MemoryWriteRequest
from stella.mismatch import approval_mismatch_warning
from stella.stella import Stella
from stella.tools import (
    ActionPreview,
    ApprovalRequest,
    FileSystemWriteTool,
    Tool,
    ToolApproval,
    ToolDispatcher,
    ToolResult,
)


def warn(text: str | None, capability: str | None, **arguments: object):
    return approval_mismatch_warning(text, capability, dict(arguments))


class RecordingLLM(LLMClient):
    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        del messages
        return "final response"


class SequenceBrain(Brain):
    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = list(decisions)

    def decide(self, context: Context) -> Decision:
        del context
        return self.decisions.pop(0)


class SafeEchoTool(Tool):
    """A SAFE tool: executes without approval, leaves an observation."""

    @property
    def name(self) -> str:
        return "safe_echo"

    @property
    def description(self) -> str:
        return "Echoes its input."

    @property
    def argument_schema(self) -> dict[str, object]:
        return {"value": "string"}

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return isinstance(arguments.get("value"), str)

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        return ToolResult(success=True, output=str(arguments.get("value")))


# --- pure function ---------------------------------------------------------


def test_unmentioned_action_and_target_warns() -> None:
    warning = warn(
        "what is the weather today",
        "filesystem_write",
        path="secrets.txt",
        content="nothing to see",
    )
    assert warning is not None
    assert "writing that file" in warning


def test_verb_mention_silences_even_without_the_target() -> None:
    assert warn("delete everything in here", "filesystem_delete",
                path="quarterly-report.txt") is None


def test_target_mention_silences_even_without_the_verb() -> None:
    assert warn("the quarterly-report looks stale", "filesystem_delete",
                path="/tmp/quarterly-report.txt") is None


def test_delegation_phrases_silence() -> None:
    assert warn("take care of my desktop", "web_fetch",
                url="https://archive.example.com/inbox") is None


def test_multi_word_verb_entries_match_the_normalized_text() -> None:
    assert warn("please tell me when it is due", "reminder_create",
                content="standup", due_at=None) is None


def test_unknown_and_empty_capabilities_never_warn() -> None:
    assert warn("anything", "outline_search", query="x") is None
    assert warn("anything", "", query="x") is None
    assert warn("anything", None, query="x") is None


def test_textless_turns_never_claim_a_mismatch() -> None:
    for text in (None, "", "   "):
        assert warn(text, "filesystem_write", path="x.txt") is None


def test_url_targets_count_their_host_and_path() -> None:
    assert warn("did the ycombinator thing change", "web_fetch",
                url="https://news.ycombinator.com/item") is None
    assert warn("how is the weather", "web_fetch",
                url="https://news.ycombinator.com/item") is not None


def test_prose_targets_need_distinctive_words() -> None:
    assert warn("what did I note about revenue", "memory_write",
                content="quarterly revenue targets moved") is None
    assert warn("how are things", "memory_write",
                content="quarterly revenue targets moved") is not None


def test_generic_filler_never_counts_as_a_target() -> None:
    # path "/home/user/file.txt" carries no distinctive tokens; the
    # matching "file" must not come from stopword-ish path noise.
    # (The user's own word "file" would count as the verb instead.)
    assert warn("the report is ready", "filesystem_write",
                path="/home/user/file.txt") is not None


# --- integration: tool approvals ------------------------------------------


def _stella_with_writer(workspace: Path, provider) -> Stella:
    return Stella(
        SequenceBrain([
            Decision(
                DecisionKind.TOOL,
                capability="filesystem_write",
                arguments={"path": "notes.txt", "content": "hello world"},
            ),
            Decision(DecisionKind.ANSWER, content="Done."),
        ]),
        RecordingLLM(),
        ToolDispatcher([FileSystemWriteTool(workspace)]),
        InMemoryMemory(),
        approval_provider=provider,
    )


def test_tool_approval_preview_carries_the_warning(tmp_path: Path) -> None:
    seen: list[ActionPreview | None] = []

    def approve(request: ApprovalRequest,
                preview: ActionPreview | None = None) -> ToolApproval:
        seen.append(preview)
        return ToolApproval(request=request, approved=False)

    _stella_with_writer(tmp_path, approve).process(
        Context(user_input="what is the weather")
    )
    assert len(seen) == 1
    assert seen[0] is not None and seen[0].warning is not None
    # The dispatcher's own preview still rides along untouched.
    assert seen[0].detail_lines
    assert not (tmp_path / "notes.txt").exists()  # denial changed nothing


def test_tool_approval_preview_is_silent_when_the_turn_backs_it(tmp_path: Path) -> None:
    seen: list[ActionPreview | None] = []

    def approve(request: ApprovalRequest,
                preview: ActionPreview | None = None) -> ToolApproval:
        seen.append(preview)
        return ToolApproval(request=request, approved=True)

    _stella_with_writer(tmp_path, approve).process(
        Context(user_input="save notes.txt for me")
    )
    assert seen[0] is not None and seen[0].warning is None
    assert (tmp_path / "notes.txt").read_text() == "hello world"


# --- integration: post-observation memory writes ---------------------------


def test_memory_proposal_after_observations_warns() -> None:
    seen: list[ActionPreview | None] = []

    def approve(request: ApprovalRequest,
                preview: ActionPreview | None = None) -> ToolApproval:
        seen.append(preview)
        return ToolApproval(request=request, approved=True)

    memory = InMemoryMemory()
    stella = Stella(
        SequenceBrain([
            Decision(DecisionKind.TOOL, capability="safe_echo",
                     arguments={"value": "done"}),
            Decision(
                DecisionKind.ANSWER,
                content="Noted.",
                memory_write=MemoryWriteRequest(
                    MemoryItem(content="boss prefers teal banners")
                ),
            ),
        ]),
        RecordingLLM(),
        ToolDispatcher([SafeEchoTool()]),
        memory,
        max_tool_steps=2,
        approval_provider=approve,
    )
    stella.process(Context(user_input="echo the status of the project"))
    assert len(seen) == 1
    assert seen[0] is not None and seen[0].warning is not None

    seen.clear()
    memory2 = InMemoryMemory()
    Stella(
        SequenceBrain([
            Decision(DecisionKind.TOOL, capability="safe_echo",
                     arguments={"value": "done"}),
            Decision(
                DecisionKind.ANSWER,
                content="Noted.",
                memory_write=MemoryWriteRequest(
                    MemoryItem(content="boss prefers teal banners")
                ),
            ),
        ]),
        RecordingLLM(),
        ToolDispatcher([SafeEchoTool()]),
        memory2,
        max_tool_steps=2,
        approval_provider=approve,
    ).process(Context(user_input="remember that the boss prefers teal"))
    # A silent memory proposal carries no preview at all: a plain None
    # is never forwarded, exactly as before this feature existed.
    assert seen[0] is None
