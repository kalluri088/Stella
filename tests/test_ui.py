"""Phase 4 Tk window tests.

These exercise the real widgets when a display is available and skip
otherwise. The window may only talk to Stella through ``StellaBridge``,
so the tests assert exactly that: widget actions post bridge commands,
approval dialogs answer the dispatcher's live request, and closing a
dialog denies rather than fabricates authorization.
"""

import datetime as dt
import os
import re
import threading
import time
import tkinter as tk

import pytest

from stella import config as stella_config
from stella import provider_keys
from stella import ui as stella_ui
from stella.app import (
    StellaApplication,
    StellaBridge,
    StellaSession,
    StellaSettings,
    TurnOutcome,
    UiEvent,
    VoicePanel,
)
from stella.audio import TranscriptionProvider
from stella.brain import Brain, Decision, DecisionKind
from stella.context import Context
from stella.llm import LLMClient
from stella.memory import InMemoryMemory, MemoryItem
from stella.reminders import InMemoryReminderStore
from stella.stella import Stella
from stella.tools import (
    ActionPreview,
    EchoTool,
    RiskLevel,
    Tool,
    ToolDispatcher,
    ToolResult,
)
from stella.ui import SetupDialog, StellaWindow
from stella.voice import Recorder


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
    def chat(self, messages, should_cancel=None) -> str:
        del should_cancel
        return "window reply"


class AnswerBrain(Brain):
    def decide(self, context: Context, should_cancel=None) -> Decision:
        del should_cancel
        return Decision(kind=DecisionKind.ANSWER, content="window reply")


class ToolThenAnswerBrain(Brain):
    def __init__(self) -> None:
        self.first = True

    def decide(self, context: Context, should_cancel=None) -> Decision:
        del should_cancel
        if self.first:
            self.first = False
            return Decision(
                DecisionKind.TOOL,
                capability="window_test_dangerous",
                arguments={"value": "x"},
            )
        return Decision(kind=DecisionKind.ANSWER, content="after")


class GatedBrain(Brain):
    """Blocks inside ``decide`` until the test opens the gate.

    Stands in for a slow provider turn: the UI stays busy while the
    worker thread is genuinely inside the Brain.
    """

    def __init__(self) -> None:
        self.gate = threading.Event()

    def decide(self, context: Context, should_cancel=None) -> Decision:
        del should_cancel
        self.gate.wait(5)
        return Decision(kind=DecisionKind.ANSWER, content="late reply")


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


class PreviewingDangerousTool(DangerousTool):
    """A dangerous tool that also offers a display-only preview."""

    def preview(self, request) -> ActionPreview:
        return ActionPreview(
            detail_lines=("- old line", "+ new line"), truncated=True
        )


def pump(root: tk.Tk, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)


def make_window(
    brain: Brain | None = None,
    tool: Tool | None = None,
    voice: VoicePanel | None = None,
    settings: StellaSettings | None = None,
    reminders: InMemoryReminderStore | None = None,
    reminder_tick_seconds: float | None = 5.0,
) -> tuple[tk.Tk, StellaWindow, StellaBridge, InMemoryMemory]:
    memory = InMemoryMemory()
    store = reminders if reminders is not None else InMemoryReminderStore()
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
            settings or StellaSettings(model="test"),
            voice,
        )

    bridge = StellaBridge(
        factory, reminder_tick_seconds=reminder_tick_seconds
    )
    root = tk.Tk()
    window = StellaWindow(
        root, bridge, settings or StellaSettings(model="test")
    )
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
        assert "> hello window" in transcript
        assert "window reply" in transcript
        assert window._busy is False
    finally:
        bridge.stop()
        root.destroy()


def test_input_recall_walks_sent_messages_like_a_terminal() -> None:
    root, window, bridge, _ = make_window()

    def send(text: str) -> None:
        window._input.insert("1.0", text)
        window._send()
        deadline = time.monotonic() + 5
        while window._busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)

    def typed() -> str:
        return window._input.get("1.0", "end").rstrip("\n")

    try:
        send("first message")
        send("second message")
        assert typed() == ""
        # Up walks newest-first and clamps at the oldest entry.
        assert window._recall(-1) == "break"
        assert typed() == "second message"
        window._recall(-1)
        assert typed() == "first message"
        window._recall(-1)
        assert typed() == "first message"
        # Down walks back; past the newest entry the recall started
        # from, the untouched draft returns.
        window._recall(1)
        assert typed() == "second message"
        assert window._recall(1) == "break"
        assert typed() == ""
        # A draft is stashed on the first Up, not lost to it.
        window._input.insert("1.0", "half-typed")
        window._recall(-1)
        assert typed() == "second message"
        window._recall(1)
        assert typed() == "half-typed"
        # Multi-line editing keeps its cursor keys: Up on a lower line
        # and Down with nothing being browsed both fall through.
        window._input.delete("1.0", "end")
        window._input.insert("1.0", "one\ntwo")
        window._input.mark_set("insert", "2.0")
        assert window._recall(-1) == ""
        window._input.mark_set("insert", "end-1c")
        assert window._recall(1) == ""
        # Repeats are stored once, like every shell.
        send("again")
        send("again")
        assert window._sent_history.count("again") == 1
    finally:
        bridge.stop()
        root.destroy()


