"""Shared application layer used by both the CLI and the graphical UI.

The UI is an interface to Stella, never a second Stella: every turn,
memory edit, and reminder change here flows through the same trusted core
(``Stella``, ``ToolDispatcher``, ``Memory``, ``ReminderStore``) that the CLI
already uses. This module adds no new authorization path; it only reuses
existing trusted operations, serialises access onto one worker thread, and
maps existing ``ToolResult``/``ActionReceipt`` semantics onto honest
user-facing statuses.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from stella.brain import LLMBrain
from stella.context import Context
from stella.llm import Message
from stella.memory import Memory, MemoryItem, SQLiteMemory
from stella.ollama_client import DEFAULT_OLLAMA_BASE_URL, OllamaLLMClient
from stella.openai_client import OpenAILLMClient
from stella.reminders import ReminderStore, SQLiteReminderStore
from stella.stella import ReminderDelivery, Stella, StellaResult
from stella.tools import (
    ApprovalRequest,
    DateTimeTool,
    EchoTool,
    FileSystemDeleteTool,
    FileSystemEditTool,
    FileSystemReadTool,
    FileSystemWriteTool,
    MemoryForgetTool,
    MemoryListTool,
    MemoryUpdateTool,
    NetworkReadTool,
    ReminderCancelTool,
    ReminderCreateTool,
    ReminderListTool,
    SystemInfoTool,
    ToolApproval,
    ToolDispatcher,
    ToolResult,
    WorkspaceFindTool,
    WorkspaceListTool,
    WorkspaceSearchTool,
)

__all__ = [
    "ApprovalBroker",
    "MemoryPanel",
    "OutcomeStatus",
    "ReminderPanel",
    "StellaApplication",
    "StellaBridge",
    "StellaSession",
    "StellaSettings",
    "TurnOutcome",
    "UiEvent",
    "build_application",
    "display_response",
    "outcome_status",
]


def display_response(result: StellaResult) -> str | None:
    """Return the user-facing response text for one turn, if any."""

    if result.response is not None:
        return result.response
    if result.tool_result is not None:
        return result.tool_result.output
    if result.needs_more_information:
        return "I need more information."
    return None


@dataclass(frozen=True)
class OutcomeStatus:
    """One honest user-facing status derived from trusted result data.

    ``kind`` is exactly one of: ``verified``, ``unverified``,
    ``inconclusive``, ``failed``, ``missing``, ``invalid``, ``denied``,
    ``succeeded`` (a success that needs no state verification), or ``none``.
    Only ``verified`` and ``succeeded`` carry the success symbol; an
    unverified mutation never renders as a verified success.
    """

    kind: str
    symbol: str
    detail: str


def outcome_status(result: ToolResult | None) -> OutcomeStatus:
    """Map a ``ToolResult`` (and its receipt) onto an ``OutcomeStatus``."""

    if result is None:
        return OutcomeStatus("none", "", "Nothing was executed.")
    if not result.success:
        if result.output in {"Approval required.", "Approval denied."}:
            return OutcomeStatus(
                "denied", "✗", "The action was denied. Nothing was changed."
            )
        return OutcomeStatus(
            "failed", "✗", result.output or "The action failed."
        )
    receipt = result.action_receipt
    if receipt is None:
        return OutcomeStatus("succeeded", "✓", result.output)
    if receipt.status == "verified":
        return OutcomeStatus("verified", "✓", result.output)
    if receipt.status == "unverified":
        return OutcomeStatus("unverified", "✗", result.output)
    if receipt.status == "inconclusive":
        return OutcomeStatus("inconclusive", "?", result.output)
    # Remaining documented receipt statuses are honest failures.
    return OutcomeStatus(receipt.status, "✗", result.output)


@dataclass(frozen=True)
class TurnOutcome:
    """The bounded result of one conversation turn."""

    result: StellaResult | None = None
    response: str | None = None
    error_message: str | None = None
    interrupted: bool = False


class StellaSession:
    """One continuous conversation shared by the CLI and the UI.

    It only orchestrates calls that the CLI already made inline: the
    trusted per-interaction due-reminder check and ``Stella.process``.
    """

    def __init__(
        self,
        stella: Stella,
        error_footer: str = "try again or type 'exit' to quit.",
    ) -> None:
        self.stella = stella
        self.history: list[Message] = []
        self._error_footer = error_footer

    def check_due_reminders(
        self, now: dt.datetime | None = None
    ) -> tuple[ReminderDelivery, ...]:
        if not isinstance(self.stella, Stella):
            # Minimal test or embedding stubs may not carry the reminder flow.
            return ()
        if now is None:
            now = dt.datetime.now(dt.UTC)
        return self.stella.check_due_reminders(now)

    def run_turn(self, user_input: str) -> TurnOutcome:
        try:
            result = self.stella.process(
                Context(
                    user_input=user_input,
                    conversation_history=list(self.history),
                )
            )
        except KeyboardInterrupt:
            return TurnOutcome(interrupted=True)
        except Exception as error:  # noqa: BLE001 - keep the session alive
            detail = " ".join(str(error).split()) or type(error).__name__
            return TurnOutcome(
                error_message=(
                    f"Stella could not finish that request "
                    f"({detail[:160]}). Nothing was changed; "
                    f"{self._error_footer}"
                )
            )
        response = display_response(result)
        self.history.append(Message(role="user", content=user_input))
        if response is not None:
            self.history.append(Message(role="assistant", content=response))
        return TurnOutcome(result=result, response=response)


@dataclass(frozen=True)
class StellaSettings:
    """The minimal local configuration a Stella application needs."""

    provider: str = "openai"
    model: str | None = None
    openai_base_url: str | None = None
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    memory_db: str = "stella_memory.db"
    reminders_db: str = "stella_reminders.db"
    workspace: str = "./stella_workspace"

    @classmethod
    def from_environment(cls) -> StellaSettings:
        """Read the same environment variables the CLI has always used."""

        model = os.environ.get("STELLA_MODEL")
        if not model:
            raise SystemExit("STELLA_MODEL is required")
        provider = os.environ.get("STELLA_LLM_PROVIDER", "openai").casefold()
        if provider not in {"openai", "ollama"}:
            raise SystemExit(
                "STELLA_LLM_PROVIDER must be 'openai' or 'ollama'"
            )
        return cls(
            provider=provider,
            model=model,
            openai_base_url=os.environ.get("OPENAI_BASE_URL"),
            ollama_base_url=os.environ.get(
                "OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL
            ),
            memory_db=os.environ.get("STELLA_MEMORY_DB", "stella_memory.db"),
            reminders_db=os.environ.get(
                "STELLA_REMINDERS_DB", "stella_reminders.db"
            ),
            workspace=os.environ.get(
                "STELLA_WORKSPACE", "./stella_workspace"
            ),
        )


@dataclass
class StellaApplication:
    """A built Stella core plus the session and settings that own it."""

    session: StellaSession
    settings: StellaSettings

    def close(self) -> None:
        memory = self.session.stella.memory
        if isinstance(memory, SQLiteMemory):
            memory.close()
        reminders = self.session.stella.reminders
        if isinstance(reminders, SQLiteReminderStore):
            reminders.close()


def build_application(settings: StellaSettings) -> StellaApplication:
    """Construct the trusted Stella core exactly like the CLI does."""

    if not settings.model:
        raise SystemExit("STELLA_MODEL is required")
    if settings.provider == "ollama":
        # The compatibility endpoint ignores per-request options on Ollama
        # 0.33.x; native /api/chat is the only way to apply num_ctx=4096,
        # which keeps the model fully on GPU (measured ~3x faster turns).
        llm = OllamaLLMClient(
            model=settings.model,
            base_url=settings.ollama_base_url,
            native=True,
            num_ctx=4096,
        )
    elif settings.provider == "openai":
        llm = OpenAILLMClient(
            model=settings.model,
            base_url=settings.openai_base_url,
        )
    else:
        raise SystemExit("STELLA_LLM_PROVIDER must be 'openai' or 'ollama'")
    memory = SQLiteMemory(settings.memory_db)
    reminders = SQLiteReminderStore(settings.reminders_db)
    workspace = settings.workspace
    tools = ToolDispatcher(
        [
            DateTimeTool(),
            SystemInfoTool(),
            EchoTool(),
            FileSystemReadTool(workspace),
            FileSystemWriteTool(workspace),
            FileSystemEditTool(workspace),
            FileSystemDeleteTool(workspace),
            WorkspaceListTool(workspace),
            WorkspaceFindTool(workspace),
            WorkspaceSearchTool(workspace),
            NetworkReadTool(),
            MemoryListTool(memory),
            MemoryUpdateTool(memory),
            MemoryForgetTool(memory),
            ReminderCreateTool(reminders),
            ReminderListTool(reminders),
            ReminderCancelTool(reminders),
        ]
    )
    stella = Stella(
        brain=LLMBrain(llm, tools),
        llm=llm,
        tool=tools,
        memory=memory,
        max_tool_steps=2,
        reminders=reminders,
    )
    return StellaApplication(StellaSession(stella), settings)


class MemoryPanel:
    """Trusted application-layer view over the existing memory backend.

    Rows keep their internal ids only inside this object; the UI sees
    content strings and positional selections, never database ids.
    """

    def __init__(self, memory: Memory) -> None:
        self._memory = memory
        self._rows: list[MemoryItem] = []

    def refresh(self, query: str | None = None) -> tuple[str, ...]:
        search = query.strip() if query and query.strip() else None
        self._rows = list(self._memory.retrieve(search))
        return tuple(item.content for item in self._rows)

    def forget(self, index: int) -> str:
        if not isinstance(index, int) or not 0 <= index < len(self._rows):
            return "No memory is selected. Pick one from the list first."
        item = self._rows[index]
        if self._memory.delete(item.id):
            self._rows.pop(index)
            return "That memory was forgotten."
        return (
            "That memory could not be forgotten; it may have changed. "
            "Refresh the list and try again."
        )


class ReminderPanel:
    """Trusted application-layer view over the existing reminder store.

    Every mutation runs through the same ``Reminder*Tool`` implementations
    (and therefore the same validation and honest output wording) that the
    approved tool path uses. A reminder here remains a notification event:
    nothing on this panel can execute other tools.
    """

    def __init__(self, store: ReminderStore | None) -> None:
        self._store = store

    @property
    def available(self) -> bool:
        return self._store is not None

    def pending_rows(self) -> tuple[tuple[str, str], ...]:
        """Pending reminders as (content, due-time) display pairs."""

        if self._store is None:
            return ()
        return tuple(
            (reminder.content, reminder.due_at.isoformat())
            for reminder in self._store.pending()
        )

    def create(self, content: str, due_at_iso: str) -> ToolResult:
        if self._store is None:
            return self._unavailable()
        return ReminderCreateTool(self._store).execute(
            {"content": content, "due_at": due_at_iso}
        )

    def cancel(self, query: str) -> ToolResult:
        if self._store is None:
            return self._unavailable()
        return ReminderCancelTool(self._store).execute({"query": query})

    def list(self) -> ToolResult:
        if self._store is None:
            return self._unavailable()
        return ReminderListTool(self._store).execute({})

    @staticmethod
    def _unavailable() -> ToolResult:
        return ToolResult(
            success=False,
            output="Reminders are not available in this configuration.",
        )


class ApprovalBroker:
    """Bridge between the dispatcher's approval calls and a UI approver.

    The dispatcher invokes ``request`` with its own exact
    ``ApprovalRequest``; the broker blocks until the UI answers *that*
    request and then produces the ``ToolApproval`` itself, always attached
    to the dispatcher's original request object. A UI answer can therefore
    never approve a different capability or a different argument set, and
    unmade requests default to denial.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens = itertools.count()
        self._waiting: dict[int, _PendingApproval] = {}
        self._outstanding = queue.Queue()

    def request(self, request: ApprovalRequest) -> ToolApproval:
        token = next(self._tokens)
        pending = _PendingApproval(request)
        with self._lock:
            self._waiting[token] = pending
        self._outstanding.put((token, request))
        pending.answered.wait()
        with self._lock:
            self._waiting.pop(token, None)
        return ToolApproval(request=request, approved=pending.approved)

    def next_request(
        self, timeout: float | None = None
    ) -> tuple[int, ApprovalRequest] | None:
        """Pop one outstanding request; None timeout means non-blocking."""

        try:
            if timeout is None:
                return self._outstanding.get_nowait()
            return self._outstanding.get(timeout=timeout)
        except queue.Empty:
            return None

    def resolve(self, token: int, approved: bool) -> bool:
        """Answer one still-outstanding request; unknown tokens do nothing."""

        with self._lock:
            pending = self._waiting.get(token)
            if pending is None:
                return False
            pending.approved = approved
            pending.answered.set()
            return True

    def deny_outstanding(self) -> None:
        """Deny every unanswered request (used when the application exits)."""

        with self._lock:
            for pending in self._waiting.values():
                pending.approved = False
                pending.answered.set()
            self._waiting.clear()


