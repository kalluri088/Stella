"""Phase 4 Tk window tests.

These exercise the real widgets when a display is available and skip
otherwise. The window may only talk to Stella through ``StellaBridge``,
so the tests assert exactly that: widget actions post bridge commands,
approval dialogs answer the dispatcher's live request, and closing a
dialog denies rather than fabricates authorization.
"""

import datetime as dt
import time
import tkinter as tk

import pytest

from stella.app import (
    StellaApplication,
    StellaBridge,
    StellaSession,
    StellaSettings,
)
from stella.brain import Brain, Decision, DecisionKind
from stella.context import Context
from stella.llm import LLMClient
from stella.memory import InMemoryMemory, MemoryItem
from stella.reminders import InMemoryReminderStore
from stella.stella import Stella
from stella.tools import (
    EchoTool,
    RiskLevel,
    Tool,
    ToolDispatcher,
    ToolResult,
)
from stella.ui import StellaWindow


def display_available() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


pytestmark = pytest.mark.skipif(
    not display_available(), reason="no display available for Tk"
)


class AnswerLLM(LLMClient):
    def chat(self, messages) -> str:
        return "window reply"


class AnswerBrain(Brain):
    def decide(self, context: Context) -> Decision:
        return Decision(kind=DecisionKind.ANSWER, content="window reply")


class ToolThenAnswerBrain(Brain):
    def __init__(self) -> None:
        self.first = True

    def decide(self, context: Context) -> Decision:
        if self.first:
            self.first = False
            return Decision(
                DecisionKind.TOOL,
                capability="window_test_dangerous",
                arguments={"value": "x"},
            )
        return Decision(kind=DecisionKind.ANSWER, content="after")


class DangerousTool(Tool):
    name = "window_test_dangerous"
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


def pump(root: tk.Tk, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)


def make_window(
    brain: Brain | None = None, tool: Tool | None = None
) -> tuple[tk.Tk, StellaWindow, StellaBridge, InMemoryMemory]:
    memory = InMemoryMemory()
    store = InMemoryReminderStore()
    tools = ToolDispatcher([tool or EchoTool()])
    stella = Stella(
        brain or AnswerBrain(),
        AnswerLLM(),
        tools,
        memory,
        reminders=store,
    )

    def factory() -> StellaApplication:
        return StellaApplication(
            StellaSession(stella, error_footer="try again."),
            StellaSettings(model="test"),
        )

    bridge = StellaBridge(factory)
    root = tk.Tk()
    window = StellaWindow(root, bridge, StellaSettings(model="test"))
    return root, window, bridge, memory


def test_window_conversation_round_trip() -> None:
    root, window, bridge, _ = make_window()
    try:
        window._input.insert("1.0", "hello window")
        window._send()
        deadline = time.monotonic() + 5
        while window._busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        transcript = window._chat.get("1.0", "end")
        assert "You: hello window" in transcript
        assert "Stella: window reply" in transcript
        assert window._busy is False
    finally:
        bridge.stop()
        root.destroy()


def test_window_reminder_panel_uses_trusted_tools() -> None:
    root, window, bridge, _ = make_window()
    try:
        due = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat()
        window._reminder_content.insert(0, "Water the plants")
        window._reminder_due.insert(0, due)
        window._add_reminder()
        pump(root, 0.6)
        rows = [
            window._reminder_list.get(i)
            for i in range(window._reminder_list.size())
        ]
        assert rows == [f"Water the plants — due {due}"]

        window._reminder_list.selection_set(0)
        window._cancel_reminder()
        pump(root, 0.6)
        rows = [
            window._reminder_list.get(i)
            for i in range(window._reminder_list.size())
        ]
        assert rows == ["(no pending reminders)"]
        assert window._reminder_status.cget("text").startswith("✓")
    finally:
        bridge.stop()
        root.destroy()


def test_window_memory_panel_shows_content_not_ids() -> None:
    root, window, bridge, memory = make_window()
    try:
        memory.store(MemoryItem("Prefers oat milk"))
        window._refresh_memories()
        pump(root, 0.6)
        values = [
            window._memory_list.get(i)
            for i in range(window._memory_list.size())
        ]
        assert values == ["Prefers oat milk"]

        window._memory_list.selection_set(0)
        window._forget_memory()
        pump(root, 0.6)
        assert memory.retrieve() == []
        values = [
            window._memory_list.get(i)
            for i in range(window._memory_list.size())
        ]
        assert values == ["(no memories stored)"]
    finally:
        bridge.stop()
        root.destroy()


def _wait_for_approval_dialog(root, window, bridge):
    bridge.post_turn("do the dangerous thing")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        root.update()
        if window._dialogs:
            return window._dialogs[0]
        time.sleep(0.02)
    raise AssertionError("approval dialog did not appear")


def test_approval_dialog_allow_runs_original_request() -> None:
    tool = DangerousTool()
    root, window, bridge, _ = make_window(
        brain=ToolThenAnswerBrain(), tool=tool
    )
    try:
        dialog = _wait_for_approval_dialog(root, window, bridge)
        dialog.allow_button.invoke()
        pump(root, 0.8)
        assert tool.executions == [{"value": "x"}]
        assert window._dialogs == []
    finally:
        bridge.stop()
        root.destroy()


def test_approval_dialog_close_denies_the_action() -> None:
    tool = DangerousTool()
    root, window, bridge, _ = make_window(
        brain=ToolThenAnswerBrain(), tool=tool
    )
    try:
        dialog = _wait_for_approval_dialog(root, window, bridge)
        dialog.event_generate("<Escape>")
        pump(root, 0.8)
        assert tool.executions == []
        assert window._dialogs == []
    finally:
        bridge.stop()
        root.destroy()
