"""Wake-word tests: pure state machines, subprocess-driven ears, bridge wiring.

The same doctrine as tests/test_barge_in.py: the suite never touches a
microphone. Listeners are driven by a Python subprocess writing
deterministic byte counts, and the bridge is wired with fake ears that
only count presses and report callbacks. The one test that loads the
real openWakeWord ONNX chain is skipped unless the ``wake`` extra and
the model files are both present on this machine.
"""

import itertools
import os
import shutil
import sys
import tempfile
import threading
import time

import pytest

from stella.app import (
    DEFAULT_WAKE_MODEL,
    StellaApplication,
    StellaBridge,
    StellaSession,
    StellaSettings,
    VoicePanel,
    build_wake,
    build_wake_ear,
    default_wake_model_dir,
)
from stella.audio_output import SpeechArtifact
from stella.brain import Brain, Decision, DecisionKind
from stella.llm import LLMClient
from stella.mic_tap import MicTap
from stella.tools import ApprovalRequest
from stella.voice import TapRecorder, VoiceError
from stella.wake import (
    FRAME_BYTES,
    WakeEndpoint,
    WakeListener,
    WakeSpotter,
    WakeUtteranceEar,
)

# ----------------------------------------------------------------- endpoint


def test_the_endpointer_needs_sustained_speech_then_silence() -> None:
    endpoint = WakeEndpoint(silence_frames=2)
    assert endpoint.feed(False, 0.0) == "pending"
    for step in range(2):  # two voiced frames: not started yet
        assert endpoint.feed(True, 0.1 * (step + 1)) == "pending"
    assert endpoint.feed(True, 0.3) == "pending"  # third: started
    assert endpoint.feed(False, 0.4) == "pending"
    assert endpoint.feed(False, 0.5) == "complete"
    assert endpoint.feed(True, 0.6) == "complete"  # terminal is sticky


def test_speaking_forever_hits_the_cap_not_a_deadlock() -> None:
    endpoint = WakeEndpoint(max_capture=1.0)
    assert all(endpoint.feed(True, 0.1 * n) == "pending" for n in range(9))
    assert endpoint.feed(True, 1.0) == "cap"


def test_silence_after_a_wake_is_a_honest_timeout() -> None:
    endpoint = WakeEndpoint(speech_timeout=1.0)
    assert endpoint.feed(False, 0.5) == "pending"
    assert endpoint.feed(False, 1.0) == "timeout"
    assert endpoint.feed(True, 1.1) == "timeout"  # sticky too


def test_reset_rearms_the_endpointer_for_the_next_wake() -> None:
    endpoint = WakeEndpoint(silence_frames=1)
    for _ in range(3):
        endpoint.feed(True, 0.0)
    assert endpoint.feed(False, 0.1) == "complete"
    endpoint.reset()
    assert endpoint.finished is None
    # A re-armed ear confirms speech the same way: the documented
    # sustained-speech streak, not a single frame.
    for _ in range(2):
        assert endpoint.feed(True, 0.0) == "pending"
    assert endpoint.feed(True, 0.0) == "pending"
    assert endpoint.feed(False, 0.1) == "complete"


# -------------------------------------------------------------- wake ear


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


def test_the_spotter_fires_once_and_the_latch_holds_until_rearm() -> None:
    seen = []

    def feed(frame: bytes) -> bool:
        seen.append(frame)
        return len(seen) == 3

    arms = []
    listener = WakeListener(
        feed=feed,
        command=python_writer(20),
        on_wake=lambda: arms.append(1),
        reset=lambda: arms.append(0),
    )
    listener.start()
    wait_until(lambda: not listener.running)
    # start() re-arms the latch before it opens the capture (that is the
    # 0); the phrase then fires exactly one wake (the 1), however many
    # frames follow it in the same recording.
    assert arms == [0, 1]


def test_restart_runs_the_rearm_hook_before_listening_again() -> None:
    events = []
    listener = WakeListener(
        feed=lambda frame: False,
        command=python_writer(2, endless=True),
        reset=lambda: events.append("armed"),
    )
    listener.start()
    listener.stop()
    listener.start()
    listener.stop()
    assert events == ["armed", "armed"]


