"""Barge-in tests: pure judgment, a real subprocess listener, bridge wiring.

The suite never touches a microphone or speakers: the listener is driven
with a Python subprocess that writes deterministic byte counts, and the
bridge is wired with a fake ear that only counts presses. The one test
that loads the real VAD model is skipped unless the ``barge-in`` extra
and the model file are both present on the machine.
"""

import os
import shutil
import sys
import tempfile
import threading
import time

import pytest

from stella.app import (
    StellaApplication,
    StellaBridge,
    StellaSession,
    StellaSettings,
    VoicePanel,
    build_barge_in,
    default_vad_model,
)
from stella.audio_output import SpeechArtifact
from stella.barge_in import (
    FRAME_BYTES,
    BargeInJudge,
    BargeInListener,
    SileroVad,
    capture_command,
)
from stella.brain import Brain, Decision, DecisionKind
from stella.llm import LLMClient
from stella.voice import VoiceError

# ------------------------------------------------------------------ judge


def make_judge(**kwargs: float) -> BargeInJudge:
    return BargeInJudge(**kwargs)


def test_sustained_voiced_frames_fire_exactly_once() -> None:
    judge = make_judge()
    results = [judge.feed(0.9, 0.5) for _ in range(9)]
    assert results == [False] * 4 + [True] + [False] * 4


def test_a_break_in_the_streak_restarts_confirmation() -> None:
    judge = make_judge()
    assert not any(judge.feed(0.9, 0.5) for _ in range(4))
    assert judge.feed(0.05, 0.5) is False  # one quiet frame resets
    assert not any(judge.feed(0.9, 0.5) for _ in range(4))
    assert judge.feed(0.9, 0.5) is True


def test_quiet_energy_never_counts_as_voiced() -> None:
    # The energy gate sits at the measured echo-cancel residue floor:
    # a high VAD probability under it is cancelled playback, not speech.
    judge = make_judge()
    assert not any(judge.feed(0.99, 0.005) for _ in range(30))


def test_reset_rearms_the_judge_for_the_next_episode() -> None:
    judge = make_judge()
    assert any(judge.feed(0.9, 0.5) for _ in range(5))
    judge.reset()
    assert not any(judge.feed(0.9, 0.5) for _ in range(4))
    assert judge.feed(0.9, 0.5) is True


# ------------------------------------------------------------ capture cmd


def test_pw_record_is_preferred_and_targets_the_echo_cancelled_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "stella.barge_in.shutil.which",
        lambda name: "/usr/bin/pw-record" if name == "pw-record" else None,
    )
    assert capture_command(None) == [
        "pw-record",
        "-a",
        "--rate",
        "16000",
        "--channels",
        "1",
        "-",
    ]
    assert capture_command("ec_mic")[-3:] == ["--target", "ec_mic", "-"]


def test_arecord_is_the_raw_alsa_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stella.barge_in.shutil.which", lambda name: None)
    argv = capture_command(None)
    assert argv[0] == "arecord" and argv[-1] == "-"
    assert "-t" in argv and "raw" in argv


# ---------------------------------------------------------------- listener


def python_writer(frames: int, *, endless: bool = False) -> list[str]:
    body = (
        "import sys,time\n"
        f"payload=b'\\x00'*{FRAME_BYTES * frames}\n"
        + (
            "sys.stdout.buffer.write(payload)\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(30)\n"
            if endless
            else f"sys.stdout.buffer.write(b'\\x00'*{FRAME_BYTES * frames})\n"
        )
    )
    return [sys.executable, "-c", body]


def wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("condition never held")


def test_five_voiced_frames_press_the_button_exactly_once() -> None:
    presses = []
    listener = BargeInListener(
        features=lambda frame: (0.9, 0.5),
        command=python_writer(20),
        on_speech=lambda: presses.append(1),
    )
    listener.start()
    wait_until(lambda: not listener.running)
    assert len(presses) == 1


def test_short_sounds_below_the_confirmation_window_never_press() -> None:
    presses = []
    replies = iter([(0.9, 0.5)] * 4 + [(0.01, 0.5)] * 30)
    listener = BargeInListener(
        features=lambda frame: next(replies),
        command=python_writer(20),
        on_speech=lambda: presses.append(1),
    )
    listener.start()
    wait_until(lambda: not listener.running)
    assert presses == []


def test_a_faulty_vad_retires_the_ear_and_presses_nothing() -> None:
    def broken(frame: bytes) -> tuple[float, float]:
        raise RuntimeError("model wedged")

    presses = []
    listener = BargeInListener(
        features=broken,
        command=python_writer(20),
        on_speech=lambda: presses.append(1),
    )
    listener.start()
    wait_until(lambda: listener.failed)
    assert presses == []
    listener.start()  # a retired ear never comes back on its own
    assert not listener.running


