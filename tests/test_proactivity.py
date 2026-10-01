import pytest

from stella.brain import SimpleBrain
from stella.llm import LLMClient, Message
from stella.memory import InMemoryMemory, MemoryItem
from stella.proactivity import (
    DelegatedAction,
    DueTaskEvent,
    DueTaskStatus,
    ProactivityDecisionKind,
    ProactivityDelegation,
    UserFacingProactivityResult,
)
from stella.stella import Stella
from stella.tools import EchoTool


class SpyLLM(LLMClient):
    def __init__(self) -> None:
        self.messages: list[list[Message | dict[str, str]]] = []

    def chat(self, messages: list[Message | dict[str, str]]) -> str:
        self.messages.append(messages)
        return "model output"


def make_stella_for_event_check() -> tuple[Stella, SpyLLM]:
    llm = SpyLLM()
    return Stella(SimpleBrain(), llm, EchoTool(), InMemoryMemory()), llm


def test_meaningful_due_event_informs_with_scoped_delegation() -> None:
    stella, llm = make_stella_for_event_check()
    event = DueTaskEvent(
        event_id="event-1",
        task_title="Expense report",
        status=DueTaskStatus.OPEN,
        is_due=True,
    )
    delegation = ProactivityDelegation(
        DelegatedAction.INFORM_DUE_TASK,
        task_scope="Expense report",
    )

    result = stella.evaluate_due_task_event(event, delegation)

    assert result.kind is ProactivityDecisionKind.INFORM
    assert result.message == "Expense report is due today."
    assert llm.messages == []


@pytest.mark.parametrize(
    ("status", "is_due", "already_handled"),
    [
        (DueTaskStatus.COMPLETED, True, False),
        (DueTaskStatus.OPEN, False, False),
        (DueTaskStatus.OPEN, True, True),
    ],
)
def test_irrelevant_due_task_event_does_nothing(
    status: DueTaskStatus,
    is_due: bool,
    already_handled: bool,
) -> None:
    stella, llm = make_stella_for_event_check()
    event = DueTaskEvent(
        event_id="event-irrelevant",
        task_title="Completed report",
        status=status,
        is_due=is_due,
        already_handled=already_handled,
    )

    result = stella.evaluate_due_task_event(
        event,
        ProactivityDelegation(DelegatedAction.INFORM_DUE_TASK),
    )

    assert result.kind is ProactivityDecisionKind.DO_NOTHING
    assert result.message is None
    assert llm.messages == []


def test_due_event_without_permission_asks_and_takes_no_action() -> None:
    llm = SpyLLM()
    memory = InMemoryMemory()
    memory.store(MemoryItem("The user likes being told when things are overdue."))
    stella = Stella(SimpleBrain(), llm, EchoTool(), memory)
    event = DueTaskEvent(
        event_id="event-no-permission",
        task_title="Tax filing",
        status=DueTaskStatus.OPEN,
        is_due=True,
    )

    result = stella.evaluate_due_task_event(event)

    assert result.kind is ProactivityDecisionKind.ASK
    assert result.message == "Tax filing is due. May I inform you about it?"
    assert llm.messages == []


def test_delegation_permits_only_the_scoped_inform_action() -> None:
    stella, llm = make_stella_for_event_check()
    event = DueTaskEvent(
        event_id="event-scoped",
        task_title="Expense report",
        status=DueTaskStatus.OPEN,
        is_due=True,
    )
    delegation = ProactivityDelegation(
        DelegatedAction.INFORM_DUE_TASK,
        task_scope="Expense report",
    )

    result = stella.evaluate_due_task_event(event, delegation)

    assert result.kind is ProactivityDecisionKind.INFORM
    assert llm.messages == []
    assert not hasattr(result, "tool")


def test_delegation_does_not_expand_to_another_task() -> None:
    stella, llm = make_stella_for_event_check()
    event = DueTaskEvent(
        event_id="event-other-task",
        task_title="Tax filing",
        status=DueTaskStatus.OPEN,
        is_due=True,
    )
    delegation = ProactivityDelegation(
        DelegatedAction.INFORM_DUE_TASK,
        task_scope="Expense report",
    )

    result = stella.evaluate_due_task_event(event, delegation)

    assert result.kind is ProactivityDecisionKind.ASK
    assert result.message == "Tax filing is due. May I inform you about it?"
    assert llm.messages == []


def test_trusted_event_handoff_returns_inspectable_inform_without_action() -> None:
    stella, llm = make_stella_for_event_check()
    event = DueTaskEvent(
        event_id="handoff-1",
        task_title="Expense report",
        status=DueTaskStatus.OPEN,
        is_due=True,
    )

    result = stella.handoff_due_task_event(
        event,
        ProactivityDelegation(DelegatedAction.INFORM_DUE_TASK),
    )

    assert result.kind is ProactivityDecisionKind.INFORM
    assert result.event_id == "handoff-1"
    assert result.message == "Expense report is due today."
    assert result.duplicate_suppressed is False
    assert llm.messages == []
    assert not hasattr(result, "capability")