def test_a_faulty_spotter_retires_the_ear_without_waking() -> None:
    wakes = []

    def broken(frame: bytes) -> bool:
        raise RuntimeError("model wedged")

    listener = WakeListener(
        feed=broken,
        command=python_writer(20),
        on_wake=lambda: wakes.append(1),
    )
    listener.start()
    wait_until(lambda: listener.failed)
    assert wakes == []
    listener.start()  # a retired ear never comes back on its own
    assert not listener.running


def test_stop_kills_the_capture_and_silences_the_ear() -> None:
    listener = WakeListener(
        feed=lambda frame: False, command=python_writer(10, endless=True)
    )
    listener.start()
    assert listener.running
    listener.stop()
    assert not listener.running
    listener.stop()  # idempotent


def test_the_utterance_ear_reports_completion_and_outlives_nothing() -> None:
    finishes = []
    replies = iter([(0.9, 0.5)] * 3 + [(0.0, 0.5)] * 20)
    ear = WakeUtteranceEar(
        features=lambda frame: next(replies),
        endpoint=WakeEndpoint(silence_frames=2),
        command=python_writer(30),
        on_finish=finishes.append,
    )
    ear.start()
    wait_until(lambda: finishes != [])
    assert finishes == ["complete"]
    wait_until(lambda: not ear.running)  # tore down its own capture


def test_the_utterance_ear_reports_a_honest_timeout() -> None:
    finishes = []
    # A bounded stand-in clock: 1 s per call is all the timeout needs.
    # list(itertools.count(...)) would materialize an infinite list and
    # eat all the machine's RAM before the ear ever reported anything.
    clock = iter(
        [0.0] + list(itertools.islice(itertools.count(1.0, 1.0), 64))
    )
    ear = WakeUtteranceEar(
        features=lambda frame: (0.0, 0.5),
        endpoint=WakeEndpoint(speech_timeout=2.0),
        command=python_writer(30),
        clock=lambda: next(clock),
        on_finish=finishes.append,
    )
    ear.start()
    wait_until(lambda: finishes != [])
    assert finishes == ["timeout"]


def test_the_utterance_ear_dies_when_its_watcher_breaks() -> None:
    def broken(frame: bytes) -> tuple[float, float]:
        raise RuntimeError("vad wedged")

    finishes = []
    ear = WakeUtteranceEar(
        features=broken,
        endpoint=WakeEndpoint(),
        command=python_writer(20),
        on_finish=finishes.append,
    )
    ear.start()
    wait_until(lambda: ear.failed)
    assert finishes == []


def test_the_wake_ear_takes_frames_from_the_shared_tap() -> None:
    # With a tap the ear reads the one capture Stella owns; the argv it
    # was also given stays unused, which is what keeps the microphone at
    # one open handle instead of two.
    tap = MicTap(command=python_writer(40, endless=True))
    seen: list[bytes] = []

    def feed(frame: bytes) -> bool:
        seen.append(frame)
        return len(seen) == 3

    wakes: list[int] = []
    listener = WakeListener(
        feed=feed,
        command=python_writer(0),
        tap=tap,
        on_wake=lambda: wakes.append(1),
    )
    listener.start()
    wait_until(lambda: wakes == [1])
    assert tap.running()
    listener.stop()
    # Leaving the tap is what turns the microphone off, and only after
    # the last subscriber is gone.
    assert not tap.running()


def test_the_utterance_watcher_endpoints_from_the_shared_tap() -> None:
    tap = MicTap(command=python_writer(40, endless=True))
    replies = iter([(0.9, 0.5)] * 3 + [(0.0, 0.5)] * 40)
    finishes: list[str] = []
    ear = WakeUtteranceEar(
        features=lambda frame: next(replies),
        endpoint=WakeEndpoint(silence_frames=2),
        command=python_writer(0),
        tap=tap,
        on_finish=finishes.append,
    )
    ear.start()
    wait_until(lambda: finishes != [])
    assert finishes == ["complete"]
    # The watcher outlives nothing: leaving the tap closed the capture.
    wait_until(lambda: not tap.running())