def test_stop_kills_the_capture_and_disarms_the_ear() -> None:
    listener = BargeInListener(
        features=lambda frame: (0.0, 0.0),
        command=python_writer(10, endless=True),
    )
    listener.start()
    assert listener.running
    listener.stop()
    assert not listener.running
    listener.stop()  # idempotent


# -------------------------------------------------------------------- vad


def test_silero_vad_scores_a_silent_frame_as_silence() -> None:
    pytest.importorskip("onnxruntime")
    import os

    if not os.path.isfile(default_vad_model()):
        pytest.skip("no Silero VAD model file on this machine")
    vad = SileroVad(default_vad_model())
    probability, rms = vad.features(b"\x00" * FRAME_BYTES)
    assert rms == 0.0
    assert probability < 0.5


def test_missing_model_is_one_friendly_error(tmp_path) -> None:
    with pytest.raises(VoiceError, match="no Silero VAD model"):
        SileroVad(str(tmp_path / "absent.onnx"))


# ----------------------------------------------------------------- settings


def clear_voice_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "STELLA_MODEL",
        "STELLA_LLM_PROVIDER",
        "STELLA_VOICE_BARGE_IN",
        "STELLA_BARGE_THRESHOLD",
        "STELLA_BARGE_SOURCE",
        "STELLA_VAD_MODEL",
        "STELLA_TRANSCRIPTION_COMMAND",
        "STELLA_SPEECH_COMMAND",
    ):
        monkeypatch.delenv(name, raising=False)


def test_barge_in_defaults_are_auto_and_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_voice_env(monkeypatch)
    fields = StellaSettings._environment_fields()
    assert fields["voice_barge_in"] == "auto"
    assert fields["barge_threshold"] == 0.5
    assert fields["barge_source"] is None
    assert fields["vad_model"] == default_vad_model()


def test_invalid_barge_in_settings_exit_at_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_voice_env(monkeypatch)
    monkeypatch.setenv("STELLA_VOICE_BARGE_IN", "maybe")
    with pytest.raises(SystemExit, match="STELLA_VOICE_BARGE_IN"):
        StellaSettings._environment_fields()
    monkeypatch.setenv("STELLA_VOICE_BARGE_IN", "on")
    monkeypatch.setenv("STELLA_BARGE_THRESHOLD", "1.5")
    with pytest.raises(SystemExit, match="STELLA_BARGE_THRESHOLD"):
        StellaSettings._environment_fields()


def test_build_barge_in_auto_arms_only_on_a_named_source(
    tmp_path,
) -> None:
    # Default auto with no declared source: the ear stays out of the
    # picture entirely — not even the model is looked for (a raw mic
    # hears the speakers and live measurement showed it self-fires).
    assert build_barge_in(StellaSettings(model="m")) is None
    # auto + an explicitly named (echo-cancelled) source arms the ear:
    # an absent model then surfaces as the usual one friendly error.
    settings = StellaSettings(
        model="m",
        barge_source="ec_mic",
        vad_model=str(tmp_path / "absent.onnx"),
    )
    with pytest.raises(VoiceError):
        build_barge_in(settings)
    # off never arms, even with a source; on arms even without one.
    assert (
        build_barge_in(
            StellaSettings(
                model="m",
                voice_barge_in="off",
                barge_source="ec_mic",
                vad_model=str(tmp_path / "absent.onnx"),
            )
        )
        is None
    )
    settings = StellaSettings(
        model="m",
        voice_barge_in="on",
        vad_model=str(tmp_path / "absent.onnx"),
    )
    with pytest.raises(VoiceError):
        build_barge_in(settings)


# ------------------------------------------------------------------ bridge


class SpyLLM(LLMClient):
    def chat(self, messages, should_cancel=None):
        del messages, should_cancel
        return "the action completed"


class ScriptedBrain(Brain):
    def __init__(self, content: str) -> None:
        self.content = content

    def decide(self, context, should_cancel=None):
        del context, should_cancel
        return Decision(kind=DecisionKind.ANSWER, content=self.content)


class FakeRecorder:
    def __init__(self) -> None:
        self.started = 0
        self.cancelled = 0

    def available(self) -> bool:
        return True

    def start(self) -> None:
        self.started += 1

    def stop(self) -> str:
        path = "/tmp/stella-test-capture.wav"
        with open(path, "wb") as audio:
            audio.write(b"RIFF fake")
        return path

    def cancel(self) -> None:
        self.cancelled += 1

    def dispose(self) -> None:
        pass


class FakeTranscriber:
    def transcribe(self, audio) -> str:
        return "hello voice"


class FakeSpeech:
    def __init__(self) -> None:
        self.directory = None

    def speak(self, output):
        if self.directory is None:
            self.directory = tempfile.mkdtemp(prefix="stella-barge-test-")
        path = os.path.join(self.directory, "reply.wav")
        with open(path, "wb") as audio:
            audio.write(b"RIFF")
        return SpeechArtifact(reference=path)

    def dispose(self) -> None:
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)


