"""Tests for `stella voice`: the headless one-shot turn.

The whole point of these tests is that the orchestration is honest and
fail-closed without a microphone, a VAD model, or a real Brain: every
peripheral is a fake, and the assertions are about what the code refuses
to do (invent a transcript, run a turn on silence, speak a file body,
approve an unclear answer) as much as what it does.
"""

import os
import tempfile
import threading
from dataclasses import dataclass

import pytest

from stella import headless_voice as hv
from stella.tools import ApprovalRequest
from stella.voice import VoiceError


@dataclass
class FakeSettings:
    # A dataclass because the production path is real: headless_settings
    # is dataclasses.replace over whatever resolve_settings returned, so
    # this fake must carry exactly the fields that override forces off.
    wake_source: str | None = None
    wake_word: str = "on"
    wake_word_enabled: bool = True
    shell_tools_enabled: bool = True
    browser_tools_enabled: bool = True


class FakeStella:
    def __init__(self) -> None:
        self.approval_provider = None


class FakeOutcome:
    def __init__(self, result="RESULT", error_message=None, cancelled=False):
        self.result = result
        self.error_message = error_message
        self.cancelled = cancelled


class FakeSession:
    def __init__(self, outcome):
        self.stella = FakeStella()
        self._outcome = outcome
        self.turns = []

    def run_turn(self, user_input, spoken=False):
        self.turns.append((user_input, spoken))
        return self._outcome


class FakeVoice:
    def __init__(self, transcript="hello", input_ok=True, output_ok=True):
        self._transcript = transcript
        self.input_available = input_ok
        self.output_available = output_ok
        self.speech_enabled = False
        self.taps = []
        self.prewarmed = 0
        self.transcribed = 0
        self.abandoned = 0
        self.played = []
        self.disposed = []
        self.phrases = []
        self.results = []
        self.fail_start = False
        self.fail_transcribe = False

    def prewarm_speech(self):
        self.prewarmed += 1

    def attach_tap(self, tap):
        self.taps.append(tap)

    def start_listening(self):
        if self.fail_start:
            raise VoiceError("no microphone")

    def stop_and_transcribe(self):
        self.transcribed += 1
        if self.fail_transcribe:
            raise VoiceError("no speech recognized")
        return self._transcript

    def abandon_listening(self):
        self.abandoned += 1

    def synthesize(self, result):
        self.results.append(result)
        return "/tmp/stella-result.wav"

    def synthesize_phrase(self, text):
        self.phrases.append(text)
        return "/tmp/stella-phrase.wav"

    def play(self, path):
        self.played.append(path)

    def dispose_artifact(self, path):
        self.disposed.append(path)


class FakeTap:
    def __init__(self, command=None):
        self.command = command
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeEar:
    def __init__(self, kind="complete", fire=True):
        self.kind = kind
        self.fire = fire
        self.on_finish = None
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        if self.fire:
            self.on_finish(self.kind)

    def stop(self):
        self.stopped = True


class FakeApplication:
    def __init__(self, voice, session):
        self.settings = FakeSettings()
        self.voice = voice
        self.session = session
        self.closed = False

    def close(self):
        self.closed = True


class FakeWake:
    """Stand-in for the spotter ear: counts arms, can ring its own bell.

    ``fire_on_rearm`` models the owner walking up to the idle ear between
    sessions — the second arm wakes Stella on the spot, which is exactly
    what a saved wake setting is supposed to do.
    """

    def __init__(self, fire_on_rearm=False):
        self.on_wake = None
        self.starts = 0
        self.stops = 0
        self.running = False
        self.fire_on_rearm = fire_on_rearm

    def start(self):
        self.starts += 1
        self.running = True
        if self.fire_on_rearm and self.starts > 1 and self.on_wake is not None:
            self.on_wake()

    def stop(self):
        self.stops += 1
        self.running = False


class FakeServer:
    """A _ControlServer stand-in that records construction and closure."""

    def __init__(self, path, on_command):
        self.path = path
        self.on_command = on_command
        self.closed = False

    def close(self):
        self.closed = True


def _server_box(box):
    def factory(path, on_command):
        server = FakeServer(path, on_command)
        box.append(server)
        return server

    return factory


