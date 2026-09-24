"""Phase 4 application-layer and UI-bridge tests.

These cover the shared ``stella.app`` behaviour the CLI and the Tk window
are built on: honest outcome mapping, the worker-thread bridge, the
approval broker's token identity, and the security boundary that the UI
path cannot forge, bypass, or widen authorization.
"""

import datetime as dt
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Self

import pytest

from stella import app
from stella.app import (
    ApprovalBroker,
    MemoryPanel,
    ReminderPanel,
    ReminderScheduler,
    StellaApplication,
    StellaBridge,
    StellaSession,
    StellaSettings,
    TurnOutcome,
    build_application,
    display_response,
    outcome_status,
)
from stella.brain import Brain, Decision, DecisionKind, LLMBrain
from stella.context import Context
from stella.llm import LLMClient, LLMResponse, Message, run_cancellable
from stella.memory import InMemoryMemory, MemoryItem, SQLiteMemory
from stella.minilm_embedding import MiniLMEmbeddingProvider
from stella.ollama_embedding import OllamaEmbeddingProvider
from stella.reminders import InMemoryReminderStore
from stella.stella import Stella, StellaResult
from stella.tools import (
    ActionPreview,
    ActionReceipt,
    ApprovalRequest,
    EchoTool,
    RiskLevel,
    Tool,
    ToolApproval,
    ToolDispatcher,
    ToolResult,
)
from stella.voice import VoiceError

NOW = dt.datetime(2026, 5, 1, 12, 0, tzinfo=dt.UTC)
REAL_NOW = dt.datetime.now(dt.UTC)


class SpyLLM(LLMClient):
    def chat(
        self,
        messages: list[Message | dict[str, str]],
        should_cancel=None,
    ) -> str:
        del should_cancel
        return "the action completed"


class ScriptedBrain(Brain):
    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = decisions

    def decide(
        self,
        context: Context,
        should_cancel=None,
    ) -> Decision:
        del should_cancel
        return self.decisions.pop(0)


class ExplodingBrain(Brain):
    def decide(
        self,
        context: Context,
        should_cancel=None,
    ) -> Decision:
        del context, should_cancel
        raise AssertionError("this turn must not consult the Brain")


class SleepingBrain(ScriptedBrain):
    """Answers like ScriptedBrain but holds the worker thread busy."""

    def __init__(self, decisions: list[Decision], delay: float) -> None:
        super().__init__(decisions)
        self._delay = delay

    def decide(
        self,
        context: Context,
        should_cancel=None,
    ) -> Decision:
        time.sleep(self._delay)
        return super().decide(context, should_cancel=should_cancel)


class DangerousTool(Tool):
    name = "approval_test"
    description = "Test-only dangerous action."

    def __init__(self) -> None:
        self.executions: list[dict[str, object]] = []

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.DANGEROUS

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return arguments == {"value": "x"}

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        self.executions.append(arguments)
        return ToolResult(success=True, output="executed")


class PreviewingDangerousTool(DangerousTool):
    """A dangerous tool that also offers a display-only preview."""

    def preview(self, request: ApprovalRequest) -> ActionPreview:
        return ActionPreview(detail_lines=("+ preview line",))


def tool_result(
    *,
    success: bool = True,
    output: str = "ok",
    receipt_status: str | None = None,
) -> ToolResult:
    receipt = (
        ActionReceipt("write", receipt_status)
        if receipt_status is not None
        else None
    )
    return ToolResult(
        success=success, output=output, action_receipt=receipt
    )


# ------------------------------------------------------- pure mapping


def test_display_response_prefers_answer_then_tool_then_questions() -> None:
    def make(**kwargs: object) -> StellaResult:
        base: dict[str, object] = {
            "decision": Decision(kind=DecisionKind.ANSWER),
        }
        base.update(kwargs)
        return StellaResult(**base)  # type: ignore[arg-type]

    assert display_response(make(response="hi")) == "hi"
    assert (
        display_response(make(tool_result=ToolResult(True, "tool said")))
        == "tool said"
    )
    assert (
        display_response(make(needs_more_information=True))
        == "I need more information."
    )
    assert display_response(make()) is None


@pytest.mark.parametrize(
    ("result", "kind", "symbol"),
    [
        (None, "none", ""),
        (tool_result(receipt_status="verified"), "verified", "✓"),
        (tool_result(receipt_status="unverified"), "unverified", "✗"),
        (tool_result(receipt_status="inconclusive"), "inconclusive", "?"),
        (tool_result(receipt_status="missing"), "missing", "✗"),
        (tool_result(receipt_status="invalid"), "invalid", "✗"),
        (tool_result(), "succeeded", "✓"),
        (
            tool_result(success=False, output="boom"),
            "failed",
            "✗",
        ),
        (
            tool_result(success=False, output="Approval required."),
            "denied",
            "✗",
        ),
        (
            tool_result(success=False, output="Approval denied."),
            "denied",
            "✗",
        ),
    ],
)
def test_outcome_status_preserves_the_trusted_distinction(
    result: ToolResult | None, kind: str, symbol: str
) -> None:
    status = outcome_status(result)

    assert status.kind == kind
    assert status.symbol == symbol


def test_unverified_mutation_never_renders_as_verified_success() -> None:
    unverified = outcome_status(tool_result(receipt_status="unverified"))
    verified = outcome_status(tool_result(receipt_status="verified"))

    assert unverified.kind != verified.kind
    assert unverified.symbol != verified.symbol
    assert verified.symbol == "✓"


# ------------------------------------------------------ StellaSession


def make_recording_stella(
    decisions: list[Decision] | None = None,
    reminders: InMemoryReminderStore | None = None,
) -> Stella:
    brain = ScriptedBrain(
        decisions
        if decisions is not None
        else [Decision(kind=DecisionKind.ANSWER, content="noted")]
    )
    return Stella(
        brain,
        SpyLLM(),
        ToolDispatcher([EchoTool()]),
        InMemoryMemory(),
        reminders=reminders,
    )


def test_run_turn_answers_and_extends_history() -> None:
    stella = make_recording_stella()
    session = StellaSession(stella)

    outcome = session.run_turn("hello")

    assert isinstance(outcome, TurnOutcome)
    assert outcome.response == "the action completed"
    assert session.history == [
        Message(role="user", content="hello"),
        Message(role="assistant", content="the action completed"),
    ]


def test_run_turn_error_wording_matches_the_cli_and_hides_details() -> None:
    class ExplodingStella:
        def process(self, context: Context) -> StellaResult:
            raise RuntimeError(
                "secret /home/user/path Traceback-worthy internals here"
            )

    session = StellaSession(ExplodingStella())  # type: ignore[arg-type]

    outcome = session.run_turn("do something")

    assert outcome.error_message == (
        "Stella could not finish that request (secret /home/user/path "
        "Traceback-worthy internals here). Nothing was changed; try again "
        "or type 'exit' to quit."
    )
    assert "Traceback (most recent call last)" not in outcome.error_message
    assert session.history == []