def test_a_wake_capture_and_its_watcher_share_one_capture() -> None:
    # The whole point of the tap: a woken utterance is recorded *and*
    # endpointed at the same time, from one microphone handle. Neither
    # consumer may turn the other's capture off.
    tap = MicTap(command=python_writer(200, endless=True))
    recorder = TapRecorder(tap)
    heard: list[bytes] = []

    def features(frame: bytes) -> tuple[float, float]:
        heard.append(frame)
        return 0.0, 0.5

    watcher = WakeUtteranceEar(
        features=features,
        endpoint=WakeEndpoint(silence_frames=2),
        tap=tap,
    )
    recorder.start()
    watcher.start()
    assert tap.running()
    wait_until(lambda: len(heard) >= 4)  # frames are flowing to both
    watcher.stop()
    assert tap.running()  # the recorder never lost its microphone
    path = recorder.stop()
    try:
        assert os.path.getsize(path) > 44
    finally:
        recorder.dispose()
    assert not tap.running()  # and it left when the last one did


def test_a_wake_session_leaves_the_tap_to_the_recorder() -> None:
    # Suspending the ear is a subscription change, not a device change:
    # the wake capture and the push-to-talk recorder use the same tap, so
    # the handover never opens a second handle on the microphone.
    tap = MicTap(command=python_writer(200, endless=True))
    wakes: list[int] = []
    seen: list[bytes] = []

    def feed(frame: bytes) -> bool:
        seen.append(frame)
        return len(seen) == 1

    listener = WakeListener(
        feed=feed,
        command=python_writer(0),
        tap=tap,
        on_wake=lambda: wakes.append(1),
    )
    listener.start()
    wait_until(lambda: wakes == [1])
    listener.stop()
    recorder = TapRecorder(tap)
    recorder.start()
    assert tap.running()
    recorder.cancel()
    assert not tap.running()



# ---------------------------------------------------------------- spotter


def test_spotter_missing_models_report_one_friendly_line(tmp_path) -> None:
    with pytest.raises(VoiceError, match="no melspectrogram model"):
        WakeSpotter(model_dir=str(tmp_path))


def test_spotter_rejects_a_nonsense_threshold(tmp_path) -> None:
    for name in (
        "melspectrogram.onnx",
        "embedding_model.onnx",
        DEFAULT_WAKE_MODEL,
    ):
        (tmp_path / name).write_bytes(b"not really onnx")
    with pytest.raises(ValueError, match="between 0 and 1"):
        WakeSpotter(model_dir=str(tmp_path), threshold=1.5)


def test_a_real_spotter_never_wakes_on_digital_silence() -> None:
    pytest.importorskip("onnxruntime")
    directory = default_wake_model_dir()
    needed = ("melspectrogram.onnx", "embedding_model.onnx", DEFAULT_WAKE_MODEL)
    if not all(os.path.isfile(os.path.join(directory, name)) for name in needed):
        pytest.skip("no openWakeWord models on this machine")
    spotter = WakeSpotter(model_dir=directory)
    for _ in range(300):  # ~9.6 s of silence
        assert spotter.feed(b"\x00" * FRAME_BYTES) is False


# ---------------------------------------------------------------- vad ear


def test_wake_ear_missing_vad_is_one_friendly_error(tmp_path) -> None:
    settings = StellaSettings(
        model="m", wake_word="on", vad_model=str(tmp_path / "absent.onnx")
    )
    with pytest.raises(VoiceError, match="no Silero VAD model"):
        build_wake_ear(settings)


# --------------------------------------------------------------- settings


def clear_wake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "STELLA_MODEL",
        "STELLA_LLM_PROVIDER",
        "STELLA_WAKE_WORD",
        "STELLA_WAKE_SOURCE",
        "STELLA_WAKE_THRESHOLD",
        "STELLA_WAKE_MODEL",
        "STELLA_WAKE_MODEL_DIR",
        "STELLA_VAD_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_wake_defaults_are_off_and_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_wake_env(monkeypatch)
    fields = StellaSettings._environment_fields()
    assert fields["wake_word"] == "off"
    assert fields["wake_source"] is None
    assert fields["wake_threshold"] == 0.5
    assert fields["wake_model"] == DEFAULT_WAKE_MODEL
    assert fields["wake_model_dir"] == default_wake_model_dir()