def test_transcript_separates_roles_in_the_widget_tree() -> None:
    # The palette file guards the colors; this guards that the window
    # actually paints them: the user's message renders as a "> "
    # blockquote on the quote band, and Stella's reply as plain text
    # with no label, no band and no background of its own. Each line's
    # newline carries its own line tag: Tk only stretches a tagged
    # line's background to the full display width when the newline has
    # the tag, so an untagged newline would shrink a "Hi" band to four
    # pixels.
    root, window, bridge, _ = make_window()
    try:
        chat = window._chat
        before = int(chat.index("end-1c").split(".")[0])
        window._line("You: first line\nsecond line", role="user")
        window._line("Stella: plain reply", role="stella")
        for line in (before, before + 1):
            # X.end is the line's newline character itself.
            assert chat.tag_names(f"{line}.end") == ("quote",)
        assert chat.tag_names(f"{before + 3}.end") == ("stella",)
        # The blank spacer lines belong to the gap, never to the band.
        assert chat.tag_names(f"{before + 2}.end") == ("gap",)
        assert chat.tag_names(f"{before + 4}.end") == ("gap",)
        assert chat.get(f"{before}.0", f"{before}.2") == "> "
        assert "quote" in chat.tag_names(f"{before}.0")
        assert "quote" in chat.tag_names(f"{before + 1}.2")
        stella_line = before + 3
        # No "Stella:" label survives into the painted reply, and the
        # reply carries no band: body-stella sets no background.
        assert chat.get(f"{stella_line}.0", f"{stella_line}.6") == "plain "
        assert "Stella:" not in chat.get(f"{stella_line}.0", "end")
        assert "stella" in chat.tag_names(f"{stella_line}.0")
        assert "quote" not in chat.tag_names(f"{stella_line}.0")
        assert str(chat.tag_cget("body-stella", "background")) in ("", "none")
    finally:
        bridge.stop()
        root.destroy()


def test_window_informs_about_due_reminder_while_idle() -> None:
    # Stage A D1: with no user input at all, the bridge tick must place
    # the due reminder into the transcript through the existing pump.
    now = dt.datetime.now(dt.UTC)
    store = InMemoryReminderStore()
    assert store.create(
        "Idle ping", now + dt.timedelta(milliseconds=200), now
    )
    root, window, bridge, _ = make_window(
        reminders=store, reminder_tick_seconds=0.05
    )
    try:
        pump(root, 2.0)
        transcript = window._chat.get("1.0", "end")
        assert "Reminder: Idle ping is due today." in transcript
    finally:
        bridge.stop()
        root.destroy()


def test_settings_apply_preserves_voice_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Release dogfood fix: Apply used to rebuild StellaSettings from only the
    # fields the panel shows, silently resetting voice configuration that the
    # user supplied through the environment.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-voice-test")
    settings = StellaSettings(
        model="test",
        voice_transcription="off",
        voice_speech="off",
        speech_command="my-tts {text} {output}",
    )
    root, window, bridge, _ = make_window(settings=settings)
    try:
        captured: list[StellaSettings] = []
        monkeypatch.setattr(bridge, "post_apply_settings", captured.append)
        model_field = window._settings_fields["Model"]
        model_field.delete("0", "end")
        model_field.insert("0", "new-model")

        window._apply_settings()

        assert len(captured) == 1
        applied = captured[0]
        assert applied.model == "new-model"
        assert applied.voice_transcription == "off"
        assert applied.voice_speech == "off"
        assert applied.speech_command == "my-tts {text} {output}"
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
        # Poll for the denial to land instead of trusting a fixed sleep:
        # under full-suite load the worker round-trip can exceed it.
        dialog.event_generate("<Escape>")
        deadline = time.monotonic() + 2
        while window._dialogs and time.monotonic() < deadline:
            pump(root, 0.05)
        if window._dialogs:
            # XWayland silently drops synthetic key events while the
            # window manager has not given the dialog an X peer, so a
            # lost Escape is an environment fact, not a product bug.
            # Fall through to the Cancel button — the same answer(False)
            # path the close protocol invokes — so the invariant is
            # exercised either way.
            dialog.cancel_button.invoke()
            deadline = time.monotonic() + 3
            while window._dialogs and time.monotonic() < deadline:
                pump(root, 0.05)
        assert tool.executions == []
        assert window._dialogs == []
    finally:
        bridge.stop()
        root.destroy()


def test_approval_dialog_shows_read_only_preview_then_answers_request() -> None:
    tool = PreviewingDangerousTool()
    root, window, bridge, _ = make_window(
        brain=ToolThenAnswerBrain(), tool=tool
    )
    try:
        dialog = _wait_for_approval_dialog(root, window, bridge)
        box = dialog.preview_box
        content = box.get("1.0", "end")
        assert "- old line" in content
        assert "+ new line" in content
        assert "[preview truncated]" in content
        # Review material is display-only: the user could not have typed
        # into it, and the answer still binds solely to the request.
        assert box.cget("state") == "disabled"
        dialog.allow_button.invoke()
        pump(root, 0.8)
        assert tool.executions == [{"value": "x"}]
        assert window._dialogs == []
    finally:
        bridge.stop()
        root.destroy()


# ------------------------------------------------------- working feedback + cancel


