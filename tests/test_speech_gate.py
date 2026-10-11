"""The capture gate: what a finished recording looks like before the recogniser.

Every case here is driven by a fake VAD that scores frames in order, so the
arithmetic — which frames survive, when nothing survives, and what happens
when the gate itself cannot work — is proved without a model, a microphone
or PipeWire. The real Silero scores are a measurement, not a unit test
(``docs/VOICE.md`` records the one taken on this machine).
"""

from __future__ import annotations

import wave

import pytest

from stella.app import (
    StellaSettings,
    build_barge_in,
    build_capture_gate,
)
from stella.barge_in import FRAME_BYTES, PAD_FRAMES, SpeechGate
from stella.voice import VoiceError


def fake_detector(loaded: list[str]):
    """A Silero stand-in that only records the path it was handed.

    Every builder case below swaps it in for the real reader, so the wiring is
    proved without an ONNX file on disk, and one shared shape keeps the three
    cases about the same one question: which path, and how often.
    """

    class RecordingVad:
        def __init__(self, model_path: str) -> None:
            loaded.append(model_path)

        def features(self, frame: bytes) -> tuple[float, float]:
            return 0.0, 0.0

        def reset(self) -> None:
            return None

    return RecordingVad


def write_clip(path: str, frames: int, rate: int = 16000, channels: int = 1) -> None:
    with wave.open(path, "wb") as sink:
        sink.setnchannels(channels)
        sink.setsampwidth(2)
        sink.setframerate(rate)
        sink.writeframes(bytes(FRAME_BYTES * channels * frames))


def frame_count(path: str) -> int:
    with wave.open(path, "rb") as source:
        return source.getnframes() * source.getsampwidth() // FRAME_BYTES


def gate_for(probabilities: list[float], rms: float = 0.2, **kwargs) -> SpeechGate:
    """A gate over a VAD that scores one clip's frames in order."""

    calls: list[bytes] = []

    def features(frame: bytes) -> tuple[float, float]:
        calls.append(frame)
        return probabilities[len(calls) - 1], rms

    return SpeechGate(features=features, **kwargs)


def clip(tmp_path, frames: int) -> str:
    path = str(tmp_path / "capture.wav")
    write_clip(path, frames)
    return path


def test_a_clip_with_no_speech_in_it_is_handed_over_unchanged(tmp_path) -> None:
    # The clutter case measured on this machine: a recording that is all
    # music. The gate does not get to decide nobody spoke — that judgement
    # belongs to the junk filter after transcription — so the clip goes to
    # the recogniser exactly as it was recorded.
    path = clip(tmp_path, 100)
    gate = gate_for([0.01] * 100)

    assert gate.trim(path) == path
    assert not (tmp_path / "capture.gated.wav").exists()


def test_the_room_outside_the_words_is_cut(tmp_path) -> None:
    # Twenty frames of speech in a hundred-frame clip, with the pad kept on
    # each side: everything the room played before the first word and after
    # the last one stops reaching the decoder.
    path = clip(tmp_path, 100)
    probabilities = [0.0] * 30 + [0.9] * 20 + [0.0] * 50
    gate = gate_for(probabilities)

    gated = gate.trim(path)

    assert gated != path
    # Twenty frames of speech, the pad on each side, nothing else.
    assert frame_count(gated) == 20 + 2 * PAD_FRAMES
    # The recording itself is never rewritten; both files live until the
    # recorder's temporary directory is removed.
    assert frame_count(path) == 100


def test_speech_that_reaches_the_edges_keeps_the_whole_clip(tmp_path) -> None:
    path = clip(tmp_path, 40)
    gate = gate_for([0.9] * 40)

    assert gate.trim(path) == path


def test_speech_that_runs_off_the_end_of_the_clip_is_not_cut_short(
    tmp_path,
) -> None:
    # A capture stopped mid-sentence is the owner's own button press, not a
    # gate decision: the kept span ends at the clip, not before it.
    path = clip(tmp_path, 30)
    probabilities = [0.0] * 10 + [0.9] * 20
    gate = gate_for(probabilities)

    gated = gate.trim(path)

    assert frame_count(gated) == 30 - (10 - PAD_FRAMES)


@pytest.mark.parametrize(
    "voiced_frames",
    [1, 2, 3, 4],
    ids=["one beat", "two", "three", "four"],
)
def test_a_transient_shorter_than_the_confirmation_window_anchors_nothing(
    tmp_path, voiced_frames: int
) -> None:
    # The same budget that stops a cough from interrupting Stella: a drum
    # hit the VAD misreads is not speech, so it cannot be the reason the
    # music in front of it is kept.
    path = clip(tmp_path, 40)
    probabilities = [0.0] * 20 + [0.9] * voiced_frames + [0.0] * (20 - voiced_frames)
    gate = gate_for(probabilities)

    assert gate.trim(path) == path


def test_a_loud_clip_is_not_speech(tmp_path) -> None:
    # The energy floor is the second half of the house rule. Music measured
    # RMS 0.13-0.28 on this machine, so loudness alone proves nothing: a
    # frame only counts when the model says speech *and* there is energy.
    path = clip(tmp_path, 40)
    gate = gate_for([0.9] * 40, rms=0.001)

    assert gate.trim(path) == path