def _raising(message):
    def boom():
        raise VoiceError(message)

    return boom


def _wire(monkeypatch, voice, ear_factory):
    """Point the module's real collaborators at fakes for one run."""

    session = FakeSession(FakeOutcome())
    application = FakeApplication(voice, session)
    monkeypatch.setattr(hv, "resolve_settings", lambda: FakeSettings())
    monkeypatch.setattr(hv, "build_application", lambda settings: application)
    monkeypatch.setattr(hv, "MicTap", FakeTap)
    monkeypatch.setattr(hv, "capture_command", lambda source: ["fake-cmd"])
    monkeypatch.setattr(hv, "build_wake_ear", lambda settings, tap: ear_factory())
    return application, session


def _wire_serve(
    monkeypatch,
    voice,
    *,
    wake=None,
    ear_factory=lambda: FakeEar("complete"),
    server_factory=None,
    loop_recorder=None,
):
    """Wire the resident path's collaborators at once.

    Same microphone doctrine as ``_wire`` — nothing here may touch a
    real device, socket path, or model: the control server, the wake
    listener and the conversation loop are all replaceable seams.
    """

    application, session = _wire(monkeypatch, voice, ear_factory)
    monkeypatch.setattr(hv, "control_path", lambda: "/tmp/stella-test.sock")
    if wake is None:
        wake = FakeWake()
    monkeypatch.setattr(hv, "build_wake", lambda settings, tap: wake)
    if server_factory is not None:
        monkeypatch.setattr(hv, "_ControlServer", server_factory)
    if loop_recorder is not None:
        monkeypatch.setattr(
            hv, "_conversation_loop", lambda *args: loop_recorder.append(args)
        )
    return application, session, wake


def test_headless_settings_disarms_every_continuous_ear() -> None:
    # The shortcut's promise against real settings: whatever the saved
    # config turned on, the headless wrapper turns off, and every other
    # field survives byte for byte.
    from stella.app import StellaSettings

    saved = StellaSettings(
        model="m",
        wake_word="on",
        wake_word_enabled=True,
        wake_models=("hey_stella.onnx",),
        shell_tools_enabled=True,
        browser_tools_enabled=True,
    )
    forced = hv.headless_settings(saved)
    assert forced.wake_word == "off"
    assert forced.wake_word_enabled is False
    assert forced.shell_tools_enabled is False
    assert forced.browser_tools_enabled is False
    assert forced.wake_models == saved.wake_models


class TestRunHeadlessVoice:
    def test_unconfigured_is_honest_and_exits_2(self, monkeypatch, capsys):
        monkeypatch.setattr(hv, "resolve_settings", lambda: None)
        assert hv.run_headless_voice() == 2
        assert "not configured" in capsys.readouterr().err.lower()

    def test_missing_output_is_a_refusal_not_a_fake_success(
        self, monkeypatch
    ):
        voice = FakeVoice(output_ok=False)
        application, session = _wire(
            monkeypatch, voice, lambda: FakeEar("complete")
        )
        assert hv.run_headless_voice() == 3
        assert session.turns == []
        assert application.closed is True

    def test_a_heard_request_runs_one_spoken_turn(self, monkeypatch):
        voice = FakeVoice(transcript="what time is it")
        application, session = _wire(
            monkeypatch, voice, lambda: FakeEar("complete")
        )
        assert hv.run_headless_voice() == 0
        # The transcript reaches run_turn exactly as typed input would,
        # marked spoken, and nothing else.
        assert session.turns == [("what time is it", True)]
        assert voice.results == ["RESULT"]
        assert voice.prewarmed == 1
        assert application.closed is True
        # The result artifact was played then removed.
        assert "/tmp/stella-result.wav" in voice.played
        assert "/tmp/stella-result.wav" in voice.disposed

    def test_silence_runs_no_turn_and_says_so(self, monkeypatch):
        voice = FakeVoice()
        _application, session = _wire(
            monkeypatch, voice, lambda: FakeEar("timeout")
        )
        assert hv.run_headless_voice() == 0
        assert session.turns == []
        assert voice.transcribed == 0
        assert voice.abandoned == 1
        assert any("catch" in p.lower() for p in voice.phrases)

    def test_a_dead_microphone_is_read_as_nothing_heard(self, monkeypatch):
        voice = FakeVoice()
        voice.fail_start = True
        _wire(monkeypatch, voice, lambda: FakeEar("complete"))
        assert hv.run_headless_voice() == 0
        assert voice.abandoned == 0  # nothing was ever started to abandon
        assert any("catch" in p.lower() for p in voice.phrases)

    def test_failed_transcription_is_not_invented(self, monkeypatch):
        voice = FakeVoice()
        voice.fail_transcribe = True
        _application, session = _wire(
            monkeypatch, voice, lambda: FakeEar("complete")
        )
        assert hv.run_headless_voice() == 0
        assert session.turns == []
        assert any("catch" in p.lower() for p in voice.phrases)

    def test_a_turn_error_is_spoken_not_faked(self, monkeypatch):
        voice = FakeVoice(transcript="do the thing")
        session = FakeSession(FakeOutcome(result=None, error_message="boom"))
        application = FakeApplication(voice, session)
        monkeypatch.setattr(hv, "resolve_settings", lambda: FakeSettings())
        monkeypatch.setattr(hv, "build_application", lambda settings: application)
        monkeypatch.setattr(hv, "MicTap", FakeTap)
        monkeypatch.setattr(hv, "capture_command", lambda source: ["fake-cmd"])
        monkeypatch.setattr(
            hv, "build_wake_ear", lambda settings, tap: FakeEar("complete")
        )
        assert hv.run_headless_voice() == 1
        assert voice.results == []  # no result to speak
        assert any("couldn't" in p.lower() for p in voice.phrases)


