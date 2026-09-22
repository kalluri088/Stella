"""Deterministic Stella behavior evaluation cases."""

import pytest

from stella.brain import Brain, Decision, DecisionKind, LLMBrain
from stella.context import Context
from stella.llm import (
    LLMClient,
    LLMResponse,
    LLMToolCall,
    LLMToolDefinition,
    Message,
    ToolUseMode,
)
from stella.memory import InMemoryMemory, MemoryItem, MemoryWriteRequest
from stella.proactivity import (
    DelegatedAction,
    DueTaskEvent,
    DueTaskStatus,
    ProactivityDelegation,
)
from stella.stella import Stella
from stella.tools import (
    ApprovalRequest,
    RiskLevel,
    Tool,
    ToolApproval,
    ToolDispatcher,
    ToolResult,
)

from .harness import (
    EvaluationCase,
    EvaluationExpectation,
    EvaluationObservation,
    evaluate_case,
)


class FixedBrain(Brain):
    answer_content_is_final = True

    def __init__(self, decision: Decision) -> None:
        self.decision = decision

    def decide(self, context: Context) -> Decision:
        del context
        return self.decision


class SequenceBrain(Brain):
    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = list(decisions)

    def decide(self, context: Context) -> Decision:
        del context
        return self.decisions.pop(0)


class MemoryAwareBrain(Brain):
    answer_content_is_final = True

    def decide(self, context: Context) -> Decision:
        if context.retrieved_memories:
            return Decision(
                DecisionKind.ANSWER,
                content="Use the remembered concise format.",
            )
        return Decision(
            DecisionKind.ASK,
            content="Which format should I use?",
        )


class RecordingLLM(LLMClient):
    def __init__(self, response: str = "evaluation response") -> None:
        self.response = response

    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        del messages
        return self.response


class NativeEvaluationLLM(LLMClient):
    def __init__(self) -> None:
        self.modes: list[ToolUseMode] = []

    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        del messages
        return "The tool result was used."

    def chat_with_tools(
        self,
        messages: list[Message | dict[str, str]],
        tools: list[LLMToolDefinition],
        tool_choice: ToolUseMode = ToolUseMode.AUTO,
    ) -> LLMResponse:
        del messages, tools
        self.modes.append(tool_choice)
        return LLMResponse(
            tool_calls=(
                LLMToolCall(
                    name="current_value",
                    arguments={"value": "live"},
                ),
            )
        )


