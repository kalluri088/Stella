"""Brain policy, multi-step workspace flow and outcome-honesty tests."""

from stella.brain import Brain, Decision, DecisionKind, LLMBrain
from stella.context import Context, ToolObservation
from stella.llm import LLMClient, LLMResponse, Message, ToolUseMode
from stella.memory import InMemoryMemory
from stella.stella import Stella
from stella.tools import (
    EchoTool,
    FileSystemReadTool,
    ToolDispatcher,
    WorkspaceFindTool,
    WorkspaceListTool,
    WorkspaceSearchTool,
)


class RecordingToolLLM(LLMClient):
    """Records the tool-choice mode (and prompt) the policy produced."""

    def __init__(self, response: str = '{"kind": "do_nothing"}') -> None:
        self.response = response
        self.tool_choice: ToolUseMode | None = None
        self.messages: list[list[Message]] = []

    def chat(self, messages) -> str:
        raise AssertionError("this test only uses the tool-decision path")

    def chat_with_tools(self, messages, tools, tool_choice=ToolUseMode.AUTO):
        self.tool_choice = tool_choice
        self.messages.append(messages)
        return LLMResponse(content=self.response)


class SequenceBrain(Brain):
    answer_content_is_final = True

    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = list(decisions)

    def decide(self, context: Context) -> Decision:
        if not self.decisions:
            raise AssertionError("brain consulted more often than expected")
        return self.decisions.pop(0)


class SynthesizingLLM(LLMClient):
    def __init__(self, response: str = "grounded answer") -> None:
        self.response = response
        self.messages: list[list] = []

    def chat(self, messages) -> str:
        self.messages.append(messages)
        return self.response


def test_workspace_requests_require_a_tool() -> None:
    for user_input in (
        "What files are in my workspace?",
        "Find files that mention JWT",
        "Search the project for SQLite",
    ):
        llm = RecordingToolLLM()

        LLMBrain(llm).decide(Context(user_input=user_input))

        assert llm.tool_choice is ToolUseMode.REQUIRED, user_input


def test_ordinary_questions_do_not_require_a_tool() -> None:
    for user_input in (
        "What does authentication mean?",
        "What is the capital of France?",
        "I forgot what TCP means.",
        "Tell me a joke about cats",
    ):
        llm = RecordingToolLLM()

        LLMBrain(llm).decide(Context(user_input=user_input))

        assert llm.tool_choice is ToolUseMode.AUTO, user_input


def test_existing_observation_relaxes_workspace_policy() -> None:
    llm = RecordingToolLLM()

    LLMBrain(llm).decide(
        Context(
            user_input="What files are in my workspace?",
            tool_observations=[
                ToolObservation(
                    capability="workspace_list",
                    arguments={},
                    success=True,
                    output="notes.txt [file] (5 bytes) modified x",
                )
            ],
        )
    )

    assert llm.tool_choice is ToolUseMode.AUTO


def test_required_policy_rejects_guessed_workspace_answers() -> None:
    llm = RecordingToolLLM(
        '{"kind": "answer", "content": "There are ten files, I think."}'
    )

    decision = LLMBrain(llm).decide(
        Context(user_input="What files are in my workspace?")
    )

    assert decision.kind is DecisionKind.ASK


def test_workspace_capabilities_are_registered_without_approval() -> None:
    brain = LLMBrain(RecordingToolLLM())

    capabilities = {
        description["capability"] for description in brain.tools.describe()
    }
    assert {"workspace_list", "workspace_find", "workspace_search"} <= capabilities
    for capability in ("workspace_list", "workspace_find", "workspace_search"):
        assert brain.tools.requires_approval(capability) is False


def test_prompt_documents_workspace_routing() -> None:
    llm = RecordingToolLLM()

    LLMBrain(llm).decide(Context(user_input="hello"))

    prompt = llm.messages[0][0].content
    assert "Workspace intelligence" in prompt
    assert "workspace_search" in prompt
    assert "ordinary knowledge" in prompt


def build_stella(workspace, decisions, tmp_memory=None):
    dispatcher = ToolDispatcher(
        [
            FileSystemReadTool(workspace),
            WorkspaceListTool(workspace),
            WorkspaceFindTool(workspace),
            WorkspaceSearchTool(workspace),
            EchoTool(),
        ]
    )
    memory = tmp_memory if tmp_memory is not None else InMemoryMemory()
    return (
        Stella(
            SequenceBrain(decisions),
            SynthesizingLLM(),
            dispatcher,
            memory,
            max_tool_steps=2,
        ),
        memory,
    )


def test_multi_step_workspace_investigation_uses_two_tool_steps(
    tmp_path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "db.py").write_text(
        "engine = 'sqlite3'\npath = 'app.db'\n", encoding="utf-8"
    )
    stella, _ = build_stella(
        root,
        [
            Decision(
                DecisionKind.TOOL,
                capability="workspace_search",
                arguments={"pattern": "sqlite"},
            ),
            Decision(
                DecisionKind.TOOL,
                capability="filesystem_read",
                arguments={"path": "db.py"},
            ),
            Decision(
                DecisionKind.ANSWER,
                content="db.py configures the SQLite engine.",
            ),
        ],
    )

    result = stella.process(
        Context(user_input="Where is SQLite configured?")
    )

    assert result.response == "db.py configures the SQLite engine."
    tool_steps = [
        step for step in result.step_trace if step.tool_result is not None
    ]
    assert [step.tool_result.success for step in tool_steps] == [True, True]


def test_truncated_observation_is_carried_to_synthesis_honestly(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(WorkspaceListTool.MAX_OUTPUT_LINES + 20):
        (root / f"file{index:03d}.txt").write_text("x", encoding="utf-8")
    stella, _ = build_stella(
        root,
        [
            Decision(
                DecisionKind.TOOL,
                capability="workspace_list",
                arguments={},
                tool_final=True,
            )
        ],
    )
    llm = stella.llm

    result = stella.process(Context(user_input="What files are here?"))

    assert result.response == "grounded answer"
    # Either the tool's own "[Truncated: ...]" notice or the context layer's
    # "[tool output truncated]" marker must reach the synthesis prompt.
    assert "truncated" in str(llm.messages).lower()


def test_no_result_observation_reaches_synthesis(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("hello", encoding="utf-8")
    stella, _ = build_stella(
        root,
        [
            Decision(
                DecisionKind.TOOL,
                capability="workspace_find",
                arguments={"pattern": "zzz"},
                tool_final=True,
            )
        ],
    )
    llm = stella.llm

    result = stella.process(Context(user_input="Find anything about zzz."))

    assert result.response == "grounded answer"
    assert "No workspace paths contain" in str(llm.messages)


def test_failed_observation_reaches_synthesis(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    stella, _ = build_stella(
        root,
        [
            Decision(
                DecisionKind.TOOL,
                capability="filesystem_read",
                arguments={"path": "missing.txt"},
                tool_final=True,
            )
        ],
    )
    llm = stella.llm

    result = stella.process(Context(user_input="Read missing.txt."))

    assert result.response == "grounded answer"
    assert "File was not found." in str(llm.messages)


def test_workspace_inspection_never_writes_memory(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "db.py").write_text("engine = 'sqlite3'", encoding="utf-8")
    memory = InMemoryMemory()
    stella, memory = build_stella(
        root,
        [
            Decision(
                DecisionKind.TOOL,
                capability="workspace_search",
                arguments={"pattern": "sqlite"},
                tool_final=True,
            )
        ],
        tmp_memory=memory,
    )

    result = stella.process(Context(user_input="Does this use SQLite?"))

    assert result.memory_write is None
    assert memory.retrieve() == []
