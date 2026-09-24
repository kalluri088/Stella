import datetime as dt
import json
from unittest.mock import patch

import pytest

from stella.brain import Brain, Decision, DecisionKind, LLMBrain
from stella.context import MAX_RETRIEVED_MEMORIES, Context
from stella.llm import LLMClient, Message
from stella.memory import (
    InMemoryMemory,
    Memory,
    MemoryItem,
    MemoryScope,
    MemoryType,
    MemoryWriteRequest,
    SQLiteMemory,
)
from stella.stella import Stella
from stella.tools import (
    ActionPreview,
    ActionReceipt,
    ApprovalRequest,
    DateTimeTool,
    EchoTool,
    FileSystemDeleteTool,
    FileSystemReadTool,
    FileSystemWriteTool,
    NetworkReadTool,
    RiskLevel,
    SystemInfoTool,
    Tool,
    ToolApproval,
    ToolDispatcher,
    ToolResult,
)
from stella.trace import ApprovalEvent


class FixedBrain(Brain):
    def __init__(self, decision: Decision) -> None:
        self.decision = decision

    def decide(self, context: Context) -> Decision:
        return self.decision


class RecordingBrain(FixedBrain):
    def __init__(self, decision: Decision) -> None:
        super().__init__(decision)
        self.contexts: list[Context] = []

    def decide(self, context: Context) -> Decision:
        self.contexts.append(context)
        return super().decide(context)


class SequenceBrain(Brain):
    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = list(decisions)
        self.contexts: list[Context] = []

    def decide(self, context: Context) -> Decision:
        self.contexts.append(context)
        return self.decisions.pop(0)


class MemoryAwareBrain(Brain):
    def __init__(self) -> None:
        self.contexts: list[Context] = []

    def decide(self, context: Context) -> Decision:
        self.contexts.append(context)
        if context.retrieved_memories:
            return Decision(DecisionKind.ANSWER)
        return Decision(DecisionKind.ASK, content="I do not know yet.")


class EveningTeaPreferenceBrain(Brain):
    """Make the later clarification depend on a retrieved preference."""

    answer_content_is_final = True

    def __init__(self) -> None:
        self.contexts: list[Context] = []

    def decide(self, context: Context) -> Decision:
        self.contexts.append(context)
        if context.retrieved_memories:
            return Decision(
                DecisionKind.ANSWER,
                content="Have jasmine tea this evening.",
            )
        return Decision(
            DecisionKind.ASK,
            content="Which tea would you like this evening?",
        )


class ContextualTeaBrain(Brain):
    """Choose the tea action only when the follow-up context is complete."""

    def __init__(self) -> None:
        self.contexts: list[Context] = []

    def decide(self, context: Context) -> Decision:
        self.contexts.append(context)
        has_evening_history = any(
            message.content == "I am having a quiet evening tea at home."
            for message in context.conversation_history
        )
        if context.retrieved_memories and has_evening_history:
            return Decision(
                DecisionKind.TOOL,
                capability="record",
                arguments={
                    "message": "Prepare jasmine tea for this evening."
                },
            )
        return Decision(
            DecisionKind.ASK,
            content="What should I prepare, and for which situation?",
        )


class UncertaintyAwareTeaNoteBrain(Brain):
    """Require both a relevant preference and an explicit file target."""

    def __init__(self) -> None:
        self.contexts: list[Context] = []

    def decide(self, context: Context) -> Decision:
        self.contexts.append(context)
        has_target = any(
            "plans/evening.txt" in message.content
            for message in context.conversation_history
        )
        has_relevant_memory = any(
            "jasmine tea" in memory.content.casefold()
            for memory in context.retrieved_memories
        )
        if has_target and has_relevant_memory:
            return Decision(
                DecisionKind.TOOL,
                capability="record",
                arguments={
                    "path": "plans/evening.txt",
                    "content": "Evening plan: jasmine tea.",
                },
            )
        return Decision(
            DecisionKind.ASK,
            content="Which file should I use for the evening plan?",
        )


class StatusPreferenceBrain(Brain):
    """Apply one explicit response-style preference when status is relevant."""

    answer_content_is_final = True

    def __init__(self) -> None:
        self.contexts: list[Context] = []

    def decide(self, context: Context) -> Decision:
        self.contexts.append(context)
        has_status_preference = any(
            "one-line status update" in memory.content.casefold()
            for memory in context.retrieved_memories
        )
        if has_status_preference:
            return Decision(
                DecisionKind.ANSWER,
                content="Deployment is on track.",
            )
        return Decision(
            DecisionKind.ANSWER,
            content=(
                "Deployment is on track. Tests are passing, and rollout is "
                "scheduled for Friday."
            ),
        )


class RecordingLLM(LLMClient):
    def __init__(self, response: str = "answer") -> None:
        self.response = response
        self.messages: list[list[Message | dict[str, str]]] = []

    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        self.messages.append(messages)
        return self.response


class SequenceLLM(LLMClient):
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.messages: list[list[Message | dict[str, str]]] = []

    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        self.messages.append(messages)
        return self.responses.pop(0)


class RecordingTool(Tool):
    name = "record"
    description = "Records structured arguments."

    def __init__(self) -> None:
        self.arguments: list[dict[str, object]] = []

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return True

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        self.arguments.append(arguments)
        return ToolResult(success=True, output="tool output")


class FailingResultTool(RecordingTool):
    def execute(self, arguments: dict[str, object]) -> ToolResult:
        self.arguments.append(arguments)
        return ToolResult(success=False, output="tool reported failure")


class RaisingTool(RecordingTool):
    def execute(self, arguments: dict[str, object]) -> ToolResult:
        self.arguments.append(arguments)
        raise RuntimeError("unexpected failure")


class ApprovalRecordingTool(RecordingTool):
    name = "approval_test"
    description = "Test-only approval-required action."

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.DANGEROUS


class RecordingMemory(InMemoryMemory):
    def __init__(self) -> None:
        super().__init__()
        self.write_count = 0

    def store(self, item: MemoryItem) -> None:
        self.write_count += 1
        super().store(item)


def make_stella(
    decision: Decision,
    llm: RecordingLLM | None = None,
    tool: RecordingTool | None = None,
    memory: Memory | None = None,
) -> tuple[Stella, RecordingLLM, RecordingTool]:
    llm = llm or RecordingLLM()
    tool = tool or RecordingTool()
    memory = memory or InMemoryMemory()
    return Stella(FixedBrain(decision), llm, tool, memory), llm, tool


@pytest.mark.parametrize("max_tool_steps", [0, -1, 1.5, True])
def test_stella_requires_positive_integer_tool_step_bound(max_tool_steps) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        Stella(
            FixedBrain(Decision(DecisionKind.ANSWER)),
            RecordingLLM(),
            RecordingTool(),
            InMemoryMemory(),
            max_tool_steps=max_tool_steps,
        )


