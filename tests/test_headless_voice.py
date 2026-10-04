"""Tests for `stella voice`: the headless one-shot turn.

The whole point of these tests is that the orchestration is honest and
fail-closed without a microphone, a VAD model, or a real Brain: every
peripheral is a fake, and the assertions are about what the code refuses
to do (invent a transcript, run a turn on silence, speak a file body,
approve an unclear answer) as much as what it does.
"""

import os
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