def test_duplicate_event_id_is_suppressed() -> None:
    stella, llm = make_stella_for_event_check()
    event = DueTaskEvent("same-event", "Expense report", DueTaskStatus.OPEN, True)
    delegation = ProactivityDelegation(DelegatedAction.INFORM_DUE_TASK)

    first = stella.handoff_due_task_event(event, delegation)
    duplicate = stella.handoff_due_task_event(event, delegation)

    assert first.kind is ProactivityDecisionKind.INFORM
    assert duplicate.kind is ProactivityDecisionKind.DO_NOTHING
    assert duplicate.event_id == "same-event"
    assert duplicate.message is None
    assert duplicate.duplicate_suppressed is True
    assert llm.messages == []


def test_distinct_event_ids_are_evaluated_independently() -> None:
    stella, _ = make_stella_for_event_check()
    delegation = ProactivityDelegation(DelegatedAction.INFORM_DUE_TASK)
    first = DueTaskEvent("event-a", "Expense report", DueTaskStatus.OPEN, True)
    second = DueTaskEvent("event-b", "Expense report", DueTaskStatus.OPEN, True)

    assert stella.handoff_due_task_event(first, delegation).kind is (
        ProactivityDecisionKind.INFORM
    )
    assert stella.handoff_due_task_event(second, delegation).kind is (
        ProactivityDecisionKind.INFORM
    )


def test_handoff_scopes_delegation_and_asks_without_permission() -> None:
    stella, llm = make_stella_for_event_check()
    event = DueTaskEvent("scoped-handoff", "Tax filing", DueTaskStatus.OPEN, True)
    delegation = ProactivityDelegation(
        DelegatedAction.INFORM_DUE_TASK,
        task_scope="Expense report",
    )

    result = stella.handoff_due_task_event(event, delegation)

    assert result.kind is ProactivityDecisionKind.ASK
    assert result.message == "Tax filing is due. May I inform you about it?"
    assert llm.messages == []


def test_ordinary_memory_cannot_create_handoff_delegation() -> None:
    llm = SpyLLM()
    memory = InMemoryMemory()
    memory.store(MemoryItem("The user likes being told when things are overdue."))
    stella = Stella(SimpleBrain(), llm, EchoTool(), memory)
    event = DueTaskEvent("memory-not-permission", "Tax filing", DueTaskStatus.OPEN, True)

    result = stella.handoff_due_task_event(event)

    assert result.kind is ProactivityDecisionKind.ASK
    assert llm.messages == []


def test_user_facing_inform_result_exposes_message_without_delivery() -> None:
    stella, llm = make_stella_for_event_check()
    event = DueTaskEvent("present-inform", "Expense report", DueTaskStatus.OPEN, True)

    result = stella.present_due_task_event(
        event,
        ProactivityDelegation(DelegatedAction.INFORM_DUE_TASK),
    )

    assert isinstance(result, UserFacingProactivityResult)
    assert result.kind is ProactivityDecisionKind.INFORM
    assert result.event_id == "present-inform"
    assert result.message == "Expense report is due today."
    assert llm.messages == []


def test_user_facing_ask_result_exposes_permission_question() -> None:
    stella, llm = make_stella_for_event_check()
    event = DueTaskEvent("present-ask", "Tax filing", DueTaskStatus.OPEN, True)

    result = stella.present_due_task_event(event)

    assert result.kind is ProactivityDecisionKind.ASK
    assert result.message == "Tax filing is due. May I inform you about it?"
    assert llm.messages == []


def test_user_facing_do_nothing_has_no_message_or_action() -> None:
    stella, llm = make_stella_for_event_check()
    event = DueTaskEvent(
        "present-noop", "Completed report", DueTaskStatus.COMPLETED, True
    )

    result = stella.present_due_task_event(
        event,
        ProactivityDelegation(DelegatedAction.INFORM_DUE_TASK),
    )

    assert result.kind is ProactivityDecisionKind.DO_NOTHING
    assert result.message is None
    assert result.duplicate_suppressed is False
    assert llm.messages == []


def test_duplicate_user_facing_result_is_silent() -> None:
    stella, llm = make_stella_for_event_check()
    event = DueTaskEvent("present-duplicate", "Expense report", DueTaskStatus.OPEN, True)
    delegation = ProactivityDelegation(DelegatedAction.INFORM_DUE_TASK)

    first = stella.present_due_task_event(event, delegation)
    duplicate = stella.present_due_task_event(event, delegation)

    assert first.message == "Expense report is due today."
    assert duplicate.kind is ProactivityDecisionKind.DO_NOTHING
    assert duplicate.message is None
    assert duplicate.duplicate_suppressed is True
    assert llm.messages == []


def test_handoff_irrelevant_event_is_do_nothing_without_action() -> None:
    stella, llm = make_stella_for_event_check()
    event = DueTaskEvent(
        "irrelevant-handoff",
        "Completed report",
        DueTaskStatus.COMPLETED,
        True,
    )

    result = stella.handoff_due_task_event(
        event,
        ProactivityDelegation(DelegatedAction.INFORM_DUE_TASK),
    )

    assert result.kind is ProactivityDecisionKind.DO_NOTHING
    assert result.message is None
    assert result.duplicate_suppressed is False
    assert llm.messages == []