def test_invalid_wake_settings_exit_at_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_wake_env(monkeypatch)
    monkeypatch.setenv("STELLA_WAKE_WORD", "maybe")
    with pytest.raises(SystemExit, match="STELLA_WAKE_WORD"):
        StellaSettings._environment_fields()
    monkeypatch.setenv("STELLA_WAKE_WORD", "on")
    monkeypatch.setenv("STELLA_WAKE_THRESHOLD", "1.5")
    with pytest.raises(SystemExit, match="STELLA_WAKE_THRESHOLD"):
        StellaSettings._environment_fields()


def test_build_wake_arms_only_on_an_explicit_on(tmp_path) -> None:
    # The default never opens the microphone — not even to look for
    # model files.
    assert build_wake(StellaSettings(model="m")) is None
    assert (
        build_wake(
            StellaSettings(
                model="m",
                wake_word="off",
                wake_model_dir=str(tmp_path),
            )
        )
        is None
    )
    # on arms the ear, and a missing model surfaces as the usual one
    # friendly error instead of silence.
    with pytest.raises(VoiceError, match="no melspectrogram model"):
        build_wake(
            StellaSettings(model="m", wake_word="on", wake_model_dir=str(tmp_path))
        )


# ----------------------------------------------------------------- bridge


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
        path = tempfile.mktemp(prefix="stella-wake-test-")
        with open(path, "wb") as audio:
            audio.write(b"RIFF fake")
        return path

    def cancel(self) -> None:
        self.cancelled += 1

    def dispose(self) -> None:
        pass


class FakeTranscriber:
    """A transcriber with a script the test can change mid-session."""

    def __init__(self, text: str = "hello voice") -> None:
        self.text = text

    def transcribe(self, audio) -> str:
        return self.text


class FakeSpeech:
    def __init__(self) -> None:
        self.directory = None
        # A held render stays in flight until the test releases it, which
        # is how a test catches Stella mid-interlude.
        self.hold: threading.Event | None = None

    def speak(self, output):
        if self.directory is None:
            self.directory = tempfile.mkdtemp(prefix="stella-wake-test-")
        path = os.path.join(self.directory, "reply.wav")
        with open(path, "wb") as audio:
            audio.write(b"RIFF")
        if self.hold is not None:
            assert self.hold.wait(5)
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


class FakeWake:
    """Duck-typed stand-in for one armed wake ear: counts only."""

    def __init__(self, *, failed: bool = False) -> None:
        self.starts = 0
        self.stops = 0
        self.failed = failed
        self.on_wake = None

    def start(self) -> None:
        if self.failed:
            return
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


class FakeWakeEar:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.failed = False
        self.on_finish = None

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


class FakeBarge:
    """Duck-typed barge-in ear: it counts arming and nothing else."""

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.failed = False
        self.on_speech = None

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


class PendingApproval:
    """One genuine request parked in front of the broker on its own thread.

    Driven through the real :class:`ApprovalBroker` rather than by setting
    a flag, because the point of the Stage 3 rule is that the microphone
    stands down for as long as the *broker* is waiting — including for
    however long the answer never comes.
    """

    def __init__(self, bridge: StellaBridge) -> None:
        self.bridge = bridge
        self.token: int | None = None
        self.approved: bool | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        approval = self.bridge.approvals.request(
            ApprovalRequest("cap", {"value": "x"})
        )
        self.approved = approval.approved

    def open(self, timeout: float = 5.0) -> "PendingApproval":
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = self.bridge.next_approval_request()
            if item is not None:
                self.token = item[0]
                return self
            time.sleep(0.02)
        raise AssertionError("no approval request appeared")

    def close(self, approved: bool = False) -> None:
        assert self.token is not None
        assert self.bridge.resolve_approval(self.token, approved) is True
        self._thread.join(5)


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