def test_stella_uses_llm_for_answer_decision() -> None:
    stella, llm, _ = make_stella(Decision(DecisionKind.ANSWER))
    context = Context(
        user_input="Current question",
        conversation_history=[Message(role="user", content="Earlier question")],
    )

    result = stella.process(context)

    assert result.response == "answer"
    assert len(llm.messages) == 1
    assert llm.messages[0][0].role == "system"
    answer_context = json.loads(llm.messages[0][1].content)
    assert answer_context["user_input"] == "Current question"
    assert answer_context["conversation_history"] == [
        {"role": "user", "content": "Earlier question"}
    ]
    assert answer_context["decision"]["kind"] == "answer"


def test_llm_brain_answer_content_is_used_without_second_llm_call() -> None:
    llm = RecordingLLM(
        response='{"kind":"answer","content":"The answer is 42."}'
    )
    stella = Stella(
        LLMBrain(llm, ToolDispatcher([])),
        llm,
        ToolDispatcher([]),
        InMemoryMemory(),
    )

    result = stella.process(Context(user_input="What is the answer?"))

    assert result.response == "The answer is 42."
    assert len(llm.messages) == 1


def test_llm_brain_answer_without_content_keeps_synthesis_fallback() -> None:
    llm = RecordingLLM(response='{"kind":"answer"}')
    stella = Stella(
        LLMBrain(llm, ToolDispatcher([])),
        llm,
        ToolDispatcher([]),
        InMemoryMemory(),
    )

    result = stella.process(Context(user_input="What is the answer?"))

    assert result.response == '{"kind":"answer"}'
    assert len(llm.messages) == 2


def test_stella_returns_more_information_result_for_ask_decision() -> None:
    stella, llm, _ = make_stella(
        Decision(DecisionKind.ASK, content="Which account should I use?")
    )

    result = stella.process(Context(user_input="ask: account"))

    assert result.needs_more_information is True
    assert result.response == "Which account should I use?"
    assert llm.messages == []


def test_stella_executes_tool_with_structured_arguments() -> None:
    stella, llm, tool = make_stella(
        Decision(
            DecisionKind.TOOL,
            arguments={"message": "hello"},
            capability="record",
        )
    )

    result = stella.process(Context(user_input="tool: echo"))

    assert result.tool_result == ToolResult(success=True, output="tool output")
    assert result.response == "answer"
    assert tool.arguments == [{"message": "hello"}]
    assert len(llm.messages) == 1
    assert len(result.step_trace) == 1
    assert result.step_trace[0].tool_result == result.tool_result


def test_stella_executes_datetime_and_sends_result_to_llm() -> None:
    llm = RecordingLLM(response="The local time is 12:34:56.")
    stella = Stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                arguments={"kind": "time"},
                capability="datetime",
            )
        ),
        llm,
        DateTimeTool(),
        InMemoryMemory(),
    )

    fixed = dt.datetime(2026, 9, 6, 12, 34, 56, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    with patch("stella.tools.dt.datetime") as datetime_class:
        datetime_class.now.return_value = fixed
        result = stella.process(Context(user_input="What time is it?"))

    assert result.tool_result is not None
    assert result.tool_result.success is True
    payload = json.loads(llm.messages[0][1].content)
    assert payload["decision"]["capability"] == "datetime"
    assert payload["tool_result"] == {
        "success": True,
        "output": "12:34:56 +0530",
    }
    assert result.response == "The local time is 12:34:56."


def test_stella_dispatches_system_info_capability() -> None:
    llm = RecordingLLM(response="Linux platform")
    stella = Stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                arguments={"kind": "platform"},
                capability="system_info",
            )
        ),
        llm,
        ToolDispatcher([DateTimeTool(), SystemInfoTool(), EchoTool()]),
        InMemoryMemory(),
    )

    with patch("stella.tools.platform.platform", return_value="Linux platform"):
        result = stella.process(Context(user_input="What platform is this?"))

    assert result.tool_result == ToolResult(
        success=True, output="Linux platform"
    )
    assert result.response == "Linux platform"


def test_stella_dispatches_echo_capability() -> None:
    llm = RecordingLLM(response="Echo result: hello")
    stella = Stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                arguments={"message": "hello"},
                capability="echo",
            )
        ),
        llm,
        ToolDispatcher([DateTimeTool(), SystemInfoTool(), EchoTool()]),
        InMemoryMemory(),
    )

    result = stella.process(Context(user_input="Echo hello"))

    assert result.tool_result == ToolResult(success=True, output="hello")
    assert result.response == "Echo result: hello"


def test_stella_dispatches_network_read_and_sends_untrusted_result_to_llm(
    monkeypatch,
) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.sock = type("Socket", (), {"settimeout": lambda self, value: None})()

        def request(self, method, path, headers) -> None:
            self.requested = (method, path, headers)

        def getresponse(self):
            class Response:
                status = 200

                def __init__(self) -> None:
                    self.body = b"public note"

                def getheader(self, name):
                    return (
                        "text/plain; charset=utf-8"
                        if name == "Content-Type"
                        else str(len(self.body))
                    )

                def read(self, amount):
                    chunk, self.body = self.body[:amount], self.body[amount:]
                    return chunk

            return Response()

        def close(self) -> None:
            pass

    connection = FakeConnection()
    monkeypatch.setattr(
        "stella.tools.NetworkReadTool._resolve_public_addresses",
        classmethod(lambda cls, hostname: ("93.184.216.34",)),
    )
    monkeypatch.setattr(
        "stella.tools._ValidatedHTTPSConnection",
        lambda hostname, address, timeout: connection,
    )
    llm = RecordingLLM(response="The public note says: public note")
    arguments = {"url": "https://example.com/notes.txt"}
    request = ApprovalRequest("network_read", arguments)
    stella = Stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                capability="network_read",
                arguments=arguments,
            )
        ),
        llm,
        ToolDispatcher([NetworkReadTool()]),
        InMemoryMemory(),
        approval_provider=lambda approval_request, _preview=None: ToolApproval(
            request=request, approved=True
        ),
    )

    result = stella.process(Context(user_input="Read the public note."))

    assert result.tool_result == ToolResult(
        success=True,
        output="public note",
        action_receipt=ActionReceipt("fetch", "verified", 11),
    )
    assert result.response == "The public note says: public note"
    payload = json.loads(llm.messages[0][1].content)
    assert payload["tool_result"] == {
        "success": True,
        "output": "public note",
    }


def test_stella_dispatches_filesystem_read_and_sends_result_to_llm(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("Remember the launch date.", encoding="utf-8")
    llm = RecordingLLM(response="The notes say: Remember the launch date.")
    stella = Stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                arguments={"path": "notes.txt"},
                capability="filesystem_read",
            )
        ),
        llm,
        ToolDispatcher([FileSystemReadTool(workspace)]),
        InMemoryMemory(),
    )

    result = stella.process(Context(user_input="Read notes.txt"))

    assert result.tool_result == ToolResult(
        success=True, output="Remember the launch date."
    )
    assert result.response == "The notes say: Remember the launch date."
    payload = json.loads(llm.messages[0][1].content)
    assert payload["decision"]["capability"] == "filesystem_read"
    assert payload["tool_result"] == {
        "success": True,
        "output": "Remember the launch date.",
    }