class EvaluationTool(Tool):
    def __init__(
        self,
        name: str,
        *,
        success: bool = True,
        risk: RiskLevel = RiskLevel.SAFE,
    ) -> None:
        self._name = name
        self.success = success
        self._risk = risk
        self.calls: list[dict[str, object]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Reads the current local value for evaluation."

    @property
    def argument_schema(self) -> dict[str, object]:
        return {"value": "string"}

    @property
    def risk_level(self) -> RiskLevel:
        return self._risk

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return set(arguments) == {"value"} and isinstance(
            arguments["value"], str
        )

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        self.calls.append(dict(arguments))
        return ToolResult(self.success, "evaluation tool output")


def _stella(
    brain: Brain,
    dispatcher: ToolDispatcher,
    *,
    llm: LLMClient | None = None,
    memory: InMemoryMemory | None = None,
    approval_provider=None,
    max_tool_steps: int = 1,
) -> Stella:
    client = llm or RecordingLLM()
    return Stella(
        brain,
        client,
        dispatcher,
        memory or InMemoryMemory(),
        approval_provider=approval_provider,
        max_tool_steps=max_tool_steps,
    )


def test_evaluation_harness_covers_direct_answer_and_ask() -> None:
    cases = [
        EvaluationCase(
            "direct answer",
            lambda: EvaluationObservation.from_stella(
                _stella(
                    FixedBrain(
                        Decision(DecisionKind.ANSWER, content="42")
                    ),
                    ToolDispatcher([]),
                ).process(Context(user_input="What is 2 + 2?"))
            ),
            EvaluationExpectation(
                decision_kinds=("answer",),
                tool_successes=(),
                memory_written=False,
                trace_events=(
                    "InputReceivedEvent",
                    "MemoryRetrievedEvent",
                    "DecisionEvent",
                    "FinalResponseEvent",
                    "MemoryWriteEvent",
                ),
            ),
        ),
        EvaluationCase(
            "clarification",
            lambda: EvaluationObservation.from_stella(
                _stella(
                    FixedBrain(
                        Decision(DecisionKind.ASK, content="Which one?")
                    ),
                    ToolDispatcher([]),
                ).process(Context(user_input="Do the ambiguous thing."))
            ),
            EvaluationExpectation(
                decision_kinds=("ask",),
                tool_successes=(),
                trace_events=(
                    "InputReceivedEvent",
                    "MemoryRetrievedEvent",
                    "DecisionEvent",
                    "FinalResponseEvent",
                    "MemoryWriteEvent",
                ),
            ),
        ),
    ]

    for case in cases:
        evaluate_case(case)


def test_evaluation_harness_covers_required_native_tool_use() -> None:
    llm = NativeEvaluationLLM()
    tool = EvaluationTool("current_value")
    stella = _stella(
        LLMBrain(llm, ToolDispatcher([tool])),
        ToolDispatcher([tool]),
        llm=llm,
    )

    result = stella.process(Context(user_input="What is the current value?"))
    observation = EvaluationObservation.from_stella(
        result,
        policy_modes=tuple(mode.value for mode in llm.modes),
    )

    EvaluationCase(
        "required tool use",
        lambda: observation,
        EvaluationExpectation(
            decision_kinds=("tool",),
            capabilities=("current_value",),
            tool_successes=(True,),
            policy_modes=("required",),
        ),
    ).expected.assert_matches("required tool use", observation)


@pytest.mark.parametrize("success", [True, False])
def test_evaluation_harness_covers_tool_outcomes(success: bool) -> None:
    tool = EvaluationTool("outcome", success=success)
    result = _stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                capability="outcome",
                arguments={"value": "input"},
            )
        ),
        ToolDispatcher([tool]),
    ).process(Context(user_input="Use the outcome tool."))

    observation = EvaluationObservation.from_stella(result)
    EvaluationCase(
        "successful tool" if success else "failed tool",
        lambda: observation,
        EvaluationExpectation(
            decision_kinds=("tool",),
            capabilities=("outcome",),
            tool_successes=(success,),
        ),
    ).expected.assert_matches("tool outcome", observation)


def test_evaluation_harness_covers_multi_step_tool_interaction() -> None:
    first = EvaluationTool("first")
    second = EvaluationTool("second")
    result = _stella(
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
                Decision(DecisionKind.ANSWER, content="complete"),
            ]
        ),
        ToolDispatcher([first, second]),
        max_tool_steps=3,
    ).process(Context(user_input="Use both tools."))

    observation = EvaluationObservation.from_stella(result)
    EvaluationCase(
        "multi-step",
        lambda: observation,
        EvaluationExpectation(
            decision_kinds=("tool", "tool", "answer"),
            capabilities=("first", "second"),
            tool_successes=(True, True),
        ),
    ).expected.assert_matches("multi-step", observation)


def test_evaluation_harness_covers_dangerous_approval() -> None:
    tool = EvaluationTool("dangerous", risk=RiskLevel.DANGEROUS)
    approvals: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> ToolApproval:
        approvals.append(request)
        return ToolApproval(request, approved=True)

    result = _stella(
        FixedBrain(
            Decision(
                DecisionKind.TOOL,
                capability="dangerous",
                arguments={"value": "approved"},
            )
        ),
        ToolDispatcher([tool]),
        approval_provider=approve,
    ).process(Context(user_input="Perform the approved action."))

    observation = EvaluationObservation.from_stella(result)
    EvaluationCase(
        "dangerous approval",
        lambda: observation,
        EvaluationExpectation(
            decision_kinds=("tool",),
            capabilities=("dangerous",),
            tool_successes=(True,),
            approval_decisions=(True,),
        ),
    ).expected.assert_matches("dangerous approval", observation)
    assert approvals == [ApprovalRequest("dangerous", {"value": "approved"})]


