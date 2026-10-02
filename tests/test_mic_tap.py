"""Tests for the shared microphone tap (Stage 2).

Every capture here is a bounded ``python -c`` writer: no test below
opens a real device, plays a real frame into a detector, or leaves a
process behind.
"""

import sys
import time

import pytest

from stella.barge_in import capture_command
from stella.mic_tap import FRAME_BYTES, MicTap, TapClient, read_frame
from stella.voice import VoiceError


def writer(frames: int, *, keep_open: float = 0.0) -> list[str]:
    """A fake capture: ``frames`` identifiable frames, then exit or wait.

    Frame ``n`` is ``FRAME_BYTES`` copies of byte ``n``, so a test can
    tell which frame it is holding instead of counting anonymous zeros.
    """

    body = (
        "import sys, time\n"
        f"payload = b''.join("
        f"bytes([n % 256]) * {FRAME_BYTES} for n in range({frames}))\n"
        "sys.stdout.buffer.write(payload)\n"
        "sys.stdout.buffer.flush()\n"
        + (f"time.sleep({keep_open})\n" if keep_open else "")
    )
    return [sys.executable, "-c", body]


def marker(frame: bytes) -> int:
    return frame[0]


def wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("condition never held")


def take(client: TapClient, count: int, timeout: float = 5.0) -> list[bytes]:
    frames: list[bytes] = []
    deadline = time.monotonic() + timeout
    while len(frames) < count and time.monotonic() < deadline:
        frame = client.poll()
        if frame is None:
            break
        frames.append(frame)
    return frames


# ------------------------------------------------------------------ reader


def test_read_frame_returns_whole_frames_and_stops_at_eof() -> None:
    class Stream:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self, size: int) -> bytes:
            part, self._data = self._data[:size], self._data[size:]
            return part

    stream = Stream(b"\x01" * (FRAME_BYTES + 3))
    assert read_frame(stream) == b"\x01" * FRAME_BYTES
    # A short tail is not a frame: it reads as the end of the capture.
    assert read_frame(stream) is None
    assert read_frame(Stream(b"")) is None


# ------------------------------------------------------------- laziness


def test_the_capture_opens_for_the_first_subscriber_only() -> None:
    tap = MicTap(command=writer(4, keep_open=30))
    try:
        assert not tap.running()
        first = tap.subscribe("first")
        assert tap.running()
        second = tap.subscribe("second")
        assert take(first, 4) == [bytes([n]) * FRAME_BYTES for n in range(4)]
        assert take(second, 4) == [bytes([n]) * FRAME_BYTES for n in range(4)]
    finally:
        tap.stop()
    assert not tap.running()


def test_the_capture_closes_when_the_last_subscriber_leaves() -> None:
    tap = MicTap(command=writer(2, keep_open=30))
    first = tap.subscribe("first")
    second = tap.subscribe("second")
    first.close()
    assert tap.running()  # one consumer left: the mic stays open
    second.close()
    wait_until(lambda: not tap.running())
    assert first.poll() is None and second.poll() is None
    # Leaving is idempotent, and a re-subscribe reopens the capture.
    second.close()
    third = tap.subscribe("third")
    assert tap.running()
    assert marker(third.poll() or b"\xff") == 0
    tap.stop()


# ---------------------------------------------------------------- fan-out


def test_a_slow_consumer_drops_frames_and_never_stalls_the_reader() -> None:
    tap = MicTap(command=writer(6, keep_open=30))
    try:
        fast = tap.subscribe("fast", backlog=8)
        slow = tap.subscribe("slow", backlog=2)  # never polls during the burst
        # The reader handed all six frames onward despite the wedged
        # subscriber: a slow ear cannot cost the microphone to a fast one.
        assert [marker(f) for f in take(fast, 6)] == list(range(6))
        # The backlogged queue kept the newest two and lost the oldest.
        assert [marker(slow.poll() or b"") for _ in range(2)] == [4, 5]
    finally:
        tap.stop()


def test_stop_hands_every_subscriber_one_terminal_notice() -> None:
    tap = MicTap(command=writer(2, keep_open=30))
    first = tap.subscribe("first")
    second = tap.subscribe("second")
    tap.stop()
    assert first.poll() is None
    assert second.poll() is None
    assert not tap.failed  # asked for, so not a fault
    assert first.poll() is None  # sticky: terminal stays terminal


def test_a_clean_stop_leaves_the_tap_able_to_open_again() -> None:
    tap = MicTap(command=writer(3, keep_open=30))
    tap.subscribe("first").close()
    wait_until(lambda: not tap.running())
    assert not tap.failed
    client = tap.subscribe("second")
    assert tap.running()
    assert marker(client.poll() or b"") == 0
    tap.stop()


# ---------------------------------------------------------- as a stream


def test_a_subscriber_reads_like_the_capture_pipe() -> None:
    # The listeners read whole frames from a pipe, so a subscriber that
    # answers read() the same way needs no second loop anywhere.
    tap = MicTap(command=writer(3, keep_open=30))
    half = FRAME_BYTES // 2
    try:
        client = tap.subscribe("reader")
        assert read_frame(client) == bytes([0]) * FRAME_BYTES
        assert client.read(half) == bytes([1]) * half
        assert client.read(FRAME_BYTES) == bytes([1]) * half
        assert client.read(FRAME_BYTES) == bytes([2]) * FRAME_BYTES
    finally:
        tap.stop()
    assert client.read() == b""  # a closed subscriber reads as EOF


# ----------------------------------------------------------------- faults


def test_a_capture_that_dies_reports_once_to_every_subscriber() -> None:
    tap = MicTap(command=writer(2))  # writes two frames, then exits
    first = tap.subscribe("first")
    second = tap.subscribe("second")
    wait_until(lambda: tap.failed)
    assert "capture ended" in (tap.error or "")
    assert first.poll() is None and second.poll() is None
    # One fault message carries the whole tap: late joiners hear it
    # instead of opening a capture that is already gone.
    with pytest.raises(VoiceError) as caught:
        tap.subscribe("latecomer")
    assert str(caught.value) == tap.error


# ------------------------------------------------------------------ argv


def test_the_tap_captures_raw_16k_mono_on_stdout() -> None:
    # The shared tap uses barge-in's argv verbatim: one device, one
    # format, one command every consumer already agrees on.
    assert MicTap()._command == capture_command(None)
    assert MicTap(command=["pw-record", "-"])._command == ["pw-record", "-"]