def test_run_turn_reports_interruption_without_changes() -> None:
    class InterruptingStella:
        def process(self, context: Context) -> StellaResult:
            raise KeyboardInterrupt

    outcome = StellaSession(InterruptingStella()).run_turn(  # type: ignore[arg-type]
        "stop me"
    )

    assert outcome.interrupted is True
    assert outcome.result is None


def test_session_custom_error_footer_is_used() -> None:
    class ExplodingStella:
        def process(self, context: Context) -> StellaResult:
            raise RuntimeError("nope")

    outcome = StellaSession(
        ExplodingStella(), error_footer="try again."  # type: ignore[arg-type]
    ).run_turn("x")

    assert outcome.error_message is not None
    assert outcome.error_message.endswith("Nothing was changed; try again.")


def test_check_due_reminders_delivers_through_the_trusted_flow() -> None:
    store = InMemoryReminderStore()
    assert store.create("Water the plants", REAL_NOW + dt.timedelta(hours=1), REAL_NOW)
    session = StellaSession(make_recording_stella(reminders=store))

    deliveries = session.check_due_reminders(REAL_NOW + dt.timedelta(hours=2))

    assert [delivery.message for delivery in deliveries] == [
        "Water the plants is due today."
    ]
    assert session.check_due_reminders(REAL_NOW + dt.timedelta(hours=3)) == ()


# ---------------------------------------------------------- MemoryPanel


def make_memory() -> InMemoryMemory:
    memory = InMemoryMemory()
    memory.store(MemoryItem("Prefers oat milk"))
    memory.store(MemoryItem("Lives in Berlin"))
    return memory


def test_memory_panel_exposes_content_strings_not_ids() -> None:
    panel = MemoryPanel(make_memory())

    rows = panel.refresh()

    assert rows == ("Prefers oat milk", "Lives in Berlin")
    assert all(isinstance(row, str) for row in rows)


def test_memory_panel_search_uses_the_backend_query() -> None:
    panel = MemoryPanel(make_memory())

    rows = panel.refresh("oat")

    assert rows == ("Prefers oat milk",)


def test_memory_panel_forgets_by_visible_position() -> None:
    memory = make_memory()
    panel = MemoryPanel(memory)
    panel.refresh()

    message = panel.forget(0)

    assert message == "That memory was forgotten."
    assert panel.refresh() == ("Lives in Berlin",)
    assert [item.content for item in memory.retrieve()] == [
        "Lives in Berlin"
    ]


def test_memory_panel_forget_without_selection_is_safe() -> None:
    panel = MemoryPanel(make_memory())
    panel.refresh()

    assert panel.forget(5) == (
        "No memory is selected. Pick one from the list first."
    )
    assert panel.forget(-1) == (
        "No memory is selected. Pick one from the list first."
    )
    assert len(panel.refresh()) == 2


# --------------------------------------------------------- ReminderPanel


def test_reminder_panel_matches_the_trusted_tool_wording() -> None:
    store = InMemoryReminderStore()
    panel = ReminderPanel(store)
    due = (REAL_NOW + dt.timedelta(hours=1)).isoformat()

    created = panel.create("Call the dentist", due)

    assert created.success
    assert created.output == (
        f"Reminder created (ID 1): Call the dentist at {due}."
    )
    assert panel.pending_rows() == (("Call the dentist", due),)


def test_reminder_panel_rejects_bad_input_honestly() -> None:
    store = InMemoryReminderStore()
    panel = ReminderPanel(store)

    bad_time = panel.create("anything", "sometime tomorrow")
    empty = panel.create("   ", (REAL_NOW + dt.timedelta(hours=1)).isoformat())

    assert not bad_time.success
    assert "ISO-8601" in bad_time.output
    assert not empty.success
    assert panel.pending_rows() == ()
    assert store.pending() == ()


def test_reminder_panel_cancel_requires_one_clear_match() -> None:
    store = InMemoryReminderStore()
    panel = ReminderPanel(store)
    panel.create("Submit the assignment", (REAL_NOW + dt.timedelta(hours=1)).isoformat())
    panel.create("Attend the meeting", (REAL_NOW + dt.timedelta(hours=2)).isoformat())

    ambiguous = panel.cancel("the")
    single = panel.cancel("assignment")

    assert not ambiguous.success
    assert "nothing was cancelled" in ambiguous.output
    assert single.success
    assert len(panel.pending_rows()) == 1


def test_reminder_panel_without_a_store_stays_inert() -> None:
    panel = ReminderPanel(None)

    assert panel.available is False
    assert panel.pending_rows() == ()
    result = panel.create("anything", "2099-01-01T00:00:00+00:00")

    assert not result.success
    assert result.output == (
        "Reminders are not available in this configuration."
    )


# -------------------------------------------------------- ApprovalBroker


def run_broker_request(
    broker: ApprovalBroker, request: ApprovalRequest
) -> tuple[threading.Thread, dict[str, ToolApproval]]:
    outcome: dict[str, ToolApproval] = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault(
            "approval", broker.request(request)
        )
    )
    thread.start()
    return thread, outcome


def wait_for_approval(
    broker: ApprovalBroker,
) -> tuple[int, ApprovalRequest]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        item = broker.next_request()
        if item is not None:
            token, request, _preview = item
            return token, request
        time.sleep(0.01)
    raise AssertionError("no approval request appeared")


def test_broker_approves_only_the_dispatchers_own_request_object() -> None:
    broker = ApprovalBroker()
    request = ApprovalRequest("cap", {"value": "x"})
    thread, outcome = run_broker_request(broker, request)

    token, seen = wait_for_approval(broker)

    assert seen is request
    assert broker.resolve(token, True) is True
    thread.join(5)
    approval = outcome["approval"]
    assert approval.request is request
    assert approval.approved is True


def test_broker_answers_each_request_only_once() -> None:
    broker = ApprovalBroker()
    request = ApprovalRequest("cap", {"value": "x"})
    thread, outcome = run_broker_request(broker, request)

    token, _request = wait_for_approval(broker)
    assert broker.resolve(token, True) is True
    thread.join(5)

    assert broker.resolve(token, False) is False
    assert broker.resolve(999, True) is False
    assert outcome["approval"].approved is True


def test_broker_answer_is_consumed_before_the_requester_wakes() -> None:
    # Security audit F2: the token must be consumed by the answer itself, not
    # by the requester thread finishing. Otherwise a second stale UI answer
    # (or shutdown denial) landing inside the wake-up window could overwrite
    # the decision the dispatcher is about to observe.
    broker = ApprovalBroker()
    thread, outcome = run_broker_request(
        broker, ApprovalRequest("cap", {"value": "x"})
    )

    token, _request = wait_for_approval(broker)
    assert broker.resolve(token, True) is True
    assert broker.resolve(token, False) is False  # no join in between
    broker.deny_outstanding()  # must also not touch the answered request
    thread.join(5)

    assert outcome["approval"].approved is True


