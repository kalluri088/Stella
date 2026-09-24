"""Phase 4 Tk window tests.

These exercise the real widgets when a display is available and skip
otherwise. The window may only talk to Stella through ``StellaBridge``,
so the tests assert exactly that: widget actions post bridge commands,
approval dialogs answer the dispatcher's live request, and closing a
dialog denies rather than fabricates authorization.
"""

import datetime as dt
import os
import threading
import time
import tkinter as tk

import pytest

from stella import config as stella_config
from stella.app import (
    StellaApplication,
    StellaBridge,
    StellaSession,
    StellaSettings,
    TurnOutcome,
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
        assert "You: hello window" in transcript
        assert "Stella: window reply" in transcript
        assert window._busy is False
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
        dialog.event_generate("<Escape>")
        # Poll for the denial to land instead of trusting a fixed sleep:
        # under full-suite load the worker round-trip can exceed it.
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
        assert "Stella: window reply" not in transcript
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
    assert StellaWindow._duration_tail(TurnOutcome()) == ""
    assert (
        StellaWindow._duration_tail(TurnOutcome(duration_seconds=1.24))
        == " (took 1.2 s)"
    )
    assert (
        StellaWindow._duration_tail(TurnOutcome(duration_seconds=47.4))
        == " (took 47 s)"
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
        assert "Stella: window reply (took " in window._chat.get(
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
            if "Stella: window reply" in transcript:
                break
            time.sleep(0.02)

        # The transcript appears as user input and the reply followed the
        # exact typed conversation path — one Stella, one session.
        assert "You (voice): speak to the window" in transcript
        assert "Stella: window reply" in transcript
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
        assert "You (voice)" not in transcript
        assert "Stella:" not in transcript
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


def test_setup_api_key_field_is_masked_and_never_persisted(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root, dialog = make_dialog()
    try:
        dialog._mode.set("openai")
        dialog._mode_changed()
        assert dialog._fields["API key"].cget("show") == "*"
        dialog._fields["Model"].insert("0", "gpt-4o-mini")
        dialog._fields["API key"].insert("0", "sk-dialog-secret-value")
        monkeypatch.setattr(
            stella_config,
            "test_connection",
            lambda **_kwargs: stella_config.ConnectionTest(True, "Connected."),
        )
        dialog.test_connection()
        # The typed key moved to this process's environment and vanished
        # from the widget; the saved configuration contains no key material.
        assert os.environ["OPENAI_API_KEY"] == "sk-dialog-secret-value"
        assert dialog._fields["API key"].get() == ""
        dialog.finish()
        saved = (tmp_path / "xdg" / "stella" / "config.json").read_text(
            encoding="utf-8"
        )
        assert "sk-dialog-secret-value" not in saved
        assert "api_key" not in saved
    finally:
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


def test_settings_apply_moves_entered_key_to_environment_and_clears_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, window, bridge, _ = make_window()
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "placeholder")
        captured: list[StellaSettings] = []
        monkeypatch.setattr(bridge, "post_apply_settings", captured.append)
        window._settings_fields["API key"].insert("0", "sk-window-secret")

        window._apply_settings()

        assert os.environ["OPENAI_API_KEY"] == "sk-window-secret"
        assert window._settings_fields["API key"].get() == ""
        assert captured[0].model == "test"
    finally:
        bridge.stop()
        root.destroy()


def test_settings_apply_without_a_key_leaves_the_environment_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, window, bridge, _ = make_window()
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "untouched")
        monkeypatch.setattr(bridge, "post_apply_settings", lambda _s: None)
        window._apply_settings()
        assert os.environ["OPENAI_API_KEY"] == "untouched"
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