class TestCapture:
    def test_one_tap_is_shared_across_captures(self, monkeypatch):
        voice = FakeVoice(transcript="first")
        monkeypatch.setattr(hv, "MicTap", FakeTap)
        monkeypatch.setattr(hv, "capture_command", lambda source: ["fake-cmd"])
        monkeypatch.setattr(
            hv, "build_wake_ear", lambda settings, tap: FakeEar("complete")
        )
        capture = hv._make_capture(FakeSettings(), voice)
        assert len(voice.taps) == 1
        assert capture() == ("first", "complete")
        assert capture() == ("first", "complete")
        # Reused, never a second handle on the same device.
        assert len(voice.taps) == 1

    def test_wait_timeout_is_read_as_silence(self, monkeypatch):
        voice = FakeVoice()
        monkeypatch.setattr(hv, "CAPTURE_WAIT_SECONDS", 0.05)
        monkeypatch.setattr(hv, "MicTap", FakeTap)
        monkeypatch.setattr(hv, "capture_command", lambda source: ["fake-cmd"])
        # The watcher never calls back: a reader that died mid-capture.
        monkeypatch.setattr(
            hv, "build_wake_ear", lambda settings, tap: FakeEar("complete", fire=False)
        )
        capture = hv._make_capture(FakeSettings(), voice)
        assert capture() == ("", "timeout")
        assert voice.abandoned == 1


class TestApprover:
    def _request(self):
        return ApprovalRequest(
            capability="filesystem_write",
            arguments={"path": "/notes/x.txt", "content": "TOPSECRETBODY"},
        )

    def test_clear_yes_approves_the_exact_request(self):
        voice = FakeVoice()
        approve = hv._make_approver(voice, lambda: ("yes", "complete"))
        approval = approve(self._request(), None)
        assert approval.approved is True
        assert approval.request == self._request()

    @pytest.mark.parametrize(
        "answer",
        [
            ("no", "complete"),
            ("yes no", "complete"),
            ("maybe", "complete"),
            ("", "complete"),
            ("yes", "timeout"),  # affirmative heard, but capture never ended
        ],
    )
    def test_anything_unclear_denies(self, answer):
        voice = FakeVoice()
        approve = hv._make_approver(voice, lambda: answer)
        assert approve(self._request(), None).approved is False

    def test_a_file_body_is_never_read_aloud(self):
        voice = FakeVoice()
        approve = hv._make_approver(voice, lambda: ("yes", "complete"))
        approve(self._request(), None)
        assert voice.phrases  # something was spoken
        assert all("TOPSECRETBODY" not in p for p in voice.phrases)

    def test_an_unknown_capability_fallback_is_bounded_to_one_line(self):
        voice = FakeVoice()
        request = ApprovalRequest(
            capability="mystery_tool",
            arguments={"body": "X" * 500},
        )
        approve = hv._make_approver(voice, lambda: ("no", "complete"))
        approve(request, None)
        spoken = voice.phrases[0]
        assert "\n" not in spoken
        assert "X" * 200 not in spoken  # the dump was truncated