@dataclass
class _PendingApproval:
    request: ApprovalRequest
    answered: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


@dataclass(frozen=True)
class UiEvent:
    """One asynchronous application-layer event destined for the UI."""

    kind: str
    payload: object = None


class StellaBridge:
    """Serialise all UI requests onto one worker thread that owns Stella.

    SQLite connections are thread-bound, and Stella's own state is not
    thread-safe, so exactly one worker thread touches the application
    layer. The UI thread only posts commands and drains events; it never
    calls into ``Stella``, the dispatcher, memory, or the reminder store
    itself. Every command is wrapped so an unexpected failure becomes one
    friendly ``("error", ...)`` event instead of a stack trace.
    """

    def __init__(self, factory: Callable[[], StellaApplication]) -> None:
        self.approvals = ApprovalBroker()
        self._application: StellaApplication | None = None
        self._memory: MemoryPanel | None = None
        self._reminders: ReminderPanel | None = None
        self._commands: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._events: queue.Queue[UiEvent] = queue.Queue()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name="stella-app", daemon=True
        )
        self._thread.start()
        # The application must be *built* on the worker thread because the
        # SQLite connections it owns are bound to their creating thread.
        self._post(lambda: self._create(factory))
        self._ready.wait()

    def _create(self, factory: Callable[[], StellaApplication]) -> None:
        try:
            application = factory()
        except BaseException as error:  # noqa: BLE001 - report, never crash
            # SystemExit included: a missing model must not kill the thread.
            detail = " ".join(str(error).split()) or type(error).__name__
            self._emit(
                "error",
                f"Stella could not start ({detail[:160]}). "
                "Nothing was changed.",
            )
            self._ready.set()
            return
        self._application = application
        self._rebind(application)
        self._ready.set()

    def _rebind(self, application: StellaApplication) -> None:
        stella = application.session.stella
        if isinstance(stella, Stella):
            stella.approval_provider = self.approvals.request
        self._memory = MemoryPanel(stella.memory)
        self._reminders = ReminderPanel(
            getattr(stella, "reminders", None)
        )

    def _serve(self) -> None:
        while True:
            command = self._commands.get()
            if command is None:
                return
            try:
                command()
            except BaseException as error:  # noqa: BLE001 - report, never crash
                detail = " ".join(str(error).split()) or type(error).__name__
                self._events.put(
                    UiEvent(
                        "error",
                        f"Stella could not complete that request "
                        f"({detail[:160]}). Nothing was changed.",
                    )
                )

    def _post(self, command: Callable[[], None]) -> None:
        self._commands.put(command)

    def _emit(self, kind: str, payload: object = None) -> None:
        self._events.put(UiEvent(kind, payload))

    def poll(self) -> list[UiEvent]:
        """Drain every queued event without blocking the UI thread."""

        events: list[UiEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def next_approval_request(
        self, timeout: float | None = None
    ) -> tuple[int, ApprovalRequest] | None:
        return self.approvals.next_request(timeout)

    def resolve_approval(self, token: int, approved: bool) -> bool:
        return self.approvals.resolve(token, approved)

    def _require_session(self) -> StellaSession:
        if self._application is None:
            raise RuntimeError("Stella is not running in this session.")
        return self._application.session

    def _require_memory(self) -> MemoryPanel:
        if self._memory is None:
            raise RuntimeError("Stella is not running in this session.")
        return self._memory

    def _require_reminders(self) -> ReminderPanel:
        if self._reminders is None:
            raise RuntimeError("Stella is not running in this session.")
        return self._reminders

    def post_turn(self, user_input: str) -> None:
        def handle() -> None:
            session = self._require_session()
            for delivery in session.check_due_reminders():
                if delivery.delivered and delivery.message is not None:
                    self._emit("reminder_delivered", delivery.message)
            outcome = session.run_turn(user_input)
            self._emit("turn", outcome)

        self._post(handle)

    def post_memories(self, query: str | None = None) -> None:
        def handle() -> None:
            self._emit("memories", self._require_memory().refresh(query))

        self._post(handle)

    def post_forget(self, index: int) -> None:
        def handle() -> None:
            memory = self._require_memory()
            message = memory.forget(index)
            self._emit("memory_result", message)
            self._emit("memories", memory.refresh())

        self._post(handle)

    def post_reminders(self) -> None:
        def handle() -> None:
            self._emit("reminders", self._require_reminders().pending_rows())

        self._post(handle)

    def post_reminder_add(self, content: str, due_at_iso: str) -> None:
        def handle() -> None:
            panel = self._require_reminders()
            result = panel.create(content, due_at_iso)
            self._emit("reminder_result", outcome_status(result))
            self._emit("reminders", panel.pending_rows())

        self._post(handle)

    def post_reminder_cancel(self, query: str) -> None:
        def handle() -> None:
            panel = self._require_reminders()
            result = panel.cancel(query)
            self._emit("reminder_result", outcome_status(result))
            self._emit("reminders", panel.pending_rows())

        self._post(handle)

    def post_apply_settings(self, settings: StellaSettings) -> None:
        def handle() -> None:
            # Build first so a bad configuration cannot destroy the
            # working session; only then retire the old application.
            application = build_application(settings)
            old = self._application
            self._application = application
            self._rebind(application)
            if old is not None:
                old.close()
            self._emit(
                "settings",
                (
                    f"Stella now uses {settings.provider}/"
                    f"{settings.model} with workspace {settings.workspace}."
                ),
            )

        self._post(handle)

    def stop(self) -> None:
        self.approvals.deny_outstanding()
        self._post(None)
        self._thread.join(timeout=5)
        if self._application is not None:
            try:
                self._application.close()
            except Exception as error:  # noqa: BLE001 - never raise in shutdown
                del error