def test_audio_that_is_not_a_stella_capture_is_left_alone(tmp_path) -> None:
    # The frame arithmetic is only valid for the 16 kHz mono 16-bit shape
    # every recorder here writes. Anything else is somebody else's audio and
    # comes back untouched.
    wrong_rate = str(tmp_path / "wrong-rate.wav")
    write_clip(wrong_rate, 40, rate=48000)
    stereo = str(tmp_path / "stereo.wav")
    write_clip(stereo, 40, channels=2)
    gate = gate_for([0.9] * 40)

    assert gate.trim(wrong_rate) == wrong_rate
    assert gate.trim(stereo) == stereo


def test_a_file_that_is_not_audio_is_left_alone(tmp_path) -> None:
    missing = str(tmp_path / "never-recorded.wav")
    not_audio = tmp_path / "capture.wav"
    not_audio.write_bytes(b"not a wave file at all")
    gate = gate_for([0.9] * 4)

    assert gate.trim(missing) == missing
    assert gate.trim(str(not_audio)) == str(not_audio)


def test_a_vad_that_raises_costs_the_turn_nothing(tmp_path) -> None:
    # The gate is a filter on a path that must keep working. A model that
    # dies mid-clip leaves the recording exactly as it was recorded.
    path = clip(tmp_path, 20)

    def explode(frame: bytes) -> tuple[float, float]:
        raise RuntimeError("the model stopped answering")

    gate = SpeechGate(features=explode)

    assert gate.trim(path) == path


def test_a_gate_that_cannot_write_hands_over_the_original(tmp_path) -> None:
    # The sibling file name is taken by a directory, so the write fails and
    # the whole clip still reaches the recogniser.
    path = clip(tmp_path, 60)
    (tmp_path / "capture.gated.wav").mkdir()
    gate = gate_for([0.0] * 20 + [0.9] * 20 + [0.0] * 20)

    assert gate.trim(path) == path


def test_every_clip_starts_the_model_from_silence(tmp_path) -> None:
    # One gate instance serves every recording in the session, and the VAD
    # carries context between frames. Without the reset the second clip
    # would be scored with the first one's tail in it.
    path = clip(tmp_path, 60)
    resets: list[int] = []
    gate = gate_for([0.0] * 30 + [0.9] * 20 + [0.0] * 10, reset=lambda: None)
    gate.reset = lambda: resets.append(1)

    gate.trim(path)
    gate.trim(path)

    assert resets == [1, 1]


def test_one_frame_is_scored_once(tmp_path) -> None:
    path = clip(tmp_path, 60)
    probabilities = [0.0] * 30 + [0.9] * 20 + [0.0] * 10
    calls: list[int] = []

    def features(frame: bytes) -> tuple[float, float]:
        index = len(calls)
        calls.append(index)
        return probabilities[index], 0.2

    SpeechGate(features=features).trim(path)

    # Exactly one pass over the clip's frames: the closing pass that ends a
    # run still in progress at the last frame is not another read of the
    # model, and a second scoring of any frame would double the cost.
    assert len(calls) == 60


def test_the_pad_and_the_confirmation_window_are_settings_of_the_gate(
    tmp_path,
) -> None:
    # The shipped answers are the house ones; a tighter pad and a longer
    # window are still arithmetically consistent.
    path = clip(tmp_path, 60)
    probabilities = [0.0] * 20 + [0.9] * 10 + [0.0] * 30
    gate = gate_for(probabilities, pad_frames=2, confirm_frames=3)

    gated = gate.trim(path)

    assert frame_count(gated) == 10 + 2 * 2


# --------------------------------------------------------------- the builder
#
# The gate is assembled from what the owner saved, which is the only reason
# these exist: a switch that cannot be read from config.json is a switch the
# owner does not have, and a model path she cannot write down is a gate that
# silently never engages.


def test_the_gate_is_not_built_when_the_owner_switched_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[str] = []
    monkeypatch.setattr("stella.app.SileroVad", fake_detector(loaded))

    settings = StellaSettings(model="m", voice_capture_gate=False)

    assert build_capture_gate(settings) is None
    # Off means the model is never opened at all, so a machine that has no
    # detector on disk still pays nothing for a gate that was declined.
    assert loaded == []


def test_the_gate_reads_the_saved_detector_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[str] = []
    monkeypatch.setattr("stella.app.SileroVad", fake_detector(loaded))

    gate = build_capture_gate(
        StellaSettings(model="m", vad_model="/elsewhere/silero_vad.onnx")
    )

    assert loaded == ["/elsewhere/silero_vad.onnx"]
    assert gate is not None
    # One model, one state: the gate's scorer and its reset are the same
    # reader, or a second clip would be judged by a VAD mid-sentence.
    assert gate.features.__self__ is gate.reset.__self__


def test_a_missing_detector_costs_the_gate_nothing_but_costs_the_ear_its_ear(
    tmp_path,
) -> None:
    # The doctrine difference in one pair of lines: an armed ear the owner
    # asked for says so when its model is absent, while a filter on a path
    # that used to work stops working quietly rather than taking her voice
    # input away with it.
    absent = str(tmp_path / "absent.onnx")
    gate_settings = StellaSettings(model="m", vad_model=absent)

    assert build_capture_gate(gate_settings) is None

    with pytest.raises(VoiceError):
        build_barge_in(
            StellaSettings(model="m", voice_barge_in="on", vad_model=absent)
        )