def make_wake_bridge(
    wake: FakeWake | None,
    ear: FakeWakeEar | None,
    notice: str | None = None,
    barge: FakeBarge | None = None,
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
        wake=wake,
        wake_ear=ear,
        wake_notice=notice,
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
    raise AssertionError(
        f"never saw match; got {[(e.kind, e.payload) for e in events]}"
    )


def voice_state(events, wanted: str):
    return any(
        event.kind == "voice_state" and event.payload == wanted
        for event in events
    )


def test_a_configured_wake_ear_is_armed_at_startup() -> None:
    wake = FakeWake()
    bridge, _panel, _player = make_wake_bridge(wake, FakeWakeEar())
    try:
        # _rebind runs before the constructor returns: arming is settled.
        assert wake.starts == 1
    finally:
        bridge.stop()


def test_a_faulted_wake_ear_retires_silently_without_arming() -> None:
    wake = FakeWake(failed=True)
    bridge, _panel, _player = make_wake_bridge(wake, FakeWakeEar())
    try:
        assert wake.starts == 0
    finally:
        bridge.stop()


def test_an_unusable_wake_ear_reports_itself_once_and_changes_nothing() -> None:
    bridge, panel, player = make_wake_bridge(
        None, None, notice="Wake word found no melspectrogram model."
    )
    try:
        events = drain_until(
            bridge, lambda e: any(x.kind == "voice_error" for x in e)
        )
        notice = next(e for e in events if e.kind == "voice_error")
        assert "Wake word" in str(notice.payload)
        panel.speech_enabled = True
        bridge.post_turn("hi")
        assert drain_until(bridge, lambda e: voice_state(e, "speaking"))
        player.hold.set()
        assert drain_until(bridge, lambda e: voice_state(e, "idle"))
    finally:
        bridge.stop()


def test_a_wake_phrase_starts_the_ordinary_capture_and_endpoints_itself() -> None:
    wake = FakeWake()
    ear = FakeWakeEar()
    bridge, panel, _player = make_wake_bridge(wake, ear)
    try:
        assert wake.starts == 1 and ear.starts == 0
        assert wake.on_wake is not None and ear.on_finish is not None
        wake.on_wake()  # the spotter "heard" the phrase
        assert drain_until(
            bridge, lambda e: voice_state(e, "listening")
        )
        # The mic handover: wake ear down, capture up, watcher armed.
        assert wake.stops >= 1
        assert ear.starts == 1
        recorder = panel._recorder
        assert recorder.started == 1
        ear.on_finish("complete")  # the watcher heard the utterance end
        events = drain_until(
            bridge, lambda e: any(x.kind == "turn" for x in e)
        )
        transcript = [e for e in events if e.kind == "voice_transcript"]
        assert transcript and transcript[0].payload == "hello voice"
        # No speech was enabled, so the ear re-arms right after the turn.
        # A counter predicate has no event to return: drain_until raising
        # is the failure, an empty event list is not.
        drain_until(bridge, lambda e: wake.starts >= 2)
    finally:
        bridge.stop()


def test_a_wake_that_hears_nothing_is_cancelled_with_one_honest_line() -> None:
    wake = FakeWake()
    ear = FakeWakeEar()
    bridge, panel, _player = make_wake_bridge(wake, ear)
    try:
        wake.on_wake()
        drain_until(bridge, lambda e: voice_state(e, "listening"))
        ear.on_finish("timeout")
        events = drain_until(
            bridge,
            lambda e: any(x.kind == "voice_error" for x in e)
            and voice_state(e, "idle"),
        )
        honest = [e for e in events if e.kind == "voice_error"]
        assert "heard no words" in str(honest[-1].payload)
        assert "Nothing was sent" in str(honest[-1].payload)
        drain_until(bridge, lambda e: wake.starts >= 2)
        assert panel._recorder.cancelled == 1
    finally:
        bridge.stop()


def test_a_late_utterance_finish_after_cancel_is_ignored() -> None:
    wake = FakeWake()
    ear = FakeWakeEar()
    bridge, _panel, _player = make_wake_bridge(wake, ear)
    try:
        wake.on_wake()
        drain_until(bridge, lambda e: voice_state(e, "listening"))
        bridge.post_listen_cancel()  # the user pressed Cancel
        drain_until(bridge, lambda e: voice_state(e, "idle"))
        starts_before = wake.starts
        ear.on_finish("complete")  # a retired watcher speaks too late
        time.sleep(0.2)
        events = bridge.poll()
        assert not any(e.kind == "voice_transcript" for e in events)
        assert wake.starts <= starts_before + 1  # no double re-arm storm
    finally:
        bridge.stop()


def test_push_to_talk_takes_the_microphone_back_from_the_wake_ear() -> None:
    wake = FakeWake()
    bridge, _panel, _player = make_wake_bridge(wake, FakeWakeEar())
    try:
        bridge.post_listen_start()
        drain_until(bridge, lambda e: voice_state(e, "listening"))
        assert wake.stops >= 1
        bridge.post_listen_cancel()
        drain_until(bridge, lambda e: voice_state(e, "idle"))
        assert wake.starts >= 2  # re-armed once the button session ended
    finally:
        bridge.stop()


def test_speaking_suspends_the_wake_ear_and_idle_rearms_it() -> None:
    wake = FakeWake()
    bridge, panel, player = make_wake_bridge(wake, FakeWakeEar())
    try:
        panel.speech_enabled = True
        bridge.post_turn("hi")
        drain_until(bridge, lambda e: voice_state(e, "speaking"))
        assert wake.stops >= 1
        player.hold.set()
        drain_until(bridge, lambda e: voice_state(e, "idle"))
        assert wake.starts >= 2
    finally:
        bridge.stop()


def test_narration_suspends_the_wake_ear_while_it_plays() -> None:
    wake = FakeWake()
    bridge, panel, player = make_wake_bridge(wake, FakeWakeEar())
    try:
        assert wake.starts == 1  # armed, because nothing else is on the mic
        render = threading.Event()
        panel._speech.hold = render
        bridge._narrate("thinking", threading.Event())
        # A work phrase is Stella's own voice, and it plays while a turn
        # is in flight with the ear armed: the interlude takes the mic
        # off, exactly like a reply or a capture does.
        drain_until(bridge, lambda e: wake.stops >= 1)
        assert wake.starts == 1
        player.hold.set()
        render.set()
        drain_until(bridge, lambda e: wake.starts >= 2)
    finally:
        bridge.stop()


def test_a_cancelled_interlude_still_releases_the_wake_ear() -> None:
    wake = FakeWake()
    bridge, panel, player = make_wake_bridge(wake, FakeWakeEar())
    try:
        dead = threading.Event()
        render = threading.Event()
        panel._speech.hold = render
        bridge._narrate("thinking", dead)
        drain_until(bridge, lambda e: wake.stops >= 1)
        # The phrase is retired before it ever reaches a speaker: the
        # ear must not stay asleep for a voice that never sounded.
        dead.set()
        player.hold.set()
        render.set()
        drain_until(bridge, lambda e: wake.starts >= 2)
    finally:
        bridge.stop()


def test_a_pending_approval_suppresses_wake_takeover() -> None:
    wake = FakeWake()
    ear = FakeWakeEar()
    bridge, panel, _player = make_wake_bridge(wake, ear)
    approval = PendingApproval(bridge).open()
    try:
        # The dialog itself takes the ear off the microphone: hands-free
        # input is never a way to answer a dangerous-action request.
        assert wake.stops >= 1
        assert wake.starts == 1
        wake.on_wake()  # the user says the phrase at the open dialog
        events = drain_until(
            bridge, lambda e: any(x.kind == "voice_error" for x in e)
        )
        honest = [e for e in events if e.kind == "voice_error"]
        assert "waiting for a decision on screen" in str(honest[-1].payload)
        assert "Allow or Cancel" in str(honest[-1].payload)
        # Refused means refused: no listening state, no recorder, and no
        # utterance watcher armed to endpoint a capture that never began.
        assert not voice_state(events, "listening")
        assert panel._recorder.started == 0
        assert ear.starts == 0
        approval.close()
        drain_until(bridge, lambda e: wake.starts >= 2)
    finally:
        bridge.stop()


def test_an_unanswered_approval_never_strands_a_wake_takeover() -> None:
    # The refusal is not a deferred command: the phrase spoken during the
    # dialog is gone, so answering it later must not start a recording the
    # user stopped caring about.
    wake = FakeWake()
    bridge, panel, _player = make_wake_bridge(wake, FakeWakeEar())
    approval = PendingApproval(bridge).open()
    try:
        wake.on_wake()
        drain_until(bridge, lambda e: any(x.kind == "voice_error" for x in e))
        approval.close()
        drain_until(bridge, lambda e: wake.starts >= 2)
        time.sleep(0.2)
        assert panel._recorder.started == 0
        assert not any(
            e.kind == "voice_state" and e.payload == "listening"
            for e in bridge.poll()
        )
    finally:
        bridge.stop()


def test_a_wake_that_hears_only_the_fillers_own_words_sends_nothing() -> None:
    # Whisper answers a silent capture with "Thank you." — a real string
    # that is not a request. A wake-initiated transcript is the only
    # thing between a mis-detected phrase and a turn nobody asked for, so
    # that filler is read as what it is and never reaches the model.
    wake = FakeWake()
    ear = FakeWakeEar()
    bridge, panel, _player = make_wake_bridge(wake, ear)
    try:
        panel._transcriber.text = "Thank you."
        wake.on_wake()
        drain_until(bridge, lambda e: voice_state(e, "listening"))
        ear.on_finish("complete")
        events = drain_until(
            bridge, lambda e: any(x.kind == "voice_error" for x in e)
        )
        assert not any(e.kind == "voice_transcript" for e in events)
        assert not any(e.kind == "turn" for e in events)
        honest = [e for e in events if e.kind == "voice_error"]
        assert "heard no words" in str(honest[-1].payload)
        assert "Nothing was sent" in str(honest[-1].payload)
        drain_until(bridge, lambda e: wake.starts >= 2)
    finally:
        bridge.stop()


def test_a_pressed_listen_button_is_never_second_guessed() -> None:
    # The filter is for captures nobody asked for. After a deliberate
    # press the user sees the transcript on screen, so what they said —
    # even "thank you" — is sent exactly as heard.
    wake = FakeWake()
    bridge, panel, _player = make_wake_bridge(wake, FakeWakeEar())
    try:
        panel._transcriber.text = "Thank you."
        bridge.post_listen_start()
        drain_until(bridge, lambda e: voice_state(e, "listening"))
        bridge.post_listen_stop()
        events = drain_until(
            bridge, lambda e: any(x.kind == "voice_transcript" for x in e)
        )
        transcript = [e for e in events if e.kind == "voice_transcript"]
        assert transcript[0].payload == "Thank you."
    finally:
        bridge.stop()


def test_a_pending_approval_does_not_arm_barge_in() -> None:
    barge = FakeBarge()
    wake = FakeWake()
    bridge, panel, player = make_wake_bridge(
        wake, FakeWakeEar(), barge=barge
    )
    approval = PendingApproval(bridge).open()
    try:
        panel.speech_enabled = True
        bridge.post_turn("hi")
        drain_until(bridge, lambda e: voice_state(e, "speaking"))
        # Stella talks while a decision is on screen; interrupting her now
        # would cancel the very turn that dialog belongs to.
        assert barge.starts == 0
        player.hold.set()
        drain_until(bridge, lambda e: voice_state(e, "idle"))
        approval.close()
        assert wake.starts >= 2
        bridge.post_turn("again")
        drain_until(bridge, lambda e: voice_state(e, "speaking"))
        # The refusal was for that episode only: the next one arms normally.
        assert barge.starts == 1
        # And the episode retires it again on the way out. (The player is
        # already released, so this may have happened before the poll.)
        drain_until(bridge, lambda e: barge.stops >= 1)
    finally:
        bridge.stop()


# ------------------------------------------------------------- the opt-in box
#
# Stage 6 gives the wake word a checkbox in Settings. Two things that an
# environment-only feature never had to prove now need proving: one
# decision cannot be spelled two contradictory ways, and Apply must arm or
# retire the ear in the session that pressed it.


def test_the_saved_checkbox_and_the_running_mode_never_disagree(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_wake_env(monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    for enabled in (False, True):
        settings = StellaSettings.from_saved(
            provider="ollama", model="m", wake_word_enabled=enabled
        )
        assert settings.wake_word_enabled is enabled
        # build_wake reads the mode and the checkbox reads the bool, so the
        # two spellings of one decision must land on the same microphone.
        assert settings.wake_word == ("on" if enabled else "off")
    # STELLA_WAKE_WORD decides a single launch in either direction, and it
    # decides the bool with it: an override can never leave a saved "off"
    # running an "on" ear, or a saved "on" silently disarmed.
    monkeypatch.setenv("STELLA_WAKE_WORD", "on")
    settings = StellaSettings.from_saved(
        provider="ollama", model="m", wake_word_enabled=False
    )
    assert (settings.wake_word, settings.wake_word_enabled) == ("on", True)
    monkeypatch.setenv("STELLA_WAKE_WORD", "off")
    settings = StellaSettings.from_saved(
        provider="ollama", model="m", wake_word_enabled=True
    )
    assert (settings.wake_word, settings.wake_word_enabled) == ("off", False)
    # The variable-only path agrees with itself for the same reason.
    monkeypatch.setenv("STELLA_MODEL", "m")
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "ollama")
    monkeypatch.delenv("STELLA_WAKE_WORD")
    env = StellaSettings.from_environment()
    assert (env.wake_word, env.wake_word_enabled) == ("off", False)
    monkeypatch.setenv("STELLA_WAKE_WORD", "on")
    env = StellaSettings.from_environment()
    assert (env.wake_word, env.wake_word_enabled) == ("on", True)
    # And the choice survives a launch: written to config.json, read back
    # as the same pair through the path a real start takes.
    from stella import config as stella_config

    monkeypatch.delenv("STELLA_MODEL")
    monkeypatch.delenv("STELLA_LLM_PROVIDER")
    monkeypatch.delenv("STELLA_WAKE_WORD")
    stella_config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama", model="m", wake_word_enabled=True
        )
    )
    read_back = stella_config.resolve_settings()
    assert read_back is not None
    assert (read_back.wake_word, read_back.wake_word_enabled) == ("on", True)