def test_activity_event_names_the_capability_on_the_status_line() -> None:
    # Report 35 target 3: the long tool+synthesis stretch no longer reads
    # as a dead pane — a calling:<capability> event upgrades the status
    # line to "Stella is calling reminder list · …" and a fresh turn
    # reverts to the generic wording.
    root, window, bridge, _ = make_window()
    try:
        window._begin_turn_timer()
        window._busy = True
        window._render_working_status()
        assert str(window._status.cget("text")).startswith(
            "Stella is working · "
        )

        window._handle_event(UiEvent("activity", "calling:reminder_list"))
        window._render_working_status()
        assert str(window._status.cget("text")).startswith(
            "Stella is calling reminder list · "
        )

        # A new turn clears the label; narration never sticks.
        window._begin_turn_timer()
        window._render_working_status()
        assert str(window._status.cget("text")).startswith(
            "Stella is working · "
        )
    finally:
        bridge.stop()
        root.destroy()


def test_activity_event_carries_no_authority_and_ignores_blank_capability() -> None:
    root, window, bridge, _ = make_window()
    try:
        window._begin_turn_timer()
        window._busy = True
        # a calling: event with an empty capability must not blank the
        # status into "Stella is calling " — it falls back to working
        window._handle_event(UiEvent("activity", "calling:"))
        window._render_working_status()
        assert str(window._status.cget("text")).startswith(
            "Stella is working · "
        )
    finally:
        bridge.stop()
        root.destroy()


def test_window_shows_elapsed_time_and_cancel_ends_turn_cleanly() -> None:
    brain = GatedBrain()
    root, window, bridge, _ = make_window(brain=brain)
    try:
        assert str(window._cancel_button["state"]) == "disabled"
        window._input.insert("1.0", "take your time")
        window._send()
        pump(root, 0.3)
        # A turn that is merely slow must not read as broken: the status
        # line shows elapsed seconds while the worker is inside the Brain.
        assert str(window._status.cget("text")).startswith(
            "Stella is working · "
        )
        assert str(window._cancel_button["state"]) == "normal"

        window._cancel_turn()
        assert window._status.cget("text") == "Stella is stopping..."
        assert str(window._cancel_button["state"]) == "disabled"
        # The provider request already in flight is not interrupted; it
        # lands, and the turn stops at the next safe point afterwards.
        brain.gate.set()
        deadline = time.monotonic() + 5
        while window._busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        pump(root, 0.2)
        transcript = window._chat.get("1.0", "end")
        assert "stopped that request at your cancel" in transcript
        assert "window reply" not in transcript
        assert window._status.cget("text") == ""
        assert str(window._cancel_button["state"]) == "disabled"
    finally:
        bridge.stop()
        root.destroy()


def test_window_cancel_during_approval_denies_and_executes_nothing() -> None:
    tool = DangerousTool()
    root, window, bridge, _ = make_window(
        brain=ToolThenAnswerBrain(), tool=tool
    )
    try:
        window._input.insert("1.0", "do the dangerous thing")
        window._send()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not window._dialogs:
            root.update()
            time.sleep(0.02)
        assert window._dialogs, "approval dialog did not appear"

        window._cancel_turn()
        # The open approval was denied fail-closed and its dialog
        # dismissed; nothing executed, and the turn ends as cancelled.
        assert window._dialogs == []
        deadline = time.monotonic() + 5
        while window._busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        pump(root, 0.2)
        assert tool.executions == []
        transcript = window._chat.get("1.0", "end")
        assert "stopped that request at your cancel" in transcript
    finally:
        bridge.stop()
        root.destroy()


def test_duration_tail_formats_every_finished_turn() -> None:
    # The clock time is wall-clock, so the tests match its shape, not
    # its value: "(took 1.2 s · 14:32)".
    assert StellaWindow._duration_tail(TurnOutcome()) == ""
    assert re.fullmatch(
        r" \(took 1\.2 s · \d\d:\d\d\)",
        StellaWindow._duration_tail(TurnOutcome(duration_seconds=1.24)),
    )
    assert re.fullmatch(
        r" \(took 47 s · \d\d:\d\d\)",
        StellaWindow._duration_tail(TurnOutcome(duration_seconds=47.4)),
    )


def test_window_transcript_records_turn_duration() -> None:
    root, window, bridge, _ = make_window()
    try:
        window._input.insert("1.0", "how long")
        window._send()
        deadline = time.monotonic() + 5
        while window._busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        # A6: every completed turn says how long it took, so a slow
        # local model reads as slow, not broken.
        assert "window reply (took " in window._chat.get(
            "1.0", "end"
        )
    finally:
        bridge.stop()
        root.destroy()


# ---------------------------------------------------------------- voice


class WindowRecorder(Recorder):
    def __init__(self) -> None:
        self.listening = False
        self.abandoned = 0

    def available(self) -> bool:
        return True

    def start(self) -> None:
        self.listening = True

    def stop(self) -> str:
        self.listening = False
        return "/tmp/window-capture.wav"

    def cancel(self) -> None:
        self.listening = False
        self.abandoned += 1


class WindowTranscriber(TranscriptionProvider):
    def transcribe(self, audio) -> str:
        return "  speak to the window  "


def make_voice_panel() -> VoicePanel:
    return VoicePanel(WindowRecorder(), None, WindowTranscriber(), None)