class FakePlayer:
    def __init__(self) -> None:
        self.hold = threading.Event()
        self.stops = 0
        self.played = 0

    def available(self) -> bool:
        return True

    def play(self, path: str) -> None:
        self.played += 1
        assert self.hold.wait(5)

    def stop(self) -> None:
        self.stops += 1


class FakeBarge:
    def __init__(self, *, failed: bool = False) -> None:
        self.starts = 0
        self.stops = 0
        self.failed = failed
        self.on_speech = None

    def start(self) -> None:
        if self.failed:
            return
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


def make_stella(content: str = "noted"):
    from stella.memory import InMemoryMemory
    from stella.stella import Stella
    from stella.tools import EchoTool, ToolDispatcher

    return Stella(
        ScriptedBrain(content),
        SpyLLM(),
        ToolDispatcher([EchoTool()]),
        InMemoryMemory(),
    )


def make_barge_bridge(
    barge: FakeBarge | None, notice: str | None = None
) -> tuple[StellaBridge, VoicePanel, FakePlayer]:
    player = FakePlayer()
    panel = VoicePanel(
        FakeRecorder(), player, FakeTranscriber(), FakeSpeech()
    )
    application = StellaApplication(
        StellaSession(make_stella()),
        StellaSettings(model="test"),
        panel,
        barge_in=barge,
        barge_notice=notice,
    )
    return StellaBridge(lambda: application), panel, player


def drain_until(bridge: StellaBridge, predicate, timeout: float = 5.0):
    events = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events.extend(bridge.poll())
        if predicate(events):
            return events
        time.sleep(0.02)
    raise AssertionError(f"never saw {predicate}; got {[(e.kind, e.payload) for e in events]}")


def speaking_state(events) -> bool:
    return any(
        event.kind == "voice_state" and event.payload == "speaking"
        for event in events
    )


def idle_state(events) -> bool:
    return any(
        event.kind == "voice_state" and event.payload == "idle"
        for event in events
    )


def test_the_ear_is_armed_for_one_speaking_episode_only() -> None:
    barge = FakeBarge()
    bridge, panel, player = make_barge_bridge(barge)
    try:
        panel.speech_enabled = True
        bridge.post_turn("hi")
        drain_until(bridge, speaking_state)
        assert barge.starts == 1
        player.hold.set()
        drain_until(bridge, idle_state)
        assert barge.stops >= 1
    finally:
        bridge.stop()


def test_a_barge_in_press_is_exactly_a_cancel_button_press() -> None:
    barge = FakeBarge()
    bridge, panel, player = make_barge_bridge(barge)
    try:
        panel.speech_enabled = True
        bridge.post_turn("hi")
        drain_until(bridge, speaking_state)
        assert barge.on_speech is not None
        barge.on_speech()  # the detector "heard" sustained speech
        wait_until(lambda: player.stops >= 1)
        player.hold.set()
        drain_until(bridge, idle_state)
        # A cancelled turn never leaks into the conversation: the next
        # turn runs normally because submission re-armed the flag.
        bridge.post_turn("again")
        events = drain_until(bridge, lambda e: any(x.kind == "turn" for x in e))
        turn = [e for e in events if e.kind == "turn"][-1]
        assert turn.payload.cancelled is False
    finally:
        bridge.stop()


def test_push_to_talk_takes_the_microphone_back_from_the_ear() -> None:
    barge = FakeBarge()
    bridge, panel, player = make_barge_bridge(barge)
    try:
        panel.speech_enabled = True
        bridge.post_turn("hi")
        drain_until(bridge, speaking_state)
        bridge.post_listen_start()
        drain_until(
            bridge,
            lambda e: any(
                event.kind == "voice_state"
                and event.payload == "listening"
                for event in e
            ),
        )
        assert barge.stops >= 1
        player.hold.set()
    finally:
        bridge.stop()


def test_an_unusable_ear_reports_itself_once_and_changes_nothing() -> None:
    bridge, panel, player = make_barge_bridge(None, notice="Barge-in found no model.")
    try:
        events = drain_until(
            bridge, lambda e: any(x.kind == "voice_error" for x in e)
        )
        notice = next(e for e in events if e.kind == "voice_error")
        assert "Barge-in" in str(notice.payload)
        panel.speech_enabled = True
        bridge.post_turn("hi")
        drain_until(bridge, speaking_state)  # plain voice still works
        player.hold.set()
        drain_until(bridge, idle_state)
    finally:
        bridge.stop()


def test_a_faulted_ear_is_retired_without_any_new_capture() -> None:
    barge = FakeBarge(failed=True)
    bridge, panel, player = make_barge_bridge(barge)
    try:
        panel.speech_enabled = True
        bridge.post_turn("hi")
        drain_until(bridge, speaking_state)
        assert barge.starts == 0  # never armed once failed
        player.hold.set()
        drain_until(bridge, idle_state)
    finally:
        bridge.stop()