def apply_wake_settings(
    bridge: StellaBridge,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    enabled: bool,
) -> tuple[FakeWake, FakeWakeEar, VoicePanel]:
    """Press Settings → Apply with the box moved to ``enabled``.

    ``stella.app.build_application`` is replaced, so the ear the box would
    create is a counting fake: no microphone opens and no model file is
    read. The saved configuration lands in a temporary data directory, the
    same courtesy every settings test pays.
    """

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    wake = FakeWake()
    ear = FakeWakeEar()
    panel = VoicePanel(
        FakeRecorder(), FakePlayer(), FakeTranscriber(), FakeSpeech()
    )

    def replacement(settings: StellaSettings) -> StellaApplication:
        return StellaApplication(
            StellaSession(make_stella()),
            settings,
            panel,
            wake=wake if enabled else None,
            wake_ear=ear if enabled else None,
        )

    monkeypatch.setattr("stella.app.build_application", replacement)
    bridge.post_apply_settings(
        StellaSettings(
            model="test",
            wake_word="on" if enabled else "off",
            wake_word_enabled=enabled,
        )
    )
    drain_until(bridge, lambda e: any(x.kind == "settings" for x in e))
    return wake, ear, panel


def test_settings_rebind_arms_a_newly_enabled_wake_ear(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ticking the box and pressing Apply arms the ear in this session: no
    # restart, and no second capture for the same microphone.
    bridge, _panel, _player = make_wake_bridge(None, None)
    try:
        wake, ear, panel = apply_wake_settings(
            bridge, monkeypatch, tmp_path, enabled=True
        )
        assert (wake.starts, ear.starts) == (1, 0)
        assert wake.on_wake is not None and ear.on_finish is not None
        # The bridge listens to this ear now, so one phrase is one press.
        wake.on_wake()
        drain_until(bridge, lambda e: voice_state(e, "listening"))
        assert panel._recorder.started == 1
    finally:
        bridge.stop()


def test_disabling_wake_via_apply_stops_the_ear(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wake = FakeWake()
    ear = FakeWakeEar()
    bridge, _panel, _player = make_wake_bridge(wake, ear)
    # The retired application is closed just after the rebind, and its own
    # close() stops the same ears. Reading the counters at that moment is
    # what tells "the rebind took the microphone down" apart from "the
    # cleanup of the object that owned it happened to do so".
    stops_at_close: list[tuple[int, int]] = []
    application = bridge._application
    original_close = application.close

    def close_and_note() -> None:
        stops_at_close.append((wake.stops, ear.stops))
        original_close()

    monkeypatch.setattr(application, "close", close_and_note)
    try:
        apply_wake_settings(bridge, monkeypatch, tmp_path, enabled=False)
        assert stops_at_close, "the retired application never closed"
        wake_stops, ear_stops = stops_at_close[0]
        assert wake_stops >= 1, "unchecking wake left the spotter armed"
        assert ear_stops >= 1, "unchecking wake left the utterance ear open"
        # Unticking is final for this session: nothing re-arms afterwards.
        assert wake.starts == 1 and ear.starts == 0
    finally:
        bridge.stop()