def test_window_voice_round_trip_uses_the_shared_session() -> None:
    root, window, bridge, _ = make_window(voice=make_voice_panel())
    try:
        assert str(window._mic_button.cget("state")) == "normal"

        window._mic_button.invoke()
        deadline = time.monotonic() + 5
        while not window._listening and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert window._mic_button.cget("text") == "Stop"
        assert window._status.cget("text") == "Listening..."

        window._mic_button.invoke()
        deadline = time.monotonic() + 5
        transcript = ""
        while time.monotonic() < deadline:
            root.update()
            transcript = window._chat.get("1.0", "end")
            if "window reply" in transcript:
                break
            time.sleep(0.02)

        # The transcript appears as user input and the reply followed the
        # exact typed conversation path — one Stella, one session.
        assert "> speak to the window" in transcript
        assert "window reply" in transcript
        assert window._busy is False
        assert window._listening is False
        assert window._mic_button.cget("text") == "Listen"
    finally:
        bridge.stop()
        root.destroy()


def test_window_without_voice_keeps_the_mic_button_disabled() -> None:
    root, window, bridge, _ = make_window()
    try:
        assert str(window._mic_button.cget("state")) == "disabled"
        assert str(window._speak_toggle.cget("state")) == "disabled"
        window._mic_button.invoke()  # a disabled button must do nothing
        pump(root, 0.2)
        assert window._listening is False
    finally:
        bridge.stop()
        root.destroy()