class TestHelpers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("yes", True),
            ("yeah", True),
            ("sure, go", True),
            ("no", False),
            ("don't", False),
            ("yes but no", False),
            ("", False),
            ("maybe later", False),
        ],
    )
    def test_is_affirmative_fails_closed(self, text, expected):
        assert hv._is_affirmative(text) is expected

    def test_chime_plays_and_removes_its_file(self):
        voice = FakeVoice()
        hv._chime(voice, "listen")
        assert len(voice.played) == 1
        assert not os.path.exists(voice.played[0])

    def test_unknown_chime_is_a_no_op(self):
        voice = FakeVoice()
        hv._chime(voice, "nonsense")
        assert voice.played == []


class TestServeSettings:
    def test_resident_mode_keeps_the_saved_ear_and_drops_the_big_two(self):
        # The resident promise against real settings: the wake choice
        # survives untouched (that is what --serve exists to host), and
        # only shell and browser are forced off.
        from stella.app import StellaSettings

        saved = StellaSettings(
            model="m",
            wake_word="on",
            wake_word_enabled=True,
            wake_models=("hey_stella.onnx",),
            wake_phrase="hey stella",
            shell_tools_enabled=True,
            browser_tools_enabled=True,
        )
        forced = hv.serve_settings(saved)
        assert forced.wake_word == "on"
        assert forced.wake_word_enabled is True
        assert forced.wake_models == saved.wake_models
        assert forced.wake_phrase == saved.wake_phrase
        assert forced.shell_tools_enabled is False
        assert forced.browser_tools_enabled is False


class TestResidentServer:
    def test_unconfigured_is_honest_and_exits_2(self, monkeypatch, capsys):
        monkeypatch.setattr(hv, "resolve_settings", lambda: None)
        assert hv.run_voice_server() == 2
        assert "not configured" in capsys.readouterr().err.lower()

    def test_missing_voice_caps_is_a_refusal_not_a_fake_success(
        self, monkeypatch
    ):
        voice = FakeVoice(input_ok=False)
        application, _ = _wire(monkeypatch, voice, lambda: FakeEar())
        assert hv.run_voice_server() == 3
        assert application.closed is True

    def test_a_missing_endpointer_is_4_and_releases_the_socket(
        self, monkeypatch, capsys
    ):
        voice = FakeVoice()
        servers = []
        _wire_serve(
            monkeypatch,
            voice,
            ear_factory=_raising("no vad model"),
            server_factory=_server_box(servers),
        )
        assert hv.run_voice_server() == 4
        assert "no vad model" in capsys.readouterr().err
        assert servers[0].closed is True

    def test_a_second_server_exits_instead_of_hijacking(
        self, monkeypatch, capsys
    ):
        voice = FakeVoice()

        def live_server(path, on_command):
            raise OSError(f"a live server already holds {path}")

        _wire_serve(monkeypatch, voice, server_factory=live_server)
        assert hv.run_voice_server() == 5
        assert "already running" in capsys.readouterr().err.lower()

    def test_missing_wake_models_leaves_the_button_working(
        self, monkeypatch, capsys
    ):
        # A spotter that cannot load is said once and the resident
        # process keeps its button-opened conversations: honest, not a
        # dead key.
        voice = FakeVoice()
        servers, loops = [], []

        def no_spotter(settings, tap):
            raise VoiceError("Wake word found no model at /nowhere.onnx.")

        application, _session = _wire(
            monkeypatch, voice, lambda: FakeEar()
        )
        monkeypatch.setattr(hv, "control_path", lambda: "/tmp/stella.sock")
        monkeypatch.setattr(hv, "_ControlServer", _server_box(servers))
        monkeypatch.setattr(hv, "_conversation_loop", lambda *a: loops.append(a))
        monkeypatch.setattr(hv, "build_wake", no_spotter)
        assert hv.run_voice_server() == 0
        assert loops[0][3] is None
        assert "Wake word" in capsys.readouterr().err
        assert application.closed is True

    def test_serve_opens_one_tap_arms_the_saved_wake_and_closes_the_socket(
        self, monkeypatch
    ):
        voice = FakeVoice()
        wake = FakeWake()
        servers, loops = [], []
        _wire_serve(
            monkeypatch,
            voice,
            wake=wake,
            server_factory=_server_box(servers),
            loop_recorder=loops,
        )
        assert hv.run_voice_server() == 0
        # One microphone handle for recorder, watcher and spotter alike.
        assert len(voice.taps) == 1
        _application, _voice_arg, _capture, wake_arg = loops[0][:4]
        opening, ending, shutdown, active = loops[0][4:]
        assert wake_arg is wake
        assert servers[0].closed is True
        assert voice.prewarmed == 1
        # The ear's only power is to ring the opening bell — it starts
        # no turn and touches nothing else.
        wake.on_wake()
        assert opening.is_set()
        assert not ending.is_set() and not shutdown.is_set()
        assert not active.is_set()

    def test_the_control_handler_only_moves_events(self, monkeypatch):
        voice = FakeVoice()
        servers, loops = [], []
        _wire_serve(
            monkeypatch,
            voice,
            server_factory=_server_box(servers),
            loop_recorder=loops,
        )
        assert hv.run_voice_server() == 0
        handle = servers[0].on_command
        _app, _voice, _capture, _wake, opening, ending, shutdown, active = (
            loops[0]
        )
        assert handle("nonsense") == "unknown"
        assert handle("rm -rf / --no-preserve-root") == "unknown"
        assert handle("toggle") == "opening"
        assert opening.is_set()
        opening.clear()
        active.set()  # a conversation owns the microphone now
        assert handle("toggle") == "closing"
        assert ending.is_set()
        assert not opening.is_set()  # closing never re-opens on the sly
        ending.clear()
        assert handle("stop") == "stopping"
        assert shutdown.is_set()


