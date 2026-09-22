"""Bounded, authority-aware evaluation of one due-task event."""

from dataclasses import dataclass
from enum import Enum


class ProactivityDecisionKind(str, Enum):
    """The only outcomes of the one-shot proactive event check."""

    INFORM = "inform"
    ASK = "ask"
    DO_NOTHING = "do_nothing"


class DueTaskStatus(str, Enum):
    """Supported statuses for the focused due-task event."""

    OPEN = "open"
    COMPLETED = "completed"


class DelegatedAction(str, Enum):
    """Actions a trusted caller may explicitly delegate for this scenario."""

    INFORM_DUE_TASK = "inform_due_task"


@dataclass(frozen=True)
class DueTaskEvent:
    """One validated observation supplied by a trusted application boundary."""

    event_id: str
    task_title: str
    status: DueTaskStatus
    is_due: bool
    already_handled: bool = False

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id must not be empty")
        if not self.task_title.strip():
            raise ValueError("task_title must not be empty")
        if not isinstance(self.status, DueTaskStatus):
            raise TypeError("status must be a DueTaskStatus")
        if not isinstance(self.is_due, bool):
            raise TypeError("is_due must be a boolean")
        if not isinstance(self.already_handled, bool):
            raise TypeError("already_handled must be a boolean")


@dataclass(frozen=True)
class ProactivityDelegation:
    """Trusted, narrowly scoped permission separate from ordinary memory."""

    action: DelegatedAction
    task_scope: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, DelegatedAction):
            raise TypeError("action must be a DelegatedAction")
        if self.task_scope is not None and not self.task_scope.strip():
            raise ValueError("task_scope must not be empty")

    def allows(self, event: DueTaskEvent) -> bool:
        """Return whether this delegation permits informing for this event."""

        return (
            self.action is DelegatedAction.INFORM_DUE_TASK
            and (
                self.task_scope is None
                or self.task_scope.casefold() == event.task_title.casefold()
            )
        )


@dataclass(frozen=True)
class ProactivityResult:
    """One bounded proactive outcome; it never contains a tool action."""

    kind: ProactivityDecisionKind
    event_id: str
    message: str | None = None
    duplicate_suppressed: bool = False

    def __post_init__(self) -> None:
        if self.kind is ProactivityDecisionKind.DO_NOTHING and self.message:
            raise ValueError("do_nothing cannot include a message")
        if not isinstance(self.duplicate_suppressed, bool):
            raise TypeError("duplicate_suppressed must be a boolean")
        if (
            self.kind is not ProactivityDecisionKind.DO_NOTHING
            and (not self.message or not self.message.strip())
        ):
            raise ValueError("actionable outcomes require a message")


@dataclass(frozen=True)
class UserFacingProactivityResult:
    """A one-shot result a trusted caller may present to the user."""

    kind: ProactivityDecisionKind
    event_id: str
    message: str | None = None
    duplicate_suppressed: bool = False

    @classmethod
    def from_result(cls, result: ProactivityResult) -> "UserFacingProactivityResult":
        """Expose the evaluator result without adding delivery or action."""

        return cls(
            kind=result.kind,
            event_id=result.event_id,
            message=result.message,
            duplicate_suppressed=result.duplicate_suppressed,
        )

    def __post_init__(self) -> None:
        if self.kind is ProactivityDecisionKind.DO_NOTHING and self.message:
            raise ValueError("do_nothing cannot include a message")
        if (
            self.kind is not ProactivityDecisionKind.DO_NOTHING
            and (not self.message or not self.message.strip())
        ):
            raise ValueError("actionable outcomes require a message")


def evaluate_due_task_event(
    event: DueTaskEvent,
    delegation: ProactivityDelegation | None = None,
) -> ProactivityResult:
    """Evaluate one event without consulting memory, an LLM, or any tool."""

    if (
        event.status is not DueTaskStatus.OPEN
        or not event.is_due
        or event.already_handled
    ):
        return ProactivityResult(
            ProactivityDecisionKind.DO_NOTHING,
            event.event_id,
        )

    if delegation is None or not delegation.allows(event):
        return ProactivityResult(
            ProactivityDecisionKind.ASK,
            event.event_id,
            message=(
                f"{event.task_title} is due. May I inform you about it?"
            ),
        )

    return ProactivityResult(
        ProactivityDecisionKind.INFORM,
        event.event_id,
        message=f"{event.task_title} is due today.",
    )