class HangingWindowTranscriber(TranscriptionProvider):
    """Freezes the pre-turn window so the affordance can be observed."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def transcribe(self, audio) -> str:
        self.entered.set()
        self.release.wait(10)
        return "too late"


def test_window_can_cancel_during_transcribing() -> None:
    transcriber = HangingWindowTranscriber()
    panel = VoicePanel(WindowRecorder(), None, transcriber, None)
    root, window, bridge, _ = make_window(voice=panel)
    try:
        window._mic_button.invoke()
        deadline = time.monotonic() + 5
        while not window._listening and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)

        window._mic_button.invoke()  # Stop → the transcribing window opens
        deadline = time.monotonic() + 5
        while not window._transcribing and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert window._transcribing is True
        assert transcriber.entered.wait(2)
        # A8: the mic Cancel button is the affordance for this window.
        assert str(window._mic_cancel.cget("state")) == "normal"

        window._mic_cancel.invoke()

        deadline = time.monotonic() + 5
        transcript = ""
        while time.monotonic() < deadline:
            root.update()
            transcript = window._chat.get("1.0", "end")
            if "cancelled at your request" in transcript:
                break
            time.sleep(0.02)

        # The honest report, and nothing else: no invented transcript, no
        # turn, and the affordance disarms after its one shot.
        assert "cancelled at your request" in transcript
        assert "Nothing was sent" in transcript
        assert "> too late" not in transcript
        assert "window reply" not in transcript
        assert window._transcribing is False
        assert str(window._mic_cancel.cget("state")) == "disabled"
        assert window._busy is False
    finally:
        transcriber.release.set()
        bridge.stop()
        root.destroy()


# ------------------------------------------------------- setup wizard


def make_dialog() -> tuple[tk.Tk, SetupDialog]:
    root = tk.Tk()
    root.withdraw()
    return root, SetupDialog(root)


def test_setup_dialog_maps_even_though_its_parent_is_withdrawn() -> None:
    # Clean-install hang: marking the dialog transient to the withdrawn
    # root made XWayland compositors never map it, so first-run setup
    # blocked in wait_window with nothing on screen forever.
    root, dialog = make_dialog()
    try:
        pump(root, 1.0)
        assert dialog._dialog.winfo_ismapped()
    finally:
        dialog._dialog.destroy()
        root.destroy()


def test_setup_start_button_requires_a_successful_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, dialog = make_dialog()
    try:
        assert str(dialog._finish_button.cget("state")) == "disabled"
        dialog.finish()
        assert dialog.result is None
        monkeypatch.setattr(
            stella_config,
            "test_connection",
            lambda **_kwargs: stella_config.ConnectionTest(False, "nope"),
        )
        dialog.test_connection()
        assert str(dialog._finish_button.cget("state")) == "disabled"
        dialog.finish()
        assert dialog.result is None
        assert "Not connected" in dialog.status.cget("text")
    finally:
        dialog._dialog.destroy()
        root.destroy()


def test_setup_finish_saves_only_after_test_and_selection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    root, dialog = make_dialog()
    try:
        dialog._fields["Model"].insert("0", "qwen3:4b")
        monkeypatch.setattr(
            stella_config,
            "test_connection",
            lambda **_kwargs: stella_config.ConnectionTest(True, "Connected."),
        )
        dialog.test_connection()
        assert str(dialog._finish_button.cget("state")) == "normal"
        dialog.finish()
        assert dialog.result is not None
        assert dialog.result.model == "qwen3:4b"
        saved = stella_config.load_configuration()
        assert saved is not None
        assert saved["provider"] == "ollama"
        assert saved["model"] == "qwen3:4b"
    finally:
        root.destroy()


def test_setup_editing_the_model_invalidates_a_previous_test(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    root, dialog = make_dialog()
    try:
        dialog._fields["Model"].insert("0", "tested:1")
        monkeypatch.setattr(
            stella_config,
            "test_connection",
            lambda **_kwargs: stella_config.ConnectionTest(True, "Connected."),
        )
        dialog.test_connection()
        model_field = dialog._fields["Model"]
        model_field.delete("0", "end")
        model_field.insert("0", "never-tested:2")
        dialog.finish()
        assert dialog.result is None
        assert not (tmp_path / "xdg" / "stella" / "config.json").exists()
    finally:
        dialog._dialog.destroy()
        root.destroy()


def test_setup_refresh_lists_discovered_models_and_selection_fills_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, dialog = make_dialog()
    try:
        monkeypatch.setattr(
            stella_config,
            "scan_ollama_models",
            lambda *args, **kwargs: stella_config.ModelScan(
                True, ("alpha:1", "beta:2"), "Found 2 installed model(s)."
            ),
        )
        dialog.refresh_models()
        assert dialog._model_list.size() == 2
        dialog._model_list.selection_set(1)
        dialog._model_selected(None)
        assert dialog._fields["Model"].get() == "beta:2"
    finally:
        dialog._dialog.destroy()
        root.destroy()


def test_setup_explains_unreachable_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, dialog = make_dialog()
    try:
        monkeypatch.setattr(
            stella_config,
            "scan_ollama_models",
            lambda *args, **kwargs: stella_config.ModelScan(
                False, (), "Ollama is not reachable. Start it with `ollama serve`."
            ),
        )
        dialog.refresh_models()
        assert dialog._model_list.size() == 0
        assert "ollama serve" in dialog.status.cget("text")
        assert str(dialog._finish_button.cget("state")) == "disabled"
    finally:
        dialog._dialog.destroy()
        root.destroy()


def test_setup_api_key_field_is_masked_and_stored_privately(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root, dialog = make_dialog()
    try:
        dialog._preset.set(provider_keys.PRESETS["openai"].label)
        dialog._preset_changed()
        assert dialog._fields["API key"].cget("show") == "*"
        dialog._fields["Model"].insert("0", "gpt-4o-mini")
        dialog._fields["API key"].insert("0", "sk-dialog-secret-value")
        monkeypatch.setattr(
            stella_config,
            "test_connection",
            lambda **_kwargs: stella_config.ConnectionTest(True, "Connected."),
        )
        dialog.test_connection()
        # The verified key moved to the private store — not the
        # environment, not the widgets, and never into config.json.
        assert provider_keys.stored_api_key("openai") == "sk-dialog-secret-value"
        assert "OPENAI_API_KEY" not in os.environ
        assert dialog._fields["API key"].get() == ""
        assert oct(provider_keys.api_keys_path().stat().st_mode).endswith("600")
        dialog.finish()
        saved = (tmp_path / "xdg" / "stella" / "config.json").read_text(
            encoding="utf-8"
        )
        assert "sk-dialog-secret-value" not in saved
        assert "api_key" not in saved
        assert saved.find('"preset": "openai"') >= 0
    finally:
        root.destroy()


def test_setup_key_entry_typed_after_a_passing_test_disables_start(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A passing test now saves; the success must not let a stale flag
    # carry a newly typed, untested key into the store or into a launch.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    root, dialog = make_dialog()
    try:
        dialog._preset.set(provider_keys.PRESETS["openai"].label)
        dialog._preset_changed()
        dialog._fields["Model"].insert("0", "gpt-4o-mini")
        monkeypatch.setattr(
            stella_config,
            "test_connection",
            lambda **_kwargs: stella_config.ConnectionTest(True, "Connected."),
        )
        dialog.test_connection()
        assert str(dialog._finish_button.cget("state")) == "normal"
        # Tk does not deliver synthetic KeyRelease events to ttk.Entry in
        # tests, so pin both halves directly: the key entry is bound to
        # invalidate a test, and invalidation blocks Start and saving.
        assert dialog._fields["API key"].bind("<KeyRelease>") != ""
        dialog._fields["API key"].insert("0", "sk-untested-second-key")
        dialog._mark_untested()
        assert str(dialog._finish_button.cget("state")) == "disabled"
        dialog.finish()
        assert dialog.result is None
        assert provider_keys.stored_api_key("openai") is None
    finally:
        dialog._dialog.destroy()
        root.destroy()


def test_setup_preset_switch_shows_and_hides_the_right_fields(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    root, dialog = make_dialog()
    try:
        assert dialog._rows["API key"].winfo_manager() == ""
        assert dialog._rows["API base URL"].winfo_manager() == ""
        dialog._preset.set(provider_keys.PRESETS["anthropic"].label)
        dialog._preset_changed()
        assert dialog._rows["API key"].winfo_manager() == "pack"
        assert dialog._rows["API base URL"].winfo_manager() == ""
        assert (
            dialog._fields["Model"].cget("values")[0]
            == provider_keys.PRESETS["anthropic"].models[0]
        )
        dialog._preset.set(provider_keys.PRESETS["custom"].label)
        dialog._preset_changed()
        assert dialog._rows["API base URL"].winfo_manager() == "pack"
    finally:
        dialog._dialog.destroy()
        root.destroy()


# -------------------------------------------------- settings tab extras


def test_settings_status_shows_provider_and_model() -> None:
    settings = StellaSettings(provider="ollama", model="qwen3:4b")
    root, window, bridge, _ = make_window(settings=settings)
    try:
        text = window._connection_status.cget("text")
        assert "Provider: ollama" in text
        assert "Model: qwen3:4b" in text
    finally:
        bridge.stop()
        root.destroy()


def test_settings_test_connection_reports_connected_and_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, window, bridge, _ = make_window()
    try:
        monkeypatch.setattr(
            stella_config,
            "test_connection",
            lambda **_kwargs: stella_config.ConnectionTest(True, "Connected."),
        )
        window._test_connection()
        assert "Status: Connected" in window._connection_status.cget("text")
        monkeypatch.setattr(
            stella_config,
            "test_connection",
            lambda **_kwargs: stella_config.ConnectionTest(False, "refused"),
        )
        window._test_connection()
        assert "Status: Not connected" in window._connection_status.cget("text")
        assert "refused" in window._settings_status.cget("text")
    finally:
        bridge.stop()
        root.destroy()


def test_settings_test_connection_without_model_is_local_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, window, bridge, _ = make_window()
    try:
        window._settings_fields["Model"].delete("0", "end")

        def fail_probe(**_kwargs):
            raise AssertionError("must not probe without a model")

        monkeypatch.setattr(stella_config, "test_connection", fail_probe)
        window._test_connection()
        assert "Enter a model first." in window._settings_status.cget("text")
    finally:
        bridge.stop()
        root.destroy()


def test_settings_list_models_is_ollama_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = StellaSettings(provider="openai", model="gpt")
    root, window, bridge, _ = make_window(settings=settings)
    try:
        def fail_scan(*args, **kwargs):
            raise AssertionError("must not scan a non-Ollama provider")

        monkeypatch.setattr(stella_config, "scan_ollama_models", fail_scan)
        window._list_models()
        assert "only available" in window._settings_status.cget("text")
    finally:
        bridge.stop()
        root.destroy()


def test_settings_list_models_fills_the_model_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = StellaSettings(provider="ollama", model="old")
    root, window, bridge, _ = make_window(settings=settings)
    try:
        monkeypatch.setattr(
            stella_config,
            "scan_ollama_models",
            lambda *args, **kwargs: stella_config.ModelScan(
                True, ("fresh:1", "other:2"), "Found 2 installed model(s)."
            ),
        )
        window._list_models()
        assert window._settings_fields["Model"].get() == "fresh:1"
        assert "Found 2" in window._settings_status.cget("text")
    finally:
        bridge.stop()
        root.destroy()


def test_settings_apply_stores_a_matching_key_and_never_writes_environment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder")
    root, window, bridge, _ = make_window(
        settings=StellaSettings(
            provider="openai", model="gpt-4o-mini", preset="openai"
        )
    )
    try:
        captured: list[StellaSettings] = []
        monkeypatch.setattr(bridge, "post_apply_settings", captured.append)
        window._settings_fields["API key"].insert("0", "sk-window-secret")

        window._apply_settings()

        assert provider_keys.stored_api_key("openai") == "sk-window-secret"
        assert os.environ["OPENAI_API_KEY"] == "placeholder"
        assert window._settings_fields["API key"].get() == ""
        assert captured[0].model == "gpt-4o-mini"
        assert captured[0].preset == "openai"
    finally:
        bridge.stop()
        root.destroy()


def test_settings_apply_refuses_a_foreign_key_and_stores_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root, window, bridge, _ = make_window(
        settings=StellaSettings(
            provider="openai", model="gpt-4o-mini", preset="openai"
        )
    )
    try:
        captured: list[StellaSettings] = []
        monkeypatch.setattr(bridge, "post_apply_settings", captured.append)
        window._settings_fields["API key"].insert("0", "sk-ant-api03-sneaky")

        window._apply_settings()

        # The mismatch gate refuses before the store or a restart sees
        # the key; the user gets the switch-provider hint, key-free.
        assert captured == []
        assert provider_keys.stored_api_key("openai") is None
        assert not provider_keys.api_keys_path().exists()
        status = window._settings_status.cget("text")
        assert "Claude (Anthropic)" in status
        assert "sk-ant-api03-sneaky" not in status
    finally:
        bridge.stop()
        root.destroy()


def test_settings_apply_survives_a_disk_error_during_key_storage(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root, window, bridge, _ = make_window(
        settings=StellaSettings(
            provider="openai", model="gpt-4o-mini", preset="openai"
        )
    )

    def _disk_boom(_preset_id: str, _key: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(provider_keys, "save_api_key", _disk_boom)
    try:
        captured: list[StellaSettings] = []
        monkeypatch.setattr(bridge, "post_apply_settings", captured.append)
        window._settings_fields["API key"].insert("0", "sk-proj-doomed")

        window._apply_settings()

        # A disk-level failure lands in the status line, not as an
        # unhandled exception inside the Tk callback.
        assert captured == []
        assert "disk full" in window._settings_status.cget("text")
    finally:
        bridge.stop()
        root.destroy()


def test_settings_apply_without_a_key_leaves_the_environment_alone(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OPENAI_API_KEY", "untouched")
    root, window, bridge, _ = make_window()
    try:
        monkeypatch.setattr(bridge, "post_apply_settings", lambda _s: None)
        window._apply_settings()
        assert os.environ["OPENAI_API_KEY"] == "untouched"
        assert not provider_keys.api_keys_path().exists()
    finally:
        bridge.stop()
        root.destroy()


def test_settings_preset_picker_derives_provider_and_preset(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    root, window, bridge, _ = make_window()
    try:
        window._provider.set(provider_keys.PRESETS["xai"].label)
        window._panel_preset_changed()
        draft = window._draft_settings()
        assert draft.provider == "openai"
        assert draft.preset == "xai"
        # The endpoint is the preset's, shown but not user-editable.
        url = window._settings_fields["OpenAI base URL"]
        assert str(url.cget("state")) == "disabled"
        assert url.get() == provider_keys.PRESETS["xai"].base_url
        window._provider.set(provider_keys.PRESETS["ollama"].label)
        window._panel_preset_changed()
        assert window._draft_settings().provider == "ollama"
        assert window._draft_settings().preset is None
    finally:
        bridge.stop()
        root.destroy()


def test_settings_picker_offers_the_local_free_router(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root, window, bridge, _ = make_window()
    try:
        window._provider.set(provider_keys.PRESETS["freellmapi"].label)
        window._panel_preset_changed()
        draft = window._draft_settings()
        assert draft.provider == "openai"
        assert draft.preset == "freellmapi"
        url = window._settings_fields["OpenAI base URL"]
        assert url.get() == "http://localhost:3001/v1"
    finally:
        bridge.stop()
        root.destroy()


def test_settings_apply_refuses_a_keyless_switch_before_rebuild(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mid-session switching is live, but a preset without a key must be
    # refused in the panel, not posted to a rebuild the worker can only
    # fail. Once a key exists (stored or entered), the same switch goes.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root, window, bridge, _ = make_window()
    try:
        window._provider.set(provider_keys.PRESETS["anthropic"].label)
        window._panel_preset_changed()
        captured: list[StellaSettings] = []
        monkeypatch.setattr(bridge, "post_apply_settings", captured.append)
        window._apply_settings()
        assert captured == []
        status = window._settings_status.cget("text")
        assert "no stored key" in status
        assert provider_keys.stored_api_key("anthropic") is None

        provider_keys.save_api_key("anthropic", "sk-ant-stored")
        window._apply_settings()
        assert len(captured) == 1
        assert captured[0].preset == "anthropic"
    finally:
        bridge.stop()
        root.destroy()


class EchoToolBrain(Brain):
    """Dispatches the safe echo capability once, then answers."""

    def __init__(self) -> None:
        self.first = True

    def decide(self, context: Context, should_cancel=None) -> Decision:
        del should_cancel
        if self.first:
            self.first = False
            return Decision(
                DecisionKind.TOOL,
                capability="echo",
                arguments={"message": "recorded"},
            )
        return Decision(kind=DecisionKind.ANSWER, content="after")


def test_window_history_tab_lists_dispatched_actions() -> None:
    # Stage A A3: the durable trail must be visible in the window, and a
    # turn that dispatches must refresh it without pressing Refresh.
    root, window, bridge, _ = make_window(brain=EchoToolBrain())
    try:
        pump(root, 0.4)
        rows = [
            window._history_list.get(i)
            for i in range(window._history_list.size())
        ]
        assert rows == ["(no actions recorded yet)"]

        window._input.insert("1.0", "echo something")
        window._send()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            root.update()
            rows = [
                window._history_list.get(i)
                for i in range(window._history_list.size())
            ]
            if any("echo" in row for row in rows):
                break
            time.sleep(0.02)
    finally:
        bridge.stop()
        root.destroy()

    assert any("echo" in row and "done" in row for row in rows)
    assert "recorded" not in "".join(rows)


def test_theme_toggle_recolors_window_and_persists(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # The theme is presentation only: toggling must repaint the window
    # and remember the choice, without touching the bridge contract.
    monkeypatch.setattr(
        stella_ui, "_theme_choice_path", lambda: tmp_path / "ui-theme"
    )
    original = stella_ui.current_theme_name()
    try:
        root, window, bridge, _ = make_window()
        try:
            assert window._chat.cget("background") == str(
                stella_ui.THEMES[original].window
            )
            window._toggle_theme()
            root.update()
            other = "light" if original == "dark" else "dark"
            assert stella_ui.current_theme_name() == other
            assert window._chat.cget("background") == str(
                stella_ui.THEMES[other].window
            )
            assert window._root.cget("background") == str(
                stella_ui.THEMES[other].window
            )
            assert (tmp_path / "ui-theme").read_text().strip() == other
            assert window._theme_button.cget("text") == (
                "Dark mode" if other == "light" else "Light mode"
            )
            window._toggle_theme()
            root.update()
            assert window._chat.cget("background") == str(
                stella_ui.THEMES[original].window
            )
            assert (tmp_path / "ui-theme").read_text().strip() == original
        finally:
            bridge.stop()
            root.destroy()
    finally:
        stella_ui.apply_theme(original)


def test_theme_choice_falls_back_to_default(tmp_path) -> None:
    monkeypatch_path = tmp_path / "ui-theme"
    original = stella_ui._theme_choice_path
    stella_ui._theme_choice_path = lambda: monkeypatch_path
    try:
        assert stella_ui.load_theme_choice() == stella_ui.DEFAULT_THEME
        monkeypatch_path.write_text("light\n", encoding="utf-8")
        assert stella_ui.load_theme_choice() == "light"
        monkeypatch_path.write_text("hotdog\n", encoding="utf-8")
        assert stella_ui.load_theme_choice() == stella_ui.DEFAULT_THEME
    finally:
        stella_ui._theme_choice_path = original


def test_nav_rail_switches_sections() -> None:
    # The rail replaces the old notebook: clicking a section shows its
    # panel and marks the nav button active, without touching the bridge.
    root, window, bridge, _ = make_window()
    try:
        assert window._section == "chat"
        window._nav_buttons["memories"].invoke()
        assert window._section == "memories"
        assert str(window._nav_buttons["memories"]["style"]) == (
            "NavActive.TButton"
        )
        assert str(window._nav_buttons["chat"]["style"]) == "Nav.TButton"
        window._nav_buttons["settings"].invoke()
        assert window._section == "settings"
        assert str(window._nav_buttons["settings"]["style"]) == (
            "NavActive.TButton"
        )
    finally:
        bridge.stop()
        root.destroy()


# ------------------------------------------------------------- slash commands


class RecordingBrain(Brain):
    """Counts turns and remembers what the model was asked."""

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def decide(self, context: Context, should_cancel=None) -> Decision:
        del should_cancel
        self.inputs.append(str(context.user_input))
        return Decision(kind=DecisionKind.ANSWER, content="recorded")


def settle(root: tk.Tk, window: StellaWindow, seconds: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while window._busy and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    pump(root, 0.1)


def test_window_status_command_renders_without_a_turn() -> None:
    brain = RecordingBrain()
    root, window, bridge, _ = make_window(brain=brain)
    try:
        window._input.insert("1.0", "/status")
        window._send()
        settle(root, window)
        transcript = window._chat.get("1.0", "end")
        assert "> /status" in transcript
        assert "provider:" in transcript
        assert brain.inputs == []
        assert window._busy is False
    finally:
        bridge.stop()
        root.destroy()


def test_window_template_expands_into_an_ordinary_turn(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STELLA_PERSONA_DIR", str(tmp_path))
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "plan.md").write_text(
        "Plan this: $ARGUMENTS", encoding="utf-8"
    )
    brain = RecordingBrain()
    root, window, bridge, _ = make_window(brain=brain)
    try:
        window._input.insert("1.0", "/plan the launch")
        window._send()
        settle(root, window)
        assert brain.inputs == ["Plan this: the launch"]
        transcript = window._chat.get("1.0", "end")
        assert "> /plan the launch" in transcript
    finally:
        bridge.stop()
        root.destroy()


def test_window_unknown_command_is_a_local_note(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STELLA_PERSONA_DIR", str(tmp_path))
    brain = RecordingBrain()
    root, window, bridge, _ = make_window(brain=brain)
    try:
        window._input.insert("1.0", "/bogusxyz")
        window._send()
        settle(root, window)
        transcript = window._chat.get("1.0", "end")
        assert "no /bogusxyz command" in transcript
        assert brain.inputs == []
    finally:
        bridge.stop()
        root.destroy()


def test_window_exit_command_closes_without_a_turn() -> None:
    brain = RecordingBrain()
    root, window, bridge, _ = make_window(brain=brain)
    closed: list[bool] = []
    window._on_close = lambda: closed.append(True)
    try:
        window._input.insert("1.0", "/exit")
        window._send()
        settle(root, window)
        assert closed == [True]
        assert brain.inputs == []
    finally:
        bridge.stop()
        root.destroy()


def test_window_trace_command_explains_it_is_terminal_only() -> None:
    root, window, bridge, _ = make_window()
    try:
        window._input.insert("1.0", "/trace on")
        window._send()
        settle(root, window)
        transcript = window._chat.get("1.0", "end")
        assert "terminal" in transcript
    finally:
        bridge.stop()
        root.destroy()


def test_turns_bypassing_the_send_guard_are_never_commands() -> None:
    # The voice path posts turns without the typed-input guard in
    # _send: a spoken "/exit" is a sentence, not a command.
    brain = RecordingBrain()
    root, window, bridge, _ = make_window(brain=brain)
    try:
        window._start_turn("/exit")
        settle(root, window)
        assert brain.inputs == ["/exit"]
    finally:
        bridge.stop()
        root.destroy()


def test_window_clear_command_clears_on_the_worker_thread() -> None:
    brain = RecordingBrain()
    root, window, bridge, _ = make_window(brain=brain)
    try:
        window._input.insert("1.0", "hello")
        window._send()
        settle(root, window)
        assert bridge._application.session.history != []
        window._input.insert("1.0", "/clear")
        window._send()
        deadline = time.monotonic() + 5
        while "Conversation history cleared" not in window._chat.get(
            "1.0", "end"
        ) and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        transcript = window._chat.get("1.0", "end")
        assert "Conversation history cleared" in transcript
        assert bridge._application.session.history == []
    finally:
        bridge.stop()
        root.destroy()


def test_window_history_command_shows_a_note() -> None:
    root, window, bridge, _ = make_window()
    try:
        window._input.insert("1.0", "/history")
        window._send()
        deadline = time.monotonic() + 5
        while "action records" not in window._chat.get("1.0", "end") and (
            time.monotonic() < deadline
        ):
            root.update()
            time.sleep(0.02)
        assert "action records" in window._chat.get("1.0", "end")
    finally:
        bridge.stop()
        root.destroy()