def test_broker_denies_everything_left_unanswered() -> None:
    broker = ApprovalBroker()
    thread, outcome = run_broker_request(
        broker, ApprovalRequest("cap", {"value": "x"})
    )

    wait_for_approval(broker)
    broker.deny_outstanding()
    thread.join(5)

    assert outcome["approval"].approved is False


def test_broker_next_request_without_timeout_does_not_block() -> None:
    broker = ApprovalBroker()

    started = time.monotonic()
    assert broker.next_request() is None

    assert time.monotonic() - started < 1


def test_broker_carries_a_preview_without_touching_the_token() -> None:
    broker = ApprovalBroker()
    request = ApprovalRequest("cap", {"value": "x"})
    preview = ActionPreview(detail_lines=("+ new line",), truncated=True)
    outcome: dict[str, ToolApproval] = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault(
            "approval", broker.request(request, preview)
        )
    )
    thread.start()

    item = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        item = broker.next_request()
        if item is not None:
            break
        time.sleep(0.01)
    assert item is not None
    token, seen, seen_preview = item
    assert seen is request
    assert seen_preview is preview
    assert broker.resolve(token, True) is True
    thread.join(5)

    # The preview travels beside the request; what the answer authorizes
    # remains exactly the dispatcher's own ApprovalRequest and nothing else.
    approval = outcome["approval"]
    assert approval == ToolApproval(request=request, approved=True)
    assert not hasattr(approval, "preview")


# ---------------------------------------------------------------- bridge


def make_bridge(
    stella: Stella, settings: StellaSettings | None = None, **bridge_options
) -> StellaBridge:
    application = StellaApplication(
        StellaSession(stella), settings or StellaSettings(model="test")
    )
    return StellaBridge(lambda: application, **bridge_options)


def wait_for_event(bridge: StellaBridge, kind: str) -> list:
    """Collect events until one of ``kind`` arrives; return all payloads."""
    events = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        events.extend(bridge.poll())
        if any(event.kind == kind for event in events):
            return events
        time.sleep(0.02)
    raise AssertionError(f"no {kind!r} event; saw {[e.kind for e in events]}")


def test_bridge_runs_a_turn_on_the_worker_thread() -> None:
    bridge = make_bridge(make_recording_stella())

    bridge.post_turn("hello")
    events = wait_for_event(bridge, "turn")

    outcome: TurnOutcome = next(
        event.payload for event in events if event.kind == "turn"
    )
    assert outcome.response == "the action completed"
    bridge.stop()


def test_bridge_delivers_due_reminders_before_the_turn_event() -> None:
    store = InMemoryReminderStore()
    due = REAL_NOW + dt.timedelta(seconds=1)
    assert store.create("Private errand", due, REAL_NOW)
    bridge = make_bridge(make_recording_stella(reminders=store))

    time.sleep(1.2)
    bridge.post_turn("anything")
    events = wait_for_event(bridge, "turn")

    kinds = [event.kind for event in events]
    assert kinds.index("reminder_delivered") < kinds.index("turn")
    delivered = next(e.payload for e in events if e.kind == "reminder_delivered")
    assert delivered == "Private errand is due today."
    bridge.stop()


# ------------------------------------------------------------ idle reminder ticks


def test_bridge_delivers_due_reminder_while_idle_without_any_turn() -> None:
    # Stage A D1: an open-but-idle window must still inform, unprompted.
    now = dt.datetime.now(dt.UTC)
    store = InMemoryReminderStore()
    assert store.create("Idle errand", now + dt.timedelta(milliseconds=200), now)
    bridge = make_bridge(
        make_recording_stella(reminders=store), reminder_tick_seconds=0.05
    )
    try:
        events = wait_for_event(bridge, "reminder_delivered")
    finally:
        bridge.stop()
    payloads = [
        event.payload
        for event in events
        if event.kind == "reminder_delivered"
    ]
    assert payloads == ["Idle errand is due today."]


def test_bridge_tick_uses_the_injected_clock() -> None:
    # Determinism: the sweep compares against the bridge's clock, so a
    # fixed future "now" fires immediately with no sleeping past due time.
    store = InMemoryReminderStore()
    assert store.create("Future errand", REAL_NOW + dt.timedelta(hours=1), REAL_NOW)
    fixed = REAL_NOW + dt.timedelta(hours=2)
    bridge = make_bridge(
        make_recording_stella(reminders=store),
        reminder_tick_seconds=0.05,
        now=lambda: fixed,
    )
    try:
        events = wait_for_event(bridge, "reminder_delivered")
    finally:
        bridge.stop()
    delivered = next(
        event.payload
        for event in events
        if event.kind == "reminder_delivered"
    )
    assert delivered == "Future errand is due today."
    assert store.pending() == ()


def test_reminder_scheduler_stops_calling_after_stop() -> None:
    calls: list[int] = []
    scheduler = ReminderScheduler(
        lambda: calls.append(1), interval_seconds=0.02
    )
    scheduler.start()
    time.sleep(0.15)
    scheduler.stop()

    assert calls
    settled = len(calls)
    time.sleep(0.1)
    assert len(calls) == settled


def test_reminder_check_queues_behind_a_busy_turn() -> None:
    # A sweep posted while the worker is mid-turn must wait its turn on
    # the single command queue: no parallel Stella, no lost delivery.
    now = dt.datetime.now(dt.UTC)
    store = InMemoryReminderStore()
    assert store.create(
        "First errand", now + dt.timedelta(minutes=1), now
    )
    assert store.create(
        "Second errand", now + dt.timedelta(minutes=5), now
    )
    sweep_count = 0

    def staged_now() -> dt.datetime:
        # The turn's own pre-sweep sees only the first reminder due;
        # every later sweep (the queued one) sees both as due.
        nonlocal sweep_count
        sweep_count += 1
        if sweep_count == 1:
            return now + dt.timedelta(minutes=2)
        return now + dt.timedelta(minutes=6)

    stella = Stella(
        SleepingBrain(
            [Decision(kind=DecisionKind.ANSWER, content="slow reply")], 0.6
        ),
        SpyLLM(),
        ToolDispatcher([EchoTool()]),
        InMemoryMemory(),
        reminders=store,
    )
    bridge = make_bridge(
        stella, reminder_tick_seconds=None, now=staged_now
    )
    try:
        bridge.post_turn("hello")
        time.sleep(0.2)  # worker is now parked inside Brain.decide
        bridge.post_reminder_check()
        events: list = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events.extend(bridge.poll())
            deliveries = [e for e in events if e.kind == "reminder_delivered"]
            if len(deliveries) == 2 and any(e.kind == "turn" for e in events):
                break
            time.sleep(0.02)
    finally:
        bridge.stop()

    deliveries = [
        event.payload
        for event in events
        if event.kind == "reminder_delivered"
    ]
    kinds = [event.kind for event in events]
    assert deliveries[0] == "First errand is due today."
    assert deliveries[1] == "Second errand is due today."
    # The queued sweep could only run after the turn event: everything
    # after "turn" is that second delivery and the panel refresh.
    turn_index = kinds.index("turn")
    assert deliveries[1] in [
        event.payload for event in events[turn_index + 1 :]
    ]
    assert set(kinds[turn_index + 1 :]) <= {
        "reminder_delivered",
        "reminders",
    }
    assert store.pending() == ()