def test_evaluation_harness_covers_memory_retrieval_and_explicit_write() -> None:
    memory = InMemoryMemory()
    memory.store(
        MemoryItem("The user prefers concise technical explanations.")
    )
    result = _stella(
        MemoryAwareBrain(),
        ToolDispatcher([]),
        memory=memory,
    ).process(
        Context(
            user_input=(
                "What do you know about my technical explanation preference?"
            )
        )
    )

    observation = EvaluationObservation.from_stella(result)
    EvaluationCase(
        "memory retrieval",
        lambda: observation,
        EvaluationExpectation(
            decision_kinds=("answer",),
            memory_retrieved=1,
            memory_written=False,
        ),
    ).expected.assert_matches("memory retrieval", observation)

    write_memory = InMemoryMemory()
    request = MemoryWriteRequest(MemoryItem("User prefers concise output."))
    write_result = _stella(
        FixedBrain(
            Decision(
                DecisionKind.ANSWER,
                content="I will remember that.",
                memory_write=request,
            )
        ),
        ToolDispatcher([]),
        memory=write_memory,
    ).process(Context(user_input="Remember my concise preference."))

    write_observation = EvaluationObservation.from_stella(write_result)
    EvaluationCase(
        "explicit memory write",
        lambda: write_observation,
        EvaluationExpectation(
            decision_kinds=("answer",),
            memory_written=True,
        ),
    ).expected.assert_matches("explicit memory write", write_observation)
    assert write_memory.retrieve() == [request.item]


def test_evaluation_harness_distinguishes_irrelevant_memory() -> None:
    memory = InMemoryMemory()
    memory.store(MemoryItem("The user prefers jasmine tea."))
    result = _stella(
        MemoryAwareBrain(),
        ToolDispatcher([]),
        memory=memory,
    ).process(Context(user_input="Which file should I read?"))

    observation = EvaluationObservation.from_stella(result)
    EvaluationCase(
        "irrelevant memory",
        lambda: observation,
        EvaluationExpectation(
            decision_kinds=("ask",),
            memory_retrieved=0,
            memory_written=False,
        ),
    ).expected.assert_matches("irrelevant memory", observation)


def test_evaluation_harness_covers_proactivity_boundaries() -> None:
    def make_stella() -> Stella:
        return _stella(
            FixedBrain(Decision(DecisionKind.ANSWER, content="unused")),
            ToolDispatcher([]),
        )

    cases = [
        EvaluationCase(
            "proactivity inform",
            lambda: EvaluationObservation.from_proactivity(
                make_stella().handoff_due_task_event(
                    DueTaskEvent(
                        "eval-inform",
                        "Expense report",
                        DueTaskStatus.OPEN,
                        True,
                    ),
                    ProactivityDelegation(DelegatedAction.INFORM_DUE_TASK),
                )
            ),
            EvaluationExpectation(proactivity_kind="inform"),
        ),
        EvaluationCase(
            "proactivity ask",
            lambda: EvaluationObservation.from_proactivity(
                make_stella().handoff_due_task_event(
                    DueTaskEvent(
                        "eval-ask",
                        "Tax filing",
                        DueTaskStatus.OPEN,
                        True,
                    )
                )
            ),
            EvaluationExpectation(proactivity_kind="ask"),
        ),
        EvaluationCase(
            "proactivity do nothing",
            lambda: EvaluationObservation.from_proactivity(
                make_stella().handoff_due_task_event(
                    DueTaskEvent(
                        "eval-noop",
                        "Completed report",
                        DueTaskStatus.COMPLETED,
                        True,
                    ),
                    ProactivityDelegation(DelegatedAction.INFORM_DUE_TASK),
                )
            ),
            EvaluationExpectation(proactivity_kind="do_nothing"),
        ),
    ]

    for case in cases:
        evaluate_case(case)