class TestConversationLoop:
    def test_the_loop_chains_turns_and_rearms_the_ear(self):
        voice = FakeVoice()
        session = FakeSession(FakeOutcome())
        application = FakeApplication(voice, session)
        wake = FakeWake(fire_on_rearm=True)
        opening = threading.Event()
        ending = threading.Event()
        shutdown = threading.Event()
        active = threading.Event()
        wake.on_wake = opening.set
        transcripts = [
            ("first", "complete"),
            ("", "timeout"),  # silence closes session one
            ("second", "complete"),
            ("", "timeout"),  # and session two
        ]
        calls = {"n": 0}

        def capture():
            calls["n"] += 1
            if calls["n"] == len(transcripts):
                shutdown.set()
            return transcripts[calls["n"] - 1]

        opening.set()  # the wake ear (or a --toggle) opens the first chat
        hv._conversation_loop(
            application, voice, capture, wake, opening, ending, shutdown, active
        )
        # Two sessions, both chaining turn after turn with no re-press,
        # both ending on honest silence.
        assert session.turns == [("first", True), ("second", True)]
        assert voice.results == ["RESULT", "RESULT"]
        # The spotter is armed while idle, suspended for the talk, and
        # left stopped at exit — it never hears Stella's own voice.
        assert wake.starts == 2
        assert wake.stops == 3
        assert not wake.running
        assert not active.is_set()

    def test_spoken_stop_ends_the_conversation_without_a_turn(self):
        voice = FakeVoice()
        session = FakeSession(FakeOutcome())
        application = FakeApplication(voice, session)
        hv._run_session(
            application,
            voice,
            lambda: ("never mind", "complete"),
            threading.Event(),
            threading.Event(),
        )
        assert session.turns == []
        assert any("done" in p.lower() for p in voice.phrases)

    def test_silence_closes_a_session_without_saying_or_running_anything(
        self,
    ):
        voice = FakeVoice()
        session = FakeSession(FakeOutcome())
        application = FakeApplication(voice, session)
        hv._run_session(
            application,
            voice,
            lambda: ("", "timeout"),
            threading.Event(),
            threading.Event(),
        )
        assert session.turns == []
        assert voice.phrases == []
        # The spoken fail-closed approver is installed even if nothing
        # ever asked it — conversation mode is a weaker trigger, never
        # a weaker gate.
        assert application.session.stella.approval_provider is not None

    def test_a_turn_error_is_spoken_and_the_conversation_survives(self):
        voice = FakeVoice()
        session = FakeSession(FakeOutcome(result=None, error_message="boom"))
        application = FakeApplication(voice, session)
        transcripts = [("do it", "complete"), ("", "timeout")]
        calls = {"n": 0}

        def capture():
            item = transcripts[min(calls["n"], len(transcripts) - 1)]
            calls["n"] += 1
            return item

        hv._run_session(
            application, voice, capture, threading.Event(), threading.Event()
        )
        assert session.turns == [("do it", True)]
        assert voice.results == []
        assert any("couldn't" in p.lower() for p in voice.phrases)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("stop", True),
            ("Stop!", True),
            ("that's it", True),
            ("never mind", True),
            ("NeverMind", True),
            ("goodbye", True),
            ("stop the timer", False),
            ("quit the app", False),
            ("please stop", False),
            ("what time is it", False),
        ],
    )
    def test_only_a_whole_dismissal_dismisses(self, text, expected):
        assert hv._is_stop_phrase(text) is expected