def test_due_reminder_delivers_exactly_once_across_tick_and_turn() -> None:
    # Tick and turn race to claim the same reminder; the store's atomic
    # transition means the user is told exactly once.
    now = dt.datetime.now(dt.UTC)
    store = InMemoryReminderStore()
    assert store.create(
        "Contended errand", now + dt.timedelta(milliseconds=500), now
    )
    bridge = make_bridge(
        make_recording_stella(reminders=store), reminder_tick_seconds=0.03
    )
    try:
        bridge.post_turn("hello")
        events = wait_for_event(bridge, "reminder_delivered")
        time.sleep(0.3)  # let several more ticks pass
        events.extend(bridge.poll())
    finally:
        bridge.stop()
    deliveries = [
        event.payload
        for event in events
        if event.kind == "reminder_delivered"
    ]
    assert deliveries == ["Contended errand is due today."]
    assert any(event.kind == "turn" for event in events)


def test_bridge_reports_startup_failure_without_a_stack_trace() -> None:
    def factory() -> StellaApplication:
        raise RuntimeError("STELLA_MODEL is required")

    bridge = StellaBridge(factory)

    events = wait_for_event(bridge, "error")

    assert "STELLA_MODEL is required" in events[0].payload
    assert "Traceback (most recent call last)" not in events[0].payload

    bridge.post_turn("hello")
    events = wait_for_event(bridge, "error")

    assert "not running" in events[0].payload
    bridge.stop()


def test_bridge_stays_alive_after_a_failing_turn() -> None:
    class FailingStella:
        def __init__(self) -> None:
            self.memory = InMemoryMemory()

        def process(
            self, context: Context, should_cancel=None
        ) -> StellaResult:
            raise RuntimeError("provider exploded")

    bridge = make_bridge(FailingStella())  # type: ignore[arg-type]

    bridge.post_turn("do something")
    events = wait_for_event(bridge, "turn")

    outcome: TurnOutcome = next(
        event.payload for event in events if event.kind == "turn"
    )
    assert outcome.error_message is not None
    assert "provider exploded" in outcome.error_message
    assert "Traceback (most recent call last)" not in outcome.error_message
    bridge.stop()


def bridge_approval_request(
    bridge: StellaBridge,
) -> tuple[int, ApprovalRequest]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        item = bridge.next_approval_request()
        if item is not None:
            token, request, _preview = item
            return token, request
        time.sleep(0.02)
    raise AssertionError("no approval request surfaced through the bridge")


def test_bridge_denied_approval_executes_nothing() -> None:
    tool = DangerousTool()
    stella = Stella(
        ScriptedBrain(
            [
                Decision(
                    DecisionKind.TOOL,
                    capability="approval_test",
                    arguments={"value": "x"},
                ),
                Decision(kind=DecisionKind.ANSWER, content="skipped"),
            ]
        ),
        SpyLLM(),
        ToolDispatcher([tool]),
        InMemoryMemory(),
    )
    bridge = make_bridge(stella)

    bridge.post_turn("run the dangerous test action")
    token, request = bridge_approval_request(bridge)
    bridge.resolve_approval(token, False)
    events = wait_for_event(bridge, "turn")

    outcome: TurnOutcome = next(
        event.payload for event in events if event.kind == "turn"
    )
    assert tool.executions == []
    assert request.arguments == {"value": "x"}
    assert outcome.result is not None
    assert outcome.result.tool_result is not None
    assert outcome_status(outcome.result.tool_result).kind == "denied"
    bridge.stop()


def test_bridge_allowed_approval_runs_the_original_arguments_only() -> None:
    tool = DangerousTool()
    stella = Stella(
        ScriptedBrain(
            [
                Decision(
                    DecisionKind.TOOL,
                    capability="approval_test",
                    arguments={"value": "x"},
                ),
                Decision(kind=DecisionKind.ANSWER, content="done"),
            ]
        ),
        SpyLLM(),
        ToolDispatcher([tool]),
        InMemoryMemory(),
    )
    bridge = make_bridge(stella)

    bridge.post_turn("run the dangerous test action")
    token, _request = bridge_approval_request(bridge)
    # Answering a stale or invented token must not satisfy the real one.
    assert bridge.resolve_approval(token + 500, True) is False
    assert bridge.resolve_approval(token, True) is True
    events = wait_for_event(bridge, "turn")

    assert tool.executions == [{"value": "x"}]
    outcome: TurnOutcome = next(
        event.payload for event in events if event.kind == "turn"
    )
    assert outcome.result is not None
    bridge.stop()


def test_bridge_surfaces_preview_beside_the_approval_request() -> None:
    tool = PreviewingDangerousTool()
    stella = Stella(
        ScriptedBrain(
            [
                Decision(
                    DecisionKind.TOOL,
                    capability="approval_test",
                    arguments={"value": "x"},
                ),
                Decision(kind=DecisionKind.ANSWER, content="done"),
            ]
        ),
        SpyLLM(),
        ToolDispatcher([tool]),
        InMemoryMemory(),
    )
    bridge = make_bridge(stella)

    bridge.post_turn("run the dangerous test action")
    item = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        item = bridge.next_approval_request()
        if item is not None:
            break
        time.sleep(0.02)
    assert item is not None
    token, _request, preview = item
    assert preview is not None
    assert preview.detail_lines == ("+ preview line",)
    bridge.resolve_approval(token, True)
    wait_for_event(bridge, "turn")

    # Display-only: the preview never replaced or altered the approval.
    assert tool.executions == [{"value": "x"}]
    bridge.stop()


def test_bridge_shutdown_denies_an_unanswered_approval() -> None:
    tool = DangerousTool()
    stella = Stella(
        ScriptedBrain(
            [
                Decision(
                    DecisionKind.TOOL,
                    capability="approval_test",
                    arguments={"value": "x"},
                ),
                Decision(kind=DecisionKind.ANSWER, content="after"),
            ]
        ),
        SpyLLM(),
        ToolDispatcher([tool]),
        InMemoryMemory(),
    )
    bridge = make_bridge(stella)

    bridge.post_turn("run the dangerous test action")
    bridge_approval_request(bridge)
    bridge.stop()

    assert tool.executions == []