def test_stella_writes_filesystem_only_after_exact_approval(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    llm = RecordingLLM(response="The file was created.")
    dispatcher = ToolDispatcher([FileSystemWriteTool(workspace)])
    arguments = {"path": "notes.txt", "content": "approved content"}

    def approve(
        request: ApprovalRequest, preview: ActionPreview | None = None
    ) -> ToolApproval:
        return ToolApproval(request=request, approved=True)

    stella = Stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                capability="filesystem_write",
                arguments=arguments,
            )
        ),
        llm,
        dispatcher,
        InMemoryMemory(),
        approval_provider=approve,
    )

    result = stella.process(Context(user_input="Write the file."))

    assert result.tool_result == ToolResult(
        success=True,
        output="File created and verified.",
        action_receipt=ActionReceipt("create", "verified", 16),
    )
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == (
        "approved content"
    )
    payload = json.loads(llm.messages[0][1].content)
    assert payload["tool_result"] == {
        "success": True,
        "output": "File created and verified.",
    }


def test_stella_deletes_filesystem_only_after_exact_approval(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("delete me", encoding="utf-8")
    arguments = {"path": "notes.txt"}
    dispatcher = ToolDispatcher([FileSystemDeleteTool(workspace)])
    llm = RecordingLLM(response="The file was deleted.")

    def approve(
        request: ApprovalRequest, preview: ActionPreview | None = None
    ) -> ToolApproval:
        return ToolApproval(request=request, approved=True)

    stella = Stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                capability="filesystem_delete",
                arguments=arguments,
            )
        ),
        llm,
        dispatcher,
        InMemoryMemory(),
        approval_provider=approve,
    )

    result = stella.process(Context(user_input="Delete notes.txt"))

    assert result.tool_result == ToolResult(
        success=True,
        output="File deleted and verified to be absent.",
        action_receipt=ActionReceipt("delete", "verified"),
    )
    assert result.response == "The file was deleted."
    assert not target.exists()
    assert dispatcher.audit_records[0].approval_granted is True


def test_stella_requests_action_specific_approval_before_execution() -> None:
    tool = ApprovalRecordingTool()
    requests: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> ToolApproval:
        requests.append(request)
        return ToolApproval(request=request, approved=True)

    stella = Stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                arguments={"value": "x"},
                capability="approval_test",
            )
        ),
        RecordingLLM(),
        tool,
        InMemoryMemory(),
        approval_provider=approve,
    )

    result = stella.process(Context(user_input="perform test action"))

    assert requests == [ApprovalRequest("approval_test", {"value": "x"})]
    assert tool.arguments == [{"value": "x"}]
    assert result.tool_result == ToolResult(success=True, output="tool output")


@pytest.mark.parametrize(
    "approval_provider",
    [
        None,
        lambda request: ToolApproval(request=request, approved=False),
        lambda request: ToolApproval(
            request=ApprovalRequest(request.capability, {"value": "other"}),
            approved=True,
        ),
    ],
)
def test_stella_fails_closed_for_missing_rejected_or_invalid_approval(
    approval_provider,
) -> None:
    tool = ApprovalRecordingTool()
    stella = Stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                arguments={"value": "x"},
                capability="approval_test",
            )
        ),
        RecordingLLM(),
        tool,
        InMemoryMemory(),
        approval_provider=approval_provider,
    )

    result = stella.process(Context(user_input="perform test action"))

    assert result.tool_result is not None
    assert result.tool_result.success is False
    assert tool.arguments == []


def test_safe_tool_does_not_request_approval() -> None:
    requests: list[ApprovalRequest] = []
    tool = EchoTool()
    stella = Stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                arguments={"message": "hello"},
                capability="echo",
            )
        ),
        RecordingLLM(),
        tool,
        InMemoryMemory(),
        approval_provider=lambda request: requests.append(request),
    )

    result = stella.process(Context(user_input="echo hello"))

    assert result.tool_result == ToolResult(success=True, output="hello")
    assert requests == []


def test_llm_approval_field_cannot_bypass_runtime_approval() -> None:
    tool = ApprovalRecordingTool()
    llm = RecordingLLM(
        response=(
            '{"kind":"tool","capability":"approval_test",'
            '"arguments":{"value":"x"},"approved":true}'
        )
    )
    dispatcher = ToolDispatcher([tool])
    stella = Stella(
        LLMBrain(llm, dispatcher),
        llm,
        dispatcher,
        InMemoryMemory(),
    )

    result = stella.process(Context(user_input="Run approval_test."))

    assert result.tool_result == ToolResult(
        success=False, output="Approval required."
    )
    assert tool.arguments == []


def test_stella_collection_cannot_execute_unknown_capability() -> None:
    tool = RecordingTool()
    stella = Stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                arguments={"message": "hello"},
                capability="not-approved",
            )
        ),
        RecordingLLM(),
        ToolDispatcher([tool]),
        InMemoryMemory(),
    )

    result = stella.process(Context(user_input="Use an unknown tool"))

    assert result.tool_result == ToolResult(
        success=False, output="Tool capability unavailable."
    )
    assert tool.arguments == []


def test_stella_does_not_execute_tool_with_invalid_arguments() -> None:
    class ValidatingTool(RecordingTool):
        def validate_arguments(self, arguments: dict[str, object]) -> bool:
            return set(arguments) == {"message"} and isinstance(
                arguments["message"], str
            )

    tool = ValidatingTool()
    stella, _, _ = make_stella(
        Decision(
            DecisionKind.TOOL,
            arguments={"unexpected": "value"},
            capability="record",
        ),
        tool=tool,
    )

    result = stella.process(Context(user_input="Use the tool"))

    assert result.tool_result == ToolResult(
        success=False, output="Invalid tool arguments."
    )
    assert tool.arguments == []


def test_stella_sends_tool_result_to_llm_for_final_response() -> None:
    llm = RecordingLLM(response="The tool returned hello.")
    tool = RecordingTool()
    stella, _, _ = make_stella(
        Decision(
            DecisionKind.TOOL,
            arguments={"message": "hello"},
            capability="record",
        ),
        llm=llm,
        tool=tool,
    )

    result = stella.process(
        Context(
            user_input="Please echo hello",
            conversation_history=[Message(role="user", content="Earlier")],
        )
    )

    payload = json.loads(llm.messages[0][1].content)
    assert result.response == "The tool returned hello."
    assert payload["user_input"] == "Please echo hello"
    assert payload["conversation_history"] == [
        {"role": "user", "content": "Earlier"}
    ]
    assert payload["decision"] == {
        "kind": "tool",
        "content": None,
        "arguments": {"message": "hello"},
        "capability": "record",
    }
    assert payload["tool_result"] == {
        "success": True,
        "output": "tool output",
    }