class TestControlSocket:
    def test_the_doorbell_carries_exactly_one_word(self, monkeypatch):
        with tempfile.TemporaryDirectory() as scratch:
            path = os.path.join(scratch, "v.sock")
            monkeypatch.setattr(hv, "control_path", lambda: path)
            seen = []
            server = hv._ControlServer(
                path, lambda command: (seen.append(command), "ok")[1]
            )
            try:
                assert hv._send_command("toggle") == "ok"
                assert seen == ["toggle"]
                assert os.stat(path).st_mode & 0o777 == 0o600
                # A live socket belongs to another server and is
                # reported, never hijacked.
                with pytest.raises(OSError):
                    hv._ControlServer(path, lambda command: command)
            finally:
                server.close()
            assert hv._send_command("toggle") is None

    def test_a_stale_socket_file_is_cleared_not_defended(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = os.path.join(scratch, "v.sock")
            with open(path, "w", encoding="utf-8") as stale:
                stale.write("left behind by a crashed run")
            server = hv._ControlServer(path, lambda command: command)
            server.close()
            assert not os.path.exists(path)


class TestToggleClient:
    def test_a_live_server_takes_the_press_directly(self, monkeypatch):
        answers = ["closing"]
        spawns = []
        monkeypatch.setattr(
            hv, "_send_command", lambda command, timeout=2.0: answers.pop(0)
        )
        monkeypatch.setattr(
            hv, "_spawn_server", lambda: spawns.append(1) or True
        )
        assert hv.run_voice_toggle() == 0
        assert spawns == []

    def test_a_missing_server_is_started_then_pressed_once(
        self, monkeypatch
    ):
        answers = [None, "opening"]
        spawns = []
        monkeypatch.setattr(
            hv, "_send_command", lambda command, timeout=2.0: answers.pop(0)
        )
        monkeypatch.setattr(
            hv, "_spawn_server", lambda: spawns.append(1) or True
        )
        monkeypatch.setattr(hv, "_wait_for_server", lambda timeout: True)
        assert hv.run_voice_toggle() == 0
        assert len(spawns) == 1

    def test_a_server_that_never_came_up_is_said_out_loud(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            hv, "_send_command", lambda command, timeout=2.0: None
        )
        monkeypatch.setattr(hv, "_spawn_server", lambda: True)
        monkeypatch.setattr(hv, "_wait_for_server", lambda timeout: False)
        assert hv.run_voice_toggle() == 4
        assert "did not come up" in capsys.readouterr().err

    def test_stop_without_a_server_is_honest_and_idle(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            hv, "_send_command", lambda command, timeout=2.0: None
        )
        assert hv.run_voice_stop() == 0
        assert "No Stella voice session server" in capsys.readouterr().err

    def test_stop_reaches_a_running_server(self, monkeypatch, capsys):
        monkeypatch.setattr(
            hv,
            "_send_command",
            lambda command, timeout=2.0: "stopping",
        )
        assert hv.run_voice_stop() == 0
        assert "stopping" in capsys.readouterr().out.lower()