def test_bridge_reminder_commands_never_execute_dispatcher_tools() -> None:
    tool = DangerousTool()
    store = InMemoryReminderStore()
    stella = Stella(
        ExplodingBrain(),
        SpyLLM(),
        ToolDispatcher([tool]),
        InMemoryMemory(),
        reminders=store,
    )
    bridge = make_bridge(stella)

    bridge.post_reminder_add(
        "File taxes", (REAL_NOW + dt.timedelta(hours=1)).isoformat()
    )
    events = wait_for_event(bridge, "reminders")
    bridge.post_reminders()
    events.extend(wait_for_event(bridge, "reminders"))

    rows = next(
        event.payload
        for event in reversed(events)
        if event.kind == "reminders"
    )
    assert rows == (("File taxes", (REAL_NOW + dt.timedelta(hours=1)).isoformat()),)
    assert tool.executions == []
    bridge.stop()


def test_bridge_settings_failure_keeps_the_previous_session() -> None:
    def failing(settings: StellaSettings) -> StellaApplication:
        raise RuntimeError(f"bad model {settings.model}")

    original = app.build_application
    app.build_application = failing  # type: ignore[assignment]
    try:
        bridge = make_bridge(make_recording_stella())
        bridge.post_apply_settings(StellaSettings(model="does-not-exist"))
        events = wait_for_event(bridge, "error")
        assert "bad model does-not-exist" in events[0].payload

        bridge.post_turn("still works")
        events = wait_for_event(bridge, "turn")
        assert events[-1].payload.response == "the action completed"
        bridge.stop()
    finally:
        app.build_application = original  # type: ignore[assignment]