def test_stella_runs_two_steps_and_feeds_observation_to_brain() -> None:
    brain = SequenceBrain(
        [
            Decision(
                DecisionKind.TOOL,
                arguments={"message": "first"},
                capability="record",
            ),
            Decision(DecisionKind.ANSWER),
        ]
    )
    llm = RecordingLLM(response="Both steps completed.")
    tool = RecordingTool()
    stella = Stella(
        brain,
        llm,
        ToolDispatcher([tool]),
        InMemoryMemory(),
        max_tool_steps=2,
    )

    result = stella.process(Context(user_input="Run the next step."))

    assert result.response == "Both steps completed."
    assert tool.arguments == [{"message": "first"}]
    assert len(brain.contexts) == 2
    assert brain.contexts[0].tool_observations == []
    assert brain.contexts[1].tool_observations[0].capability == "record"
    assert brain.contexts[1].tool_observations[0].success is True
    assert brain.contexts[1].tool_observations[0].output == "tool output"
    assert [step.decision.kind for step in result.step_trace] == [
        DecisionKind.TOOL,
        DecisionKind.ANSWER,
    ]
    payload = json.loads(llm.messages[0][1].content)
    assert payload["tool_observations"] == [
        {
            "capability": "record",
            "arguments": {"message": "first"},
            "success": True,
            "output": "tool output",
        }
    ]


def test_stella_bounds_multi_step_observations_and_keeps_latest() -> None:
    decisions = [
        Decision(
            DecisionKind.TOOL,
            arguments={"step": index},
            capability="record",
        )
        for index in range(10)
    ] + [Decision(DecisionKind.ANSWER)]
    brain = SequenceBrain(decisions)
    stella = Stella(
        brain,
        RecordingLLM(response="Completed."),
        ToolDispatcher([RecordingTool()]),
        InMemoryMemory(),
        max_tool_steps=10,
    )

    result = stella.process(Context(user_input="Run all steps."))

    assert result.response == "Completed."
    observations = brain.contexts[-1].tool_observations
    assert len(observations) == 8
    assert [observation.arguments["step"] for observation in observations] == list(
        range(2, 10)
    )
    assert observations[-1].arguments["step"] == 9


def test_successful_tool_outcome_can_produce_one_memory_write() -> None:
    item = MemoryItem(content="The user prefers tea")
    brain = SequenceBrain(
        [
            Decision(
                DecisionKind.TOOL,
                arguments={"message": "The user prefers tea"},
                capability="record",
            ),
            Decision(
                DecisionKind.ANSWER,
                content="I recorded the durable result.",
                memory_write=MemoryWriteRequest(item),
            ),
        ]
    )
    memory = RecordingMemory()
    stella = Stella(
        brain,
        RecordingLLM(response="Recorded."),
        ToolDispatcher([RecordingTool()]),
        memory,
        max_tool_steps=2,
        approval_provider=lambda request: ToolApproval(request, True),
    )

    result = stella.process(Context(user_input="Record the result."))

    assert result.memory_write is not None
    assert result.memory_write.item == item
    assert memory.retrieve() == [item]
    assert memory.write_count == 1


def test_filesystem_read_outcome_can_produce_independent_memory(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "profile.txt").write_text(
        "The user prefers tea.", encoding="utf-8"
    )
    item = MemoryItem(content="The user prefers tea")
    brain = SequenceBrain(
        [
            Decision(
                DecisionKind.TOOL,
                arguments={"path": "profile.txt"},
                capability="filesystem_read",
            ),
            Decision(
                DecisionKind.ANSWER,
                memory_write=MemoryWriteRequest(item),
            ),
        ]
    )
    memory = RecordingMemory()
    stella = Stella(
        brain,
        RecordingLLM(response="Recorded."),
        ToolDispatcher([FileSystemReadTool(workspace)]),
        memory,
        max_tool_steps=2,
        approval_provider=lambda request: ToolApproval(request, True),
    )

    result = stella.process(Context(user_input="Read the profile file."))

    assert result.tool_result == ToolResult(
        success=True, output="The user prefers tea."
    )
    assert result.memory_write is not None
    assert memory.retrieve() == [item]


def test_observation_grounded_memory_write_is_not_stored_without_approval(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text(
        "The notes mention a fact worth keeping.", encoding="utf-8"
    )
    item = MemoryItem(content="INJECTED-MEMORY-FACT")
    llm = RecordingLLM(response="I read the notes.")
    stella = Stella(
        SequenceBrain(
            [
                Decision(
                    DecisionKind.TOOL,
                    arguments={"path": "notes.txt"},
                    capability="filesystem_read",
                ),
                Decision(
                    DecisionKind.ANSWER,
                    content="I will remember that.",
                    memory_write=MemoryWriteRequest(item),
                ),
            ]
        ),
        llm,
        ToolDispatcher([FileSystemReadTool(workspace)]),
        RecordingMemory(),
        max_tool_steps=2,
    )

    result = stella.process(Context(user_input="Read the notes file."))

    assert isinstance(stella.memory, RecordingMemory)
    assert stella.memory.write_count == 0
    assert stella.memory.retrieve() == []
    assert result.memory_write is not None
    assert result.memory_write.written is False
    assert result.decision.memory_write is None
    assert "not approved" in result.response
    # The refused proposal must not reach response synthesis as pending work.
    assert "INJECTED-MEMORY-FACT" not in json.dumps(
        [[message.content for message in call] for call in llm.messages]
    )
    approval_events = [
        event
        for event in result.interaction_trace.events
        if isinstance(event, ApprovalEvent)
    ]
    assert approval_events == [ApprovalEvent("memory_write", False)]


def test_explicit_provider_denial_blocks_observation_grounded_memory_write(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text(
        "The notes mention a fact worth keeping.", encoding="utf-8"
    )
    item = MemoryItem(content="INJECTED-MEMORY-FACT")
    requested: list[ApprovalRequest] = []

    def deny(request: ApprovalRequest) -> ToolApproval:
        requested.append(request)
        return ToolApproval(request=request, approved=False)

    memory = RecordingMemory()
    stella = Stella(
        SequenceBrain(
            [
                Decision(
                    DecisionKind.TOOL,
                    arguments={"path": "notes.txt"},
                    capability="filesystem_read",
                ),
                Decision(
                    DecisionKind.ANSWER,
                    content="I will remember that.",
                    memory_write=MemoryWriteRequest(item),
                ),
            ]
        ),
        RecordingLLM(response="I read the notes."),
        ToolDispatcher([FileSystemReadTool(workspace)]),
        memory,
        max_tool_steps=2,
        approval_provider=deny,
    )

    result = stella.process(Context(user_input="Read the notes file."))

    assert requested == [
        ApprovalRequest("memory_write", {"content": "INJECTED-MEMORY-FACT"})
    ]
    assert memory.write_count == 0
    assert result.memory_write is not None
    assert result.memory_write.written is False
    assert "not approved" in result.response


def test_user_grounded_memory_write_needs_no_provider() -> None:
    memory = RecordingMemory()
    item = MemoryItem(content="The user prefers tea")
    stella = Stella(
        SequenceBrain(
            [
                Decision(
                    DecisionKind.ANSWER,
                    content="I will remember that.",
                    memory_write=MemoryWriteRequest(item),
                )
            ]
        ),
        RecordingLLM(response="Notified."),
        ToolDispatcher([]),
        memory,
    )

    result = stella.process(Context(user_input="Remember that I prefer tea."))

    assert memory.write_count == 1
    assert result.memory_write is not None
    assert result.memory_write.written is True
    assert "not approved" not in result.response


def test_failed_or_irrelevant_tool_outcomes_do_not_create_memory() -> None:
    item = MemoryItem(content="A failed or irrelevant result")
    scenarios = [
        (
            FailingResultTool(),
            Decision(
                DecisionKind.ANSWER,
                memory_write=MemoryWriteRequest(item),
            ),
        ),
        (
            RecordingTool(),
            Decision(DecisionKind.ANSWER),
        ),
    ]

    for tool, final_decision in scenarios:
        memory = RecordingMemory()
        stella = Stella(
            SequenceBrain(
                [
                    Decision(
                        DecisionKind.TOOL,
                        arguments={"message": "temporary output"},
                        capability="record",
                    ),
                    final_decision,
                ]
            ),
            RecordingLLM(),
            ToolDispatcher([tool]),
            memory,
            max_tool_steps=2,
        )

        result = stella.process(Context(user_input="Process the result."))

        assert result.memory_write is None
        assert memory.retrieve() == []
        assert memory.write_count == 0


def test_outcome_memory_persists_and_changes_fresh_stella_behavior(tmp_path) -> None:
    database_path = tmp_path / "outcome-memory.db"
    item = MemoryItem(content="The user prefers tea")

    with SQLiteMemory(database_path) as first_memory:
        first_stella = Stella(
            SequenceBrain(
                [
                    Decision(
                        DecisionKind.TOOL,
                        arguments={"message": "The user prefers tea"},
                        capability="record",
                    ),
                    Decision(
                        DecisionKind.ANSWER,
                        memory_write=MemoryWriteRequest(item),
                    ),
                ]
            ),
            RecordingLLM(response="Recorded."),
            ToolDispatcher([RecordingTool()]),
            first_memory,
            max_tool_steps=2,
            approval_provider=lambda request: ToolApproval(request, True),
        )
        result = first_stella.process(Context(user_input="Record the result."))

        assert result.memory_write is not None

    second_brain = MemoryAwareBrain()
    with SQLiteMemory(database_path) as second_memory:
        second_stella = Stella(
            second_brain,
            RecordingLLM(response="Tea is the remembered preference."),
            RecordingTool(),
            second_memory,
        )
        result = second_stella.process(Context(user_input="tea"))

    assert second_brain.contexts[0].retrieved_memories == [item]
    assert result.response == "Tea is the remembered preference."


def test_persisted_preference_changes_later_decision_from_ask_to_answer(
    tmp_path,
) -> None:
    database_path = tmp_path / "decision-continuity.db"
    item = MemoryItem(
        content="The user prefers jasmine tea in the evening."
    )

    with SQLiteMemory(database_path) as first_memory:
        first_stella, _, _ = make_stella(
            Decision(
                DecisionKind.ANSWER,
                content="I will remember that.",
                memory_write=MemoryWriteRequest(item),
            ),
            memory=first_memory,
        )
        first_result = first_stella.process(
            Context(user_input="Remember my evening tea preference.")
        )
        assert first_result.memory_write is not None

    later_question = "What tea should I have this evening?"
    remembered_brain = EveningTeaPreferenceBrain()
    with SQLiteMemory(database_path) as remembered_memory:
        remembered_stella = Stella(
            remembered_brain,
            RecordingLLM(),
            RecordingTool(),
            remembered_memory,
        )
        remembered_result = remembered_stella.process(
            Context(user_input=later_question)
        )

    control_brain = EveningTeaPreferenceBrain()
    with SQLiteMemory(tmp_path / "no-memory.db") as control_memory:
        control_stella = Stella(
            control_brain,
            RecordingLLM(),
            RecordingTool(),
            control_memory,
        )
        control_result = control_stella.process(
            Context(user_input=later_question)
        )

    assert remembered_brain.contexts[0].retrieved_memories == [item]
    assert remembered_result.decision.kind is DecisionKind.ANSWER
    assert remembered_result.response == "Have jasmine tea this evening."
    assert remembered_result.needs_more_information is False
    assert control_brain.contexts[0].retrieved_memories == []
    assert control_result.decision.kind is DecisionKind.ASK
    assert control_result.needs_more_information is True


class RelevanceObligingLLM(LLMClient):
    """Acts on the runtime relevance flag: relevant memory answers, background asks."""

    def __init__(self) -> None:
        self.messages: list[list[Message | dict[str, str]]] = []

    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        self.messages.append(messages)
        payload = json.loads(messages[1].content)
        relevant = [
            memory
            for memory in payload["retrieved_memories"]
            if memory["relevant_to_current_request"]
        ]
        if relevant:
            return json.dumps(
                {
                    "kind": "answer",
                    "content": (
                        f"Following the remembered preference: "
                        f"{relevant[0]['content']}"
                    ),
                }
            )
        return json.dumps(
            {
                "kind": "ask",
                "content": "What should this be based on?",
            }
        )


def test_relevant_memory_changes_llm_brain_decision_from_ask_to_answer() -> None:
    memory = InMemoryMemory()
    memory.store(
        MemoryItem(content="The user prefers jasmine tea in the evening.")
    )
    llm = RelevanceObligingLLM()
    stella = Stella(LLMBrain(llm), llm, RecordingTool(), memory)

    result = stella.process(
        Context(user_input="What tea should I have this evening?")
    )

    assert result.decision.kind is DecisionKind.ANSWER
    assert "jasmine tea" in (result.response or "")
    assert result.needs_more_information is False


def test_history_only_memory_stays_irrelevant_to_llm_brain_decision() -> None:
    memory = InMemoryMemory()
    memory.store(
        MemoryItem(content="The user prefers jasmine tea in the evening.")
    )
    llm = RelevanceObligingLLM()
    stella = Stella(LLMBrain(llm), llm, RecordingTool(), memory)

    result = stella.process(
        Context(
            user_input="What should I prepare?",
            conversation_history=[
                Message(
                    role="user",
                    content="I am having a quiet evening tea at home.",
                )
            ],
        )
    )

    decision_payload = json.loads(llm.messages[0][1].content)
    assert [
        memory_entry["relevant_to_current_request"]
        for memory_entry in decision_payload["retrieved_memories"]
    ] == [False]
    assert result.decision.kind is DecisionKind.ASK
    assert result.needs_more_information is True


def test_process_caps_retrieved_memories_to_the_most_relevant() -> None:
    memory = InMemoryMemory()
    medium = [
        MemoryItem(content=f"weather forecast archive {index}")
        for index in range(4)
    ]
    strong = [
        MemoryItem(
            content=f"weather forecast tomorrow briefing {index}",
            memory_type=(
                MemoryType.EPISODIC if index == 0 else MemoryType.SEMANTIC
            ),
        )
        for index in range(3)
    ]
    for item in [*medium[:2], *strong, *medium[2:]]:
        memory.store(item)
    stella, _, _ = make_stella(Decision(DecisionKind.ANSWER), memory=memory)

    result = stella.process(
        Context(user_input="weather forecast tomorrow morning storm")
    )

    assert len(result.retrieved_memories) == MAX_RETRIEVED_MEMORIES
    assert [item.content for item in result.retrieved_memories] == [
        strong[2].content,
        strong[1].content,
        strong[0].content,
        medium[3].content,
        medium[2].content,
    ]
    episodic = [
        item
        for item in result.retrieved_memories
        if item.memory_type is MemoryType.EPISODIC
    ]
    assert [item.content for item in episodic] == [strong[0].content]
    assert all(
        item.scope is MemoryScope.USER for item in result.retrieved_memories
    )


def test_current_request_history_and_memory_change_tool_decision() -> None:
    item = MemoryItem(
        content="The user prefers jasmine tea in the evening."
    )
    memory = InMemoryMemory()
    memory.store(item)
    history = [
        Message(
            role="user",
            content="I am having a quiet evening tea at home.",
        )
    ]
    brain = ContextualTeaBrain()
    tool = RecordingTool()
    stella = Stella(
        brain,
        RecordingLLM(response="Prepared the tea."),
        tool,
        memory,
    )

    result = stella.process(
        Context(
            user_input="What should I prepare?",
            conversation_history=history,
        )
    )

    assert result.decision.kind is DecisionKind.TOOL
    assert result.tool_result == ToolResult(
        success=True,
        output="tool output",
    )
    assert result.response == "Prepared the tea."
    assert tool.arguments == [
        {"message": "Prepare jasmine tea for this evening."}
    ]
    assert brain.contexts[0].user_input == "What should I prepare?"
    assert brain.contexts[0].conversation_history == history
    assert brain.contexts[0].retrieved_memories == [item]

    control_brain = ContextualTeaBrain()
    control_tool = RecordingTool()
    control_stella = Stella(
        control_brain,
        RecordingLLM(),
        control_tool,
        InMemoryMemory(),
    )
    control_result = control_stella.process(
        Context(user_input="What should I prepare?")
    )

    assert control_result.decision.kind is DecisionKind.ASK
    assert control_result.needs_more_information is True
    assert control_tool.arguments == []


def test_sufficient_context_selects_action_with_required_target() -> None:
    memory = InMemoryMemory()
    memory.store(
        MemoryItem(content="The user prefers jasmine tea in the evening.")
    )
    brain = UncertaintyAwareTeaNoteBrain()
    tool = RecordingTool()
    stella = Stella(
        brain,
        RecordingLLM(response="Saved the evening plan."),
        tool,
        memory,
    )

    result = stella.process(
        Context(
            user_input="Save my evening tea plan.",
            conversation_history=[
                Message(
                    role="user",
                    content="Use plans/evening.txt for the note.",
                )
            ],
        )
    )

    assert result.decision.kind is DecisionKind.TOOL
    assert tool.arguments == [
        {
            "path": "plans/evening.txt",
            "content": "Evening plan: jasmine tea.",
        }
    ]
    assert result.response == "Saved the evening plan."


def test_insufficient_context_asks_for_missing_action_target() -> None:
    memory = InMemoryMemory()
    memory.store(
        MemoryItem(content="The user prefers jasmine tea in the evening.")
    )
    brain = UncertaintyAwareTeaNoteBrain()
    tool = RecordingTool()
    stella = Stella(brain, RecordingLLM(), tool, memory)

    result = stella.process(
        Context(
            user_input="Save my evening tea plan.",
            conversation_history=[
                Message(role="user", content="Use the usual note.")
            ],
        )
    )

    assert result.decision.kind is DecisionKind.ASK
    assert result.needs_more_information is True
    assert result.response == "Which file should I use for the evening plan?"
    assert tool.arguments == []


def test_irrelevant_retrieved_memory_does_not_cause_guessing() -> None:
    memory = InMemoryMemory()
    memory.store(
        MemoryItem(content="The user's evening plan color is blue.")
    )
    brain = UncertaintyAwareTeaNoteBrain()
    tool = RecordingTool()
    stella = Stella(brain, RecordingLLM(), tool, memory)

    result = stella.process(
        Context(
            user_input="Save my evening tea plan.",
            conversation_history=[
                Message(role="user", content="Use the usual note.")
            ],
        )
    )

    assert result.retrieved_memories == memory.retrieve(
        "Save my evening tea plan. Use the usual note."
    )
    assert result.retrieved_memories
    assert result.decision.kind is DecisionKind.ASK
    assert result.needs_more_information is True
    assert tool.arguments == []


def test_persisted_behavioral_preference_changes_relevant_responses_consistently(
    tmp_path,
) -> None:
    database_path = tmp_path / "behavioral-preference.db"
    preference = MemoryItem(
        content="The user prefers one-line status update responses."
    )

    with SQLiteMemory(database_path) as first_memory:
        first_stella, _, _ = make_stella(
            Decision(
                DecisionKind.ANSWER,
                content="I will remember that.",
                memory_write=MemoryWriteRequest(preference),
            ),
            memory=first_memory,
        )
        first_result = first_stella.process(
            Context(user_input="Remember my status update preference.")
        )
        assert first_result.memory_write is not None

    queries = [
        "Give me a status update on the deployment.",
        "Give me a status update on the release.",
    ]
    for query in queries:
        with SQLiteMemory(database_path) as memory:
            brain = StatusPreferenceBrain()
            stella = Stella(brain, RecordingLLM(), RecordingTool(), memory)
            result = stella.process(Context(user_input=query))

        assert brain.contexts[0].retrieved_memories == [preference]
        assert result.response == "Deployment is on track."
        assert "Tests are passing" not in result.response

    control_brain = StatusPreferenceBrain()
    control_stella = Stella(
        control_brain,
        RecordingLLM(),
        RecordingTool(),
        InMemoryMemory(),
    )
    control_result = control_stella.process(Context(user_input=queries[0]))

    assert control_brain.contexts[0].retrieved_memories == []
    assert "Tests are passing" in control_result.response


def test_stella_stops_deterministically_at_tool_step_limit() -> None:
    decisions = [
        Decision(
            DecisionKind.TOOL,
            arguments={"message": "first"},
            capability="record",
        ),
        Decision(
            DecisionKind.TOOL,
            arguments={"message": "second"},
            capability="record",
        ),
        Decision(
            DecisionKind.TOOL,
            arguments={"message": "third"},
            capability="record",
        ),
    ]
    brain = SequenceBrain(decisions)
    llm = RecordingLLM()
    tool = RecordingTool()
    dispatcher = ToolDispatcher([tool])
    stella = Stella(
        brain,
        llm,
        dispatcher,
        InMemoryMemory(),
        max_tool_steps=2,
    )

    result = stella.process(Context(user_input="Run bounded steps."))

    assert result.max_steps_reached is True
    assert result.response == "I reached the maximum number of tool steps."
    assert result.tool_result == ToolResult(success=True, output="tool output")
    assert tool.arguments == [
        {"message": "first"},
        {"message": "second"},
    ]
    assert len(dispatcher.audit_records) == 2
    assert result.step_trace[-1].decision.arguments == {"message": "third"}
    assert result.step_trace[-1].tool_result is None
    assert llm.messages == []


def test_stella_feeds_failed_tool_result_to_next_brain_decision() -> None:
    brain = SequenceBrain(
        [
            Decision(
                DecisionKind.TOOL,
                arguments={"message": "fail"},
                capability="record",
            ),
            Decision(DecisionKind.ANSWER),
        ]
    )
    llm = RecordingLLM(response="The first step failed safely.")
    stella = Stella(
        brain,
        llm,
        ToolDispatcher([FailingResultTool()]),
        InMemoryMemory(),
        max_tool_steps=2,
    )

    result = stella.process(Context(user_input="Use the failing tool."))

    assert result.response == "The first step failed safely."
    assert brain.contexts[1].tool_observations[0].success is False
    assert brain.contexts[1].tool_observations[0].output == (
        "tool reported failure"
    )
    assert result.step_trace[0].tool_result == ToolResult(
        success=False, output="tool reported failure"
    )


def test_stella_approves_each_dangerous_multi_step_action() -> None:
    brain = SequenceBrain(
        [
            Decision(
                DecisionKind.TOOL,
                arguments={"value": "x"},
                capability="approval_test",
            ),
            Decision(DecisionKind.ANSWER),
        ]
    )
    tool = ApprovalRecordingTool()
    requests: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> ToolApproval:
        requests.append(request)
        return ToolApproval(request=request, approved=True)

    stella = Stella(
        brain,
        RecordingLLM(response="Approved."),
        ToolDispatcher([tool]),
        InMemoryMemory(),
        approval_provider=approve,
        max_tool_steps=2,
    )

    result = stella.process(Context(user_input="Run the approved step."))

    assert result.response == "Approved."
    assert requests == [ApprovalRequest("approval_test", {"value": "x"})]
    assert tool.arguments == [{"value": "x"}]


def test_stella_synthesizes_response_for_failed_tool_result() -> None:
    llm = RecordingLLM(response="The tool could not complete the request.")
    stella, _, _ = make_stella(
        Decision(
            DecisionKind.TOOL,
            arguments={"message": "hello"},
            capability="record",
        ),
        llm=llm,
        tool=FailingResultTool(),
    )

    result = stella.process(Context(user_input="Use the tool"))

    assert result.tool_result == ToolResult(
        success=False, output="tool reported failure"
    )
    assert result.response == "The tool could not complete the request."


def test_stella_converts_unexpected_tool_exception_to_failed_result() -> None:
    llm = RecordingLLM(response="The tool failed safely.")
    stella, _, _ = make_stella(
        Decision(
            DecisionKind.TOOL,
            arguments={"message": "hello"},
            capability="record",
        ),
        llm=llm,
        tool=RaisingTool(),
    )

    result = stella.process(Context(user_input="Use the tool"))

    assert result.tool_result == ToolResult(
        success=False, output="Tool execution failed."
    )
    assert result.response == "The tool failed safely."
    assert len(llm.messages) == 1


@pytest.mark.parametrize("capability", [None, "unknown", "echo"])
def test_stella_fails_closed_for_unavailable_tool_capability(
    capability: str | None,
) -> None:
    tool = RecordingTool()
    stella, _, _ = make_stella(
        Decision(
            DecisionKind.TOOL,
            arguments={"message": "hello"},
            capability=capability,
        ),
        tool=tool,
    )

    result = stella.process(Context(user_input="Use a tool"))

    assert result.tool_result == ToolResult(
        success=False, output="Tool capability unavailable."
    )
    assert tool.arguments == []


def test_stella_does_nothing_without_calling_dependencies() -> None:
    stella, llm, tool = make_stella(Decision(DecisionKind.DO_NOTHING))

    result = stella.process(Context(user_input="do_nothing"))

    assert result.decision.kind is DecisionKind.DO_NOTHING
    assert result.response is None
    assert result.tool_result is None
    assert llm.messages == []
    assert tool.arguments == []


def test_stella_passes_retrieved_memories_to_brain_without_mutating_context() -> None:
    memory = InMemoryMemory()
    item = MemoryItem(content="User likes tea")
    memory.store(item)
    brain = RecordingBrain(Decision(DecisionKind.ANSWER))
    llm = RecordingLLM()
    original_context = Context(user_input="tea")
    stella = Stella(brain, llm, RecordingTool(), memory)

    result = stella.process(original_context)

    assert brain.contexts[0].retrieved_memories == [item]
    assert result.retrieved_memories == [item]
    assert original_context.retrieved_memories == []


def test_stella_does_not_write_to_memory_automatically() -> None:
    memory = InMemoryMemory()
    item = MemoryItem(content="User likes tea")
    memory.store(item)
    stella, _, _ = make_stella(
        Decision(DecisionKind.ANSWER), memory=memory
    )

    stella.process(Context(user_input="tea"))

    assert memory.retrieve() == [item]


def test_explicit_memory_write_decision_writes_exactly_once() -> None:
    memory = RecordingMemory()
    item = MemoryItem(content="User likes tea")
    decision = Decision(
        DecisionKind.ANSWER,
        content="I will remember that.",
        memory_write=MemoryWriteRequest(item),
    )
    stella, _, _ = make_stella(decision, memory=memory)

    result = stella.process(Context(user_input="remember this"))

    assert memory.write_count == 1
    assert memory.retrieve() == [item]
    assert result.memory_write is not None
    assert result.memory_write.item == item
    assert result.memory_write.written is True


def test_normal_interaction_does_not_write_memory() -> None:
    memory = RecordingMemory()
    stella, _, _ = make_stella(Decision(DecisionKind.ANSWER), memory=memory)

    stella.process(Context(user_input="hello"))

    assert memory.write_count == 0


def test_retrieved_memories_are_not_written_back() -> None:
    memory = RecordingMemory()
    item = MemoryItem(content="User likes tea")
    memory.store(item)
    memory.write_count = 0
    stella, _, _ = make_stella(Decision(DecisionKind.ANSWER), memory=memory)

    stella.process(Context(user_input="tea"))

    assert memory.write_count == 0
    assert memory.retrieve() == [item]


def test_explicit_memory_write_flow_works_with_sqlite(tmp_path) -> None:
    database_path = tmp_path / "memory.db"
    memory = SQLiteMemory(database_path)
    item = MemoryItem(content="Favorite language is Rust")
    stella, _, _ = make_stella(
        Decision(
            DecisionKind.ANSWER,
            memory_write=MemoryWriteRequest(item),
        ),
        memory=memory,
    )

    try:
        result = stella.process(Context(user_input="remember Rust"))
    finally:
        memory.close()

    assert result.memory_write is not None
    assert result.memory_write.written is True
    with SQLiteMemory(database_path) as reopened:
        assert reopened.retrieve() == [item]


def test_llm_brain_memory_write_reaches_sqlite_memory(tmp_path) -> None:
    database_path = tmp_path / "memory.db"
    item = MemoryItem(content="Favorite language is Rust")
    llm = SequenceLLM(
        [
            (
                '{"kind": "answer", "content": "I will remember it", '
                '"memory_write": {"content": "Favorite language is Rust"}}'
            ),
            "Acknowledged.",
        ]
    )

    with SQLiteMemory(database_path) as memory:
        stella = Stella(LLMBrain(llm), llm, RecordingTool(), memory)
        result = stella.process(Context(user_input="Remember Rust"))

        assert result.response == "I will remember it"
        assert result.memory_write is not None
        assert memory.retrieve() == [item]


def test_answer_generation_receives_memories_and_selected_decision() -> None:
    memory = InMemoryMemory()
    memory.store(MemoryItem(content="User likes tea"))
    llm = SequenceLLM(
        [
            '{"kind": "answer", "content": "Use the memory"}',
        ]
    )
    stella = Stella(
        LLMBrain(llm), llm, RecordingTool(), memory
    )

    result = stella.process(
        Context(
            user_input="tea",
            conversation_history=[Message(role="user", content="Earlier")],
        )
    )

    decision_context = json.loads(llm.messages[0][1].content)
    assert result.response == "Use the memory"
    assert decision_context["user_input"] == "tea"
    assert decision_context["conversation_history"] == [
        {"role": "user", "content": "Earlier"}
    ]
    assert decision_context["retrieved_memories"] == [
        {
            "content": "User likes tea",
            "memory_type": "semantic",
            "scope": "user",
            "relevant_to_current_request": True,
        }
    ]
    assert len(llm.messages) == 1


def test_persisted_memory_changes_later_stella_behavior(tmp_path) -> None:
    database_path = tmp_path / "memory.db"
    item = MemoryItem(content="favorite programming language: Rust")

    with SQLiteMemory(database_path) as first_memory:
        first_stella, _, _ = make_stella(
            Decision(
                DecisionKind.ANSWER,
                memory_write=MemoryWriteRequest(item),
            ),
            memory=first_memory,
        )
        first_stella.process(Context(user_input="Remember my language"))

    second_brain = MemoryAwareBrain()
    second_llm = RecordingLLM(response="Rust")
    with SQLiteMemory(database_path) as second_memory:
        second_stella = Stella(
            second_brain, second_llm, RecordingTool(), second_memory
        )
        result = second_stella.process(
            Context(user_input="favorite programming language")
        )

    assert second_brain.contexts[0].retrieved_memories == [item]
    assert result.decision.kind is DecisionKind.ANSWER
    assert result.response == "Rust"


# ------------------------------------------------- cooperative cancellation (A5)


def test_cancellation_before_first_decision_runs_nothing() -> None:
    brain = RecordingBrain(
        Decision(
            DecisionKind.TOOL, capability="record", arguments={"message": "x"}
        )
    )
    llm = RecordingLLM()
    tool = RecordingTool()
    stella = Stella(brain, llm, ToolDispatcher([tool]), InMemoryMemory())

    result = stella.process(
        Context(user_input="actually stop"), should_cancel=lambda: True
    )

    assert result.cancelled is True
    assert result.response is None
    assert brain.contexts == []
    assert llm.messages == []
    assert tool.arguments == []


def test_cancellation_after_decision_skips_approval_and_execution() -> None:
    brain = RecordingBrain(
        Decision(
            DecisionKind.TOOL,
            capability="approval_test",
            arguments={"value": "x"},
        )
    )
    tool = ApprovalRecordingTool()
    requests: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> ToolApproval:
        requests.append(request)
        return ToolApproval(request=request, approved=True)

    stella = Stella(
        brain,
        RecordingLLM(response="never reached"),
        ToolDispatcher([tool]),
        InMemoryMemory(),
        approval_provider=approve,
    )
    # False at the first checkpoint, True immediately after the decision:
    # the proposed effect must be discarded before any of its steps.
    ticks = iter([False, True])

    result = stella.process(
        Context(user_input="cancel this"), should_cancel=lambda: next(ticks)
    )

    assert result.cancelled is True
    assert len(brain.contexts) == 1
    assert requests == []
    assert tool.arguments == []
    assert result.response is None


def test_cancellation_between_steps_keeps_executed_step_and_skips_next() -> None:
    tool = RecordingTool()
    brain = SequenceBrain(
        [
            Decision(
                DecisionKind.TOOL,
                capability="record",
                arguments={"message": "first"},
            ),
            Decision(
                DecisionKind.TOOL,
                capability="record",
                arguments={"message": "second"},
            ),
            Decision(DecisionKind.ANSWER),
        ]
    )
    stella = Stella(
        brain,
        RecordingLLM(response="never synthesized"),
        ToolDispatcher([tool]),
        InMemoryMemory(),
        max_tool_steps=3,
    )
    # Cancel as soon as the first step has finished: the check reads the
    # app's own record of what executed. An executed step is never undone
    # or interrupted — cancel lands between steps.
    result = stella.process(
        Context(user_input="multi-step, stop after one"),
        should_cancel=lambda: len(tool.arguments) >= 1,
    )

    assert result.cancelled is True
    assert tool.arguments == [{"message": "first"}]
    assert len(brain.contexts) == 1
    assert result.response is None
    assert result.step_trace[0].tool_result == ToolResult(
        success=True, output="tool output"
    )


def test_turn_without_should_cancel_is_unchanged() -> None:
    # Embedding callers that never pass the keyword keep exactly their
    # previous behavior.
    stella, _, tool = make_stella(
        Decision(
            DecisionKind.TOOL, capability="record", arguments={"message": "go"}
        )
    )

    result = stella.process(Context(user_input="as before"))

    assert result.cancelled is False
    assert tool.arguments == [{"message": "go"}]
    assert result.response == "answer"