def test_bridge_settings_success_rebinds_and_persists_configuration(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from stella import config as stella_config

    def replacement(settings: StellaSettings) -> StellaApplication:
        return StellaApplication(
            StellaSession(make_recording_stella()), settings
        )

    original = app.build_application
    app.build_application = replacement  # type: ignore[assignment]
    try:
        bridge = make_bridge(make_recording_stella())
        bridge.post_apply_settings(
            StellaSettings(provider="ollama", model="picked-model")
        )
        events = wait_for_event(bridge, "settings")
        assert "picked-model" in events[-1].payload
        assert "saved for the next launch" in events[-1].payload
        saved = stella_config.load_configuration()
        assert saved is not None
        assert saved["provider"] == "ollama"
        assert saved["model"] == "picked-model"
        text = (tmp_path / "xdg" / "stella" / "config.json").read_text(
            encoding="utf-8"
        )
        assert "api_key" not in text
        bridge.stop()
    finally:
        app.build_application = original  # type: ignore[assignment]


def test_bridge_settings_failure_does_not_overwrite_saved_configuration(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from stella import config as stella_config

    stella_config.save_configuration(
        StellaSettings(provider="ollama", model="good-model")
    )

    def failing(settings: StellaSettings) -> StellaApplication:
        raise RuntimeError(f"bad model {settings.model}")

    original = app.build_application
    app.build_application = failing  # type: ignore[assignment]
    try:
        bridge = make_bridge(make_recording_stella())
        bridge.post_apply_settings(StellaSettings(model="broken-model"))
        wait_for_event(bridge, "error")
        saved = stella_config.load_configuration()
        assert saved is not None
        assert saved["model"] == "good-model"
        bridge.stop()
    finally:
        app.build_application = original  # type: ignore[assignment]


# ------------------------------------------------------ security checks


def test_a_forged_approval_never_satisfies_the_dispatcher() -> None:
    tool = DangerousTool()
    dispatcher = ToolDispatcher([tool])

    forged = dispatcher.execute(
        "approval_test",
        {"value": "x"},
        approval=ToolApproval(
            request=ApprovalRequest("something_else", {}), approved=True
        ),
    )
    no_approval = dispatcher.execute("approval_test", {"value": "x"})

    assert forged.output == "Invalid approval."
    assert not forged.success
    assert no_approval.output == "Approval required."
    assert tool.executions == []


def test_build_application_requires_a_model() -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_application(StellaSettings(provider="openai", model=None))

    assert str(exit_info.value) == "STELLA_MODEL is required"


def test_default_state_paths_follow_xdg_not_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Release blocker regression: desktop launchers start Stella from an
    # arbitrary working directory, so cwd-relative defaults silently lost
    # memory and reminders across restarts.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    arbitrary_cwd = tmp_path / "arbitrary"
    arbitrary_cwd.mkdir()
    monkeypatch.chdir(arbitrary_cwd)

    settings = StellaSettings(model="test")

    data = tmp_path / "xdg" / "stella"
    assert Path(settings.memory_db) == data / "stella_memory.db"
    assert Path(settings.reminders_db) == data / "stella_reminders.db"
    assert Path(settings.workspace) == data / "workspace"


def test_from_environment_keeps_explicit_paths_and_xdg_fallbacks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STELLA_MODEL", "test")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("STELLA_WORKSPACE", str(tmp_path / "my-workspace"))

    settings = StellaSettings.from_environment()

    assert settings.workspace == str(tmp_path / "my-workspace")
    assert settings.memory_db == str(
        tmp_path / "xdg" / "stella" / "stella_memory.db"
    )


def test_transcript_recording_is_opt_in_across_settings_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("STELLA_TRANSCRIPTS", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    # Default everywhere: recording is off unless the user says otherwise.
    assert StellaSettings().transcripts_enabled is False
    saved = StellaSettings.from_saved(provider="ollama", model="m")
    assert saved.transcripts_enabled is False
    assert Path(saved.transcripts_db) == (
        tmp_path / "xdg" / "stella" / "stella_transcript.db"
    )
    # The saved checkbox value is respected...
    assert (
        StellaSettings.from_saved(
            provider="ollama", model="m", transcripts_enabled=True
        ).transcripts_enabled
        is True
    )
    # ...and the environment can force either way.
    monkeypatch.setenv("STELLA_TRANSCRIPTS", "off")
    assert (
        StellaSettings.from_saved(
            provider="ollama", model="m", transcripts_enabled=True
        ).transcripts_enabled
        is False
    )
    monkeypatch.setenv("STELLA_TRANSCRIPTS", "1")
    monkeypatch.setenv("STELLA_MODEL", "m")
    assert StellaSettings.from_environment().transcripts_enabled is True
    monkeypatch.setenv("STELLA_TRANSCRIPT_DB", str(tmp_path / "custom.db"))
    assert (
        StellaSettings.from_environment().transcripts_db
        == str(tmp_path / "custom.db")
    )


def test_semantic_recall_is_opt_in_across_settings_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("STELLA_SEMANTIC_MEMORY", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    # Default everywhere: the duplicated index is off unless requested.
    assert StellaSettings().semantic_memory_enabled is False
    saved = StellaSettings.from_saved(provider="ollama", model="m")
    assert saved.semantic_memory_enabled is False
    assert Path(saved.semantic_db) == (
        tmp_path / "xdg" / "stella" / "stella_semantic_index.db"
    )
    # The saved checkbox value is respected...
    assert (
        StellaSettings.from_saved(
            provider="ollama", model="m", semantic_memory_enabled=True
        ).semantic_memory_enabled
        is True
    )
    # ...and the environment can force either way.
    monkeypatch.setenv("STELLA_SEMANTIC_MEMORY", "off")
    assert (
        StellaSettings.from_saved(
            provider="ollama", model="m", semantic_memory_enabled=True
        ).semantic_memory_enabled
        is False
    )
    monkeypatch.setenv("STELLA_SEMANTIC_MEMORY", "1")
    monkeypatch.setenv("STELLA_MODEL", "m")
    assert StellaSettings.from_environment().semantic_memory_enabled is True
    monkeypatch.setenv(
        "STELLA_SEMANTIC_DB", str(tmp_path / "custom-semantic.db")
    )
    assert (
        StellaSettings.from_environment().semantic_db
        == str(tmp_path / "custom-semantic.db")
    )


def _semantic_settings(
    tmp_path: Path, enabled: bool
) -> StellaSettings:
    return StellaSettings(
        provider="ollama",
        model="test",
        ollama_base_url="http://127.0.0.1:9",
        memory_db=str(tmp_path / "state" / "memory.db"),
        reminders_db=str(tmp_path / "state" / "reminders.db"),
        workspace=str(tmp_path / "workspace"),
        semantic_db=str(tmp_path / "state" / "semantic.db"),
        semantic_memory_enabled=enabled,
        voice_transcription="off",
        voice_speech="off",
    )


def test_build_application_creates_no_semantic_index_when_disabled(
    tmp_path: Path,
) -> None:
    settings = _semantic_settings(tmp_path, False)

    application = build_application(settings)
    try:
        assert application.session.stella.semantic_retriever is None
        assert not Path(settings.semantic_db).exists()
    finally:
        application.close()


def test_build_application_reconciles_the_index_at_startup(
    tmp_path: Path,
) -> None:
    # A memory stored while Stella was down must still be findable: the
    # startup reconcile heals changes the running hooks could not see.
    settings = _semantic_settings(tmp_path, True)
    Path(settings.memory_db).parent.mkdir(parents=True, exist_ok=True)
    seed = SQLiteMemory(settings.memory_db)
    seed.store(MemoryItem(content="The user prefers tea in the morning."))
    seed.close()

    application = build_application(settings)
    try:
        retriever = application.session.stella.semantic_retriever
        assert retriever is not None
        matches = retriever.retrieve("tea in the morning")
        assert [match.item.content for match in matches] == [
            "The user prefers tea in the morning."
        ]
    finally:
        application.close()
    assert Path(settings.semantic_db).is_file()


def test_semantic_provider_choice_follows_the_saved_then_env_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STELLA_SEMANTIC_PROVIDER", raising=False)

    # Default everywhere: the zero-dependency local hash.
    assert StellaSettings().semantic_provider == "local-hash"
    assert (
        StellaSettings.from_saved(provider="ollama", model="m").semantic_provider
        == "local-hash"
    )
    # The saved choice is respected...
    assert (
        StellaSettings.from_saved(
            provider="ollama", model="m", semantic_provider="ollama"
        ).semantic_provider
        == "ollama"
    )
    # ...and the environment overrides it, exactly like the enable toggle.
    monkeypatch.setenv("STELLA_SEMANTIC_PROVIDER", "minilm")
    assert (
        StellaSettings.from_saved(
            provider="ollama", model="m", semantic_provider="ollama"
        ).semantic_provider
        == "minilm"
    )
    monkeypatch.setenv("STELLA_MODEL", "m")
    assert StellaSettings.from_environment().semantic_provider == "minilm"
    monkeypatch.setenv("STELLA_EMBED_MODEL", "custom-embedder")
    assert (
        StellaSettings.from_environment().semantic_embed_model
        == "custom-embedder"
    )
    # An unknown provider name fails loudly instead of silently swapping
    # in another model's vector space.
    monkeypatch.setenv("STELLA_SEMANTIC_PROVIDER", "openai")
    with pytest.raises(SystemExit):
        StellaSettings.from_environment()


def _provider_settings(tmp_path: Path, semantic_provider: str) -> StellaSettings:
    settings = _semantic_settings(tmp_path, True)
    return replace(settings, semantic_provider=semantic_provider)


def test_build_application_uses_the_selected_embedding_provider(
    tmp_path: Path,
) -> None:
    # Construction must not touch the network: the provider only answers
    # when a real query or memory mutation asks it to.
    settings = _provider_settings(tmp_path, "ollama")

    application = build_application(settings)
    try:
        retriever = application.session.stella.semantic_retriever
        assert retriever is not None
        provider = retriever.provider
        assert isinstance(provider, OllamaEmbeddingProvider)
        assert provider.method == "ollama-embedding"
        assert provider.embed_url == "http://127.0.0.1:9/api/embed"
    finally:
        application.close()


def test_build_application_requires_the_extra_for_minilm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _provider_settings(tmp_path, "minilm")

    monkeypatch.setattr(app, "minilm_extra_available", lambda: False)
    with pytest.raises(SystemExit, match="stella\\[embed\\]"):
        build_application(settings)

    monkeypatch.setattr(app, "minilm_extra_available", lambda: True)
    application = build_application(settings)
    try:
        retriever = application.session.stella.semantic_retriever
        assert retriever is not None
        assert isinstance(retriever.provider, MiniLMEmbeddingProvider)
    finally:
        application.close()
    # First-run ergonomics: a local-only user must not have to learn
    # STELLA_LLM_PROVIDER; the cloud is used only when it is configured.
    monkeypatch.setenv("STELLA_MODEL", "qwen3:4b")
    monkeypatch.delenv("STELLA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    settings = StellaSettings.from_environment()

    assert settings.provider == "ollama"


def test_from_environment_defaults_to_openai_when_a_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STELLA_MODEL", "gpt-test")
    monkeypatch.delenv("STELLA_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-secret")

    settings = StellaSettings.from_environment()

    assert settings.provider == "openai"


def test_from_environment_still_honours_an_explicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STELLA_MODEL", "test")
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    settings = StellaSettings.from_environment()

    assert settings.provider == "openai"


def test_build_application_creates_missing_state_directories(
    tmp_path: Path,
) -> None:
    settings = StellaSettings(
        provider="ollama",
        model="test",
        ollama_base_url="http://127.0.0.1:9",
        memory_db=str(tmp_path / "state" / "memory.db"),
        reminders_db=str(tmp_path / "state" / "reminders.db"),
        workspace=str(tmp_path / "workspace"),
        voice_transcription="off",
        voice_speech="off",
    )

    application = build_application(settings)
    try:
        assert Path(settings.memory_db).is_file()
        assert Path(settings.workspace).is_dir()
    finally:
        application.close()


def test_shared_application_error_message_omits_cli_exit_hint(
    tmp_path: Path,
) -> None:
    # UI dogfood regression: the desktop app shares build_application, and
    # its error line used to tell window users to "type 'exit' to quit".
    settings = StellaSettings(
        provider="ollama",
        model="test",
        ollama_base_url="http://127.0.0.1:9",
        memory_db=str(tmp_path / "state" / "memory.db"),
        reminders_db=str(tmp_path / "state" / "reminders.db"),
        workspace=str(tmp_path / "workspace"),
        voice_transcription="off",
        voice_speech="off",
    )

    application = build_application(settings)
    try:
        outcome = application.session.run_turn("hello")
    finally:
        application.close()

    assert outcome.error_message is not None
    assert "exit" not in outcome.error_message
    assert "try again" in outcome.error_message


def test_misconfigured_voice_commands_never_prevent_startup(
    tmp_path: Path,
) -> None:
    # Release blocker regression: an invalid optional voice command raised
    # during startup and left every later turn reporting "Stella is not
    # running in this session", killing chat, memory and reminders too.
    settings = StellaSettings(
        provider="ollama",
        model="test",
        ollama_base_url="http://127.0.0.1:9",
        memory_db=str(tmp_path / "state" / "memory.db"),
        reminders_db=str(tmp_path / "state" / "reminders.db"),
        workspace=str(tmp_path / "workspace"),
        transcription_command="stub-transcribe.sh",
        speech_command="ffmpeg -f lavfi -i sine",
    )

    application = build_application(settings)
    try:
        panel = application.voice
    finally:
        application.close()

    assert panel is not None
    assert panel.input_available is False
    assert panel.output_available is False
    with pytest.raises(VoiceError) as input_error:
        panel.start_listening()
    assert "transcription command must reference {input}" in str(
        input_error.value
    )
    with pytest.raises(VoiceError) as output_error:
        panel.synthesize(StellaResult(decision=Decision(DecisionKind.ANSWER)))
    assert "speech command must reference {text} and {output}" in str(
        output_error.value
    )


def test_bridge_wires_the_broker_into_the_stella_core() -> None:
    stella = make_recording_stella()
    bridge = make_bridge(stella)

    provider = stella.approval_provider
    assert callable(provider)
    assert provider.__func__ is bridge.approvals.request.__func__
    assert provider.__self__ is bridge.approvals
    bridge.stop()


# ------------------------------------------------- provider honesty (dogfood)


class _FakeHTTPResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeHTTPConnection:
    """Stands in for http.client on the native Ollama transport."""

    def __init__(
        self, host, port=None, timeout=None, *, body: bytes = b"{}"
    ) -> None:
        self.body = body
        self.closed = 0

    def request(self, method, path, body=None, headers=None) -> None:
        pass

    def getresponse(self) -> _FakeHTTPResponse:
        return _FakeHTTPResponse(self.body)

    def close(self) -> None:
        self.closed += 1


def make_ollama_app(tmp_path: Path, base_url: str) -> StellaApplication:
    return build_application(
        StellaSettings(
            provider="ollama",
            model="test-model",
            ollama_base_url=base_url,
            memory_db=str(tmp_path / "m.db"),
            reminders_db=str(tmp_path / "r.db"),
            history_db=str(tmp_path / "h.db"),
            workspace=str(tmp_path / "ws"),
        )
    )


def test_production_application_never_registers_the_echo_test_tool(
    tmp_path: Path,
) -> None:
    # "iawd" dogfood: with echo registered, a confused model could answer
    # gibberish by echoing the user's own text with a "succeeded" outcome.
    application = make_ollama_app(tmp_path, "http://127.0.0.1:1/v1")
    try:
        capabilities = {
            str(description["capability"])
            for description in application.session.stella.tools.describe()
        }
        assert "echo" not in capabilities
        assert "datetime" in capabilities  # real SAFE tools remain
    finally:
        application.close()


def test_unreachable_ollama_reports_an_honest_error_not_an_echo_success(
    tmp_path: Path,
) -> None:
    # Nothing listens on port 1, so this is exactly a stopped Ollama.
    application = make_ollama_app(tmp_path, "http://127.0.0.1:1/v1")
    try:
        outcome = application.session.run_turn("iawd")
        assert outcome.response is None
        assert outcome.result is None
        assert outcome.error_message is not None
        assert "could not finish" in outcome.error_message
        assert "succeeded" not in outcome.error_message
    finally:
        application.close()


def test_reachable_provider_reply_answers_through_the_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(
        {
            "message": {
                "content": json.dumps(
                    {"kind": "answer", "content": "provider reply"}
                )
            }
        }
    ).encode("utf-8")

    def factory(host, port=None, timeout=None, **_kwargs):
        return _FakeHTTPConnection(host, port, timeout=timeout, body=body)

    monkeypatch.setattr(
        "stella.ollama_client.http.client.HTTPConnection", factory
    )
    application = make_ollama_app(
        tmp_path, "http://127.0.0.1:11434/v1"
    )
    try:
        outcome = application.session.run_turn("hello")
        assert outcome.error_message is None
        assert outcome.response == "provider reply"
        assert outcome.result is not None
        assert outcome.result.tool_result is None
    finally:
        application.close()


# ------------------------------------------------------ durable history (A3/A4)


def test_settings_read_history_db_from_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STELLA_MODEL", "test-model")
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("STELLA_HISTORY_DB", str(tmp_path / "h.db"))

    settings = StellaSettings.from_environment()

    assert settings.history_db == str(tmp_path / "h.db")


def test_build_application_uses_durable_history(tmp_path) -> None:
    from stella.history import SQLiteActionHistory

    application = make_ollama_app(tmp_path, "http://127.0.0.1:1")

    try:
        history = application.session.stella.tools.history
        assert isinstance(history, SQLiteActionHistory)
        assert (tmp_path / "h.db").exists()
    finally:
        application.close()


def test_action_history_survives_application_rebuild(tmp_path) -> None:
    application = make_ollama_app(tmp_path, "http://127.0.0.1:1")
    try:
        application.session.stella.tools.history.append(
            {
                "capability": "filesystem_write",
                "arguments": {"path": "notes.txt"},
                "risk_level": "dangerous",
                "approval_required": True,
                "approval_granted": True,
                "execution_success": True,
                "timestamp": "2026-09-24T00:00:00+00:00",
                "action_receipt": None,
            }
        )
    finally:
        application.close()

    relaunched = make_ollama_app(tmp_path, "http://127.0.0.1:1")
    try:
        records = relaunched.session.stella.tools.audit_records
        assert len(records) == 1
        assert records[0].capability == "filesystem_write"
        assert records[0].approval_granted is True
    finally:
        relaunched.close()


def test_bridge_emits_history_after_a_dispatched_turn() -> None:
    tool = DangerousTool()
    stella = Stella(
        ScriptedBrain(
            [
                Decision(
                    DecisionKind.TOOL,
                    capability="approval_test",
                    arguments={"value": "x"},
                ),
                Decision(kind=DecisionKind.ANSWER, content="done"),
            ]
        ),
        SpyLLM(),
        ToolDispatcher([tool]),
        InMemoryMemory(),
    )
    bridge = make_bridge(stella)

    bridge.post_turn("run the dangerous test action")
    token, _request = bridge_approval_request(bridge)
    bridge.resolve_approval(token, True)
    # The bridge emits "history" immediately after "turn" for a turn that
    # dispatched, so waiting for the newer kind cannot miss it.
    events = wait_for_event(bridge, "history")

    rows = next(event.payload for event in events if event.kind == "history")
    assert len(rows) == 1
    assert "approval_test" in rows[0]
    assert "done" in rows[0]
    bridge.stop()


def test_bridge_emits_no_history_event_for_a_turn_without_dispatch() -> None:
    bridge = make_bridge(make_recording_stella())

    bridge.post_turn("just talk")
    events = wait_for_event(bridge, "turn")
    time.sleep(0.2)
    events.extend(bridge.poll())

    assert [event.kind for event in events] == ["turn"]
    bridge.stop()


def test_bridge_post_history_answers_refresh_without_dispatches() -> None:
    bridge = make_bridge(make_recording_stella())

    bridge.post_history()
    events = wait_for_event(bridge, "history")

    payload = next(event.payload for event in events if event.kind == "history")
    assert payload == ()
    bridge.stop()


# ------------------------------------------------- cooperative cancellation (A5)


def test_run_turn_discards_cancelled_turn_from_conversation_history() -> None:
    class CancelAwareStella:
        def __init__(self) -> None:
            self.contexts: list[Context] = []

        def process(
            self, context: Context, should_cancel=None
        ) -> StellaResult:
            self.contexts.append(context)
            if should_cancel is not None and should_cancel():
                return StellaResult(
                    Decision(kind=DecisionKind.DO_NOTHING), cancelled=True
                )
            return StellaResult(
                Decision(kind=DecisionKind.ANSWER, content="noted"),
                response="noted",
            )

    stella = CancelAwareStella()
    session = StellaSession(stella)  # type: ignore[arg-type]

    outcome = session.run_turn("do something", should_cancel=lambda: True)

    assert outcome.cancelled is True
    assert outcome.result is not None
    # The cancelled turn leaves no trace: neither its request nor an
    # answer that was never given enters the conversation.
    assert session.history == []

    follow_up = session.run_turn("hello", should_cancel=lambda: False)

    assert follow_up.response == "noted"
    assert session.history == [
        Message(role="user", content="hello"),
        Message(role="assistant", content="noted"),
    ]
    assert stella.contexts[1].conversation_history == []


def test_bridge_cancel_denies_outstanding_approval_and_ends_turn() -> None:
    tool = DangerousTool()
    brain = ScriptedBrain(
        [
            Decision(
                DecisionKind.TOOL,
                capability="approval_test",
                arguments={"value": "x"},
            ),
            Decision(kind=DecisionKind.ANSWER, content="never reached"),
        ]
    )
    bridge = make_bridge(Stella(
        brain,
        SpyLLM(),
        ToolDispatcher([tool]),
        InMemoryMemory(),
    ))
    try:
        bridge.post_turn("run the dangerous test action")
        _token, _request = bridge_approval_request(bridge)

        bridge.cancel_current_turn()
        events = wait_for_event(bridge, "turn")
        outcome: TurnOutcome = next(
            event.payload for event in events if event.kind == "turn"
        )
        # Cancelling an open approval is a denial, never a bypass: the
        # tool did not run and the turn stops at the next checkpoint
        # without consulting the Brain again.
        assert outcome.cancelled is True
        assert outcome.response is None
        assert tool.executions == []
        assert len(brain.decisions) == 1

        # Cancellation is per-turn: the next turn runs normally.
        bridge.post_turn("still there?")
        events = wait_for_event(bridge, "turn")
        follow_up: TurnOutcome = next(
            event.payload for event in events if event.kind == "turn"
        )
        assert follow_up.cancelled is False
        assert follow_up.response == "the action completed"
    finally:
        bridge.stop()


def test_cancel_without_running_turn_has_no_later_effect() -> None:
    bridge = make_bridge(make_recording_stella())
    try:
        bridge.cancel_current_turn()

        bridge.post_turn("hello")
        events = wait_for_event(bridge, "turn")
        outcome: TurnOutcome = next(
            event.payload for event in events if event.kind == "turn"
        )
        assert outcome.response == "the action completed"
        assert outcome.cancelled is False
    finally:
        bridge.stop()


# ------------------------------------- in-flight provider cancellation (A7)


def test_bridge_cancel_interrupts_a_blocked_provider_request() -> None:
    class BlockingLLM(LLMClient):
        """Behaves like a real provider client: stuck until released."""

        def __init__(self) -> None:
            self.release = threading.Event()

        def chat(self, messages, should_cancel=None) -> str:
            raise AssertionError("text fallback must not be used")

        def chat_with_tools(
            self, messages, tools, tool_choice=None, should_cancel=None
        ):
            def request():
                self.release.wait(30)
                return LLMResponse(
                    content='{"kind":"answer","content":"too late"}'
                )

            return run_cancellable(request, should_cancel, poll_seconds=0.05)

    llm = BlockingLLM()
    session = StellaSession(
        Stella(
            LLMBrain(llm),  # type: ignore[arg-type]
            llm,
            ToolDispatcher([EchoTool()]),
            InMemoryMemory(),
        )
    )
    application = StellaApplication(
        session, StellaSettings(model="test")
    )
    bridge = StellaBridge(lambda: application)
    try:
        bridge.post_turn("this will hang")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not bridge.poll():
                time.sleep(0.02)
        bridge.cancel_current_turn()

        events = wait_for_event(bridge, "turn")
        outcome: TurnOutcome = next(
            event.payload for event in events if event.kind == "turn"
        )
        # The wait ended because it was cancelled, not because the
        # provider replied: no turn event arrived before the cancel.
        assert outcome.cancelled is True
        assert outcome.response is None
        assert outcome.error_message is None
        assert session.history == []
    finally:
        llm.release.set()
        bridge.stop()


# ------------------------------------------------------- per-turn duration (A6)


def test_run_turn_reports_elapsed_seconds_for_every_outcome() -> None:
    class SleepingStella:
        def process(
            self, context: Context, should_cancel=None
        ) -> StellaResult:
            time.sleep(0.05)
            return StellaResult(
                Decision(kind=DecisionKind.ANSWER, content="noted"),
                response="noted",
            )

    session = StellaSession(SleepingStella())  # type: ignore[arg-type]
    outcome = session.run_turn("slow turn")

    assert outcome.duration_seconds is not None
    assert outcome.duration_seconds >= 0.05
    # The timing is display metadata; the stored conversation stays clean.
    assert session.history[-1].content == "noted"

    class BoomStella:
        def process(
            self, context: Context, should_cancel=None
        ) -> StellaResult:
            raise RuntimeError("nope")

    failed = StellaSession(  # type: ignore[arg-type]
        BoomStella()
    ).run_turn("boom")
    assert failed.duration_seconds is not None
    assert failed.error_message is not None
