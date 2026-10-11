"""Barge-in: the ear Stella keeps open while she is speaking (Stage B7).

Design facts settled by measurement, not guessed (research reports 01
and 08): Tier-1 cooperative cancel already exists, so a barge-in
detector is *just another button* — its entire authority is one
callback into ``StellaBridge.cancel_current_turn``, which silences
playback and abandons the turn at its next safe point. It decides
nothing itself. While Stella speaks, this module streams raw 16 kHz
mono capture through Silero VAD v6 (one small ONNX file under
onnxruntime — deliberately NOT the pip ``silero-vad`` package, which
drags CUDA torch in with it, and NOT the model's own runtime) and fires
on a few consecutive voiced frames. Research measured ~30 ms of CPU
per 1000 frames: the listener is a rounding error next to the brain.

Echo is the acoustic prerequisite, not a code problem: on built-in
speakers the PipeWire ``module-echo-cancel`` pair (``ec_out``/
``ec_mic``) must be loaded and Stella must capture from ``ec_mic``
(measured: −29.6 dB, own voice fully cancelled; without it a VAD fires
on Stella's own voice ~23% of frames). Over a Bluetooth headset the
headset itself suppresses self-echo ~40 dB and the raw path is fine.
Setup and the device recipe live in ``docs/UI.md``.

Every failure here degrades barge-in only: a missing model, a missing
extra, or a dead capture process silences the ear, never the voice
path or a running turn (the voice-degradation precedent).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass, field

from stella.capture_target import resolve_capture_source
from stella.childproc import guarded_popen
from stella.mic_tap import CaptureCommand, capture_argv, read_frame
from stella.voice import (
    CAPTURE_CHANNELS,
    CAPTURE_WIDTH,
    VoiceError,
    _cancel_process_tree,
)

__all__ = [
    "BargeInListener",
    "SileroVad",
    "SpeechGate",
    "capture_command",
    "live_capture_command",
]

SAMPLE_RATE = 16_000
WINDOW_SAMPLES = 512  # one 32 ms VAD frame, 1024 bytes of s16le
CONTEXT_SAMPLES = 128  # the v6 ONNX conv context outside each chunk
FRAME_BYTES = WINDOW_SAMPLES * 2

#: Confirmation window: 5 consecutive voiced frames = 160 ms of
#: sustained speech, long enough that a cough or plosive never
#: interrupts (research report 08's frame budget).
CONFIRM_FRAMES = 5
#: Speech probability at or above which one frame counts as voiced
#: (the threshold every measurement on this machine scored with).
VAD_THRESHOLD = 0.5
#: RMS floor for a frame to count at all: post-echo-cancel playback
#: residue measured 0.0105-0.0122, so the gate sits at the residue
#: floor and only real near-end speech can clear it.
ENERGY_FLOOR = 0.01
#: Edge kept on each side of a gated capture: 320 ms, long enough to hold
#: a soft word onset and the breath before one, short enough that the
#: music Stella's own microphone hears is not all carried into the
#: recogniser. The wake endpointer waits 800 ms before deciding an
#: utterance ended; this is the same clock read the other way.
PAD_FRAMES = 10


def capture_command(source: str | None) -> list[str]:
    """The raw-capture argv: headerless s16le mono on stdout.

    ``pw-record -a`` is the PipeWire path and the only one that can
    target ``ec_mic`` (the echo-cancelled source lives above ALSA);
    ``arecord`` is the plain-ALSA fallback. Both stream to ``-``.
    """

    if shutil.which("pw-record"):
        argv = [
            "pw-record",
            "-a",
            "--rate",
            str(SAMPLE_RATE),
            "--channels",
            "1",
        ]
        if source:
            argv += ["--target", source]
        return argv + ["-"]
    argv = [
        "arecord",
        "-q",
        "-f",
        "S16_LE",
        "-r",
        str(SAMPLE_RATE),
        "-c",
        "1",
        "-t",
        "raw",
    ]
    if source:
        argv += ["-D", source]
    return argv + ["-"]


def live_capture_command(explicit: str | None) -> Callable[[], list[str]]:
    """A capture argv rebuilt from the microphones that exist *now*.

    Handing a listener the argv itself fixes the microphone for as long as
    the listener lives, and a listener lives as long as Stella does. The
    owner's headphones, a USB microphone and PipeWire's remembered default
    source all change mid-session, so every consumer that reopens the device
    builds the command here instead — see ``capture_target.py`` for why the
    unnamed capture is the one that degrades a Bluetooth headset's audio.
    """

    def build() -> list[str]:
        return capture_command(resolve_capture_source(explicit))

    return build


class SileroVad:
    """One 512-sample frame in, (speech probability, RMS) out.

    The calling convention below is the one validated on this machine
    in research report 08: the v6 ONNX keeps its conv context *outside*
    the chunk, so each run feeds the previous chunk's first 128 samples
    plus the current 512 — feeding bare 512-sample chunks silently
    returns ~0.0 for everything. Imports stay inside the constructor:
    without the ``barge-in`` extra installed, importing this module
    must keep working so barge-in can fail as a message, not a crash.
    """

    def __init__(self, model_path: str) -> None:
        if not os.path.isfile(model_path):
            raise VoiceError(
                f"Barge-in found no Silero VAD model at {model_path}. "
                "Place silero_vad.onnx there (or name the file in "
                "Settings, which is also what STELLA_VAD_MODEL says for "
                "one launch) and restart Stella."
            )
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as error:
            raise VoiceError(
                "Barge-in needs the 'barge-in' extra: run "
                "'uv sync --extra barge-in' and restart Stella."
            ) from error
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        options.log_severity_level = 4
        try:
            self._session = ort.InferenceSession(
                model_path,
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        except Exception as error:  # friendly text, never a trace
            detail = " ".join(str(error).split()) or type(error).__name__
            raise VoiceError(
                f"Barge-in could not load the VAD model ({detail[:120]})."
            ) from error
        self._np = np
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def reset(self) -> None:
        self._state = self._np.zeros((2, 1, 128), dtype=self._np.float32)
        self._context = self._np.zeros(CONTEXT_SAMPLES, dtype=self._np.float32)

    def features(self, frame: bytes) -> tuple[float, float]:
        """(speech probability, RMS) for one 1024-byte s16le mono frame."""

        if len(frame) != FRAME_BYTES:
            raise ValueError(
                f"a VAD frame must be exactly {FRAME_BYTES} bytes"
            )
        np = self._np
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        samples /= 32768.0
        rms = float(np.sqrt(np.mean(samples * samples)))
        x = np.concatenate([self._context, samples])
        out, self._state = self._session.run(
            None,
            {
                "input": x[None, :],
                "sr": np.array(SAMPLE_RATE, dtype=np.int64),
                "state": self._state,
            },
        )
        self._context = samples[:CONTEXT_SAMPLES]
        return float(out[0][0]), rms


@dataclass
class BargeInJudge:
    """Pure confirmation logic: sustained voiced frames interrupt, once.

    Fires exactly once per armed episode (latched until :meth:`reset`),
    so one interruption is one cancel, however long the user talks.
    """

    threshold: float = VAD_THRESHOLD
    confirm_frames: int = CONFIRM_FRAMES
    energy_floor: float = ENERGY_FLOOR
    _streak: int = field(default=0, init=False)
    _fired: bool = field(default=False, init=False)

    def feed(self, probability: float, rms: float) -> bool:
        voiced = probability >= self.threshold and rms >= self.energy_floor
        self._streak = self._streak + 1 if voiced else 0
        if self._fired or self._streak < self.confirm_frames:
            return False
        self._fired = True
        return True

    def reset(self) -> None:
        self._streak = 0
        self._fired = False


class BargeInListener:
    """Continuous capture -> VAD -> one callback, on its own daemon thread.

    :meth:`start` and :meth:`stop` bracket one speaking episode; the
    judge re-arms at every start. If the VAD itself raises mid-flight
    the ear retires permanently (``failed``) without touching anything
    else — following the router-degradation precedent: a broken
    listener disables only listening.
    """

    def __init__(
        self,
        *,
        features: Callable[[bytes], tuple[float, float]],
        command: CaptureCommand | None = None,
        threshold: float = VAD_THRESHOLD,
        confirm_frames: int = CONFIRM_FRAMES,
        on_speech: Callable[[], None] | None = None,
    ) -> None:
        self._features = features
        if command is None:
            command = live_capture_command(None)
        self._command = command
        self._judge = BargeInJudge(
            threshold=threshold, confirm_frames=confirm_frames
        )
        self.on_speech = on_speech
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.failed = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Arm the ear for one speaking episode. Idempotent."""

        if self.failed:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._judge.reset()
            try:
                process = guarded_popen(
                    capture_argv(self._command),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as error:
                raise VoiceError(
                    f"Barge-in could not start listening ({error}). "
                    "Voice input and output are unaffected."
                ) from error
            self._process = process
            self._thread = threading.Thread(
                target=self._run,
                args=(process.stdout,),
                name="stella-barge-in",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Disarm the ear: kill only this capture process, join the thread."""

        with self._lock:
            process, thread = self._process, self._thread
            self._process = self._thread = None
        if process is not None and process.poll() is None:
            _cancel_process_tree(process)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _run(self, stream) -> None:
        if stream is None:  # pragma: no cover - Popen always gives a pipe
            return
        try:
            while True:
                frame = self._read_frame(stream)
                if frame is None:
                    return  # capture ended (stopped, or died on its own)
                try:
                    probability, rms = self._features(frame)
                except Exception:  # noqa: BLE001 - a broken ear retires
                    self.failed = True
                    return
                if self._judge.feed(probability, rms) and self.on_speech is not None:
                    self.on_speech()
        finally:
            try:
                stream.close()
            except OSError as error:
                del error

    @staticmethod
    def _read_frame(stream) -> bytes | None:
        # One copy of the short-read loop lives in the tap module now: a
        # pipe from a capture and a tap subscriber read identically.
        return read_frame(stream, FRAME_BYTES)


@dataclass
class SpeechGate:
    """Cut the not-speech off the edges of one finished capture.

    The measurement behind it (this machine, 2026-10-11): five seconds of
    room with music playing through the headset scored RMS 0.13-0.28 — a
    hundred percent of frames above the energy floor, so no silence trim
    can find it — while the same frames scored a speech probability of
    0.000 to 0.046. Loud is not speech, and the recogniser cannot tell
    that: a whisper-family decoder handed music with no words in it
    invents them. What it can be saved from is the music that plays
    *around* the words, which is what a push-to-talk capture collects
    between the button press and the button release.

    So this trims edges and nothing else. It keeps every frame between
    the first and the last sustained speech (measured with the same
    frames, the same threshold and the same confirmation window the barge
    ear uses), plus :data:`PAD_FRAMES` at each side, and it writes a
    second file beside the recording rather than touching it. When the
    clip holds no sustained speech at all it hands back the original path
    unchanged: a gate that decides nobody spoke is a worse failure than
    a transcript of noise, and that judgement already belongs to
    ``is_transcription_junk``.

    Every error returns the original path. A gate that cannot read the
    file, cannot score it, or cannot write one leaves the turn exactly as
    it would have been without this class.
    """

    features: Callable[[bytes], tuple[float, float]]
    reset: Callable[[], None] | None = None
    threshold: float = VAD_THRESHOLD
    energy_floor: float = ENERGY_FLOOR
    confirm_frames: int = CONFIRM_FRAMES
    pad_frames: int = PAD_FRAMES

    def trim(self, path: str) -> str:
        pcm = self._read(path)
        if pcm is None:
            return path
        count = len(pcm) // FRAME_BYTES
        try:
            if self.reset is not None:
                self.reset()
            span = self._speech_span(pcm[: count * FRAME_BYTES])
        except Exception:  # noqa: BLE001 - a gate that fails says nothing
            return path
        if span is None:
            return path
        start, end = span
        if start == 0 and end == count:
            return path
        return self._write(path, pcm[start * FRAME_BYTES : end * FRAME_BYTES])

    def _speech_span(self, pcm: bytes) -> tuple[int, int] | None:
        """(first, end) frames of the speech span, or None if there is none.

        A frame only counts as speech inside a run of at least
        ``confirm_frames`` voiced frames, so one drum beat the VAD
        misreads cannot anchor the keep and drag the music in front of it
        along with it — the same budget that stops a cough from
        interrupting Stella mid-sentence.
        """

        count = len(pcm) // FRAME_BYTES
        first = -1
        last = -1
        run_start = -1
        # One extra pass over a frame that cannot exist, so a capture that
        # ends mid-speech still closes its run instead of losing the end.
        for index in range(count + 1):
            voiced = index < count and self._is_speech(
                pcm[index * FRAME_BYTES : (index + 1) * FRAME_BYTES]
            )
            if voiced:
                if run_start < 0:
                    run_start = index
                continue
            if run_start >= 0 and index - run_start >= self.confirm_frames:
                if first < 0:
                    first = run_start
                last = index - 1
            run_start = -1
        if first < 0:
            return None
        return (
            max(0, first - self.pad_frames),
            min(count, last + 1 + self.pad_frames),
        )

    def _is_speech(self, frame: bytes) -> bool:
        probability, rms = self.features(frame)
        return probability >= self.threshold and rms >= self.energy_floor

    def _read(self, path: str) -> bytes | None:
        """The capture's PCM, or None if it is not a clip to gate.

        Shape is checked, not assumed: the frame arithmetic is only valid
        for the 16 kHz mono 16-bit stream every Stella capture writes, and
        audio from anywhere else is left exactly as it was.
        """

        try:
            with wave.open(path, "rb") as source:
                if (
                    source.getframerate() != SAMPLE_RATE
                    or source.getnchannels() != CAPTURE_CHANNELS
                    or source.getsampwidth() != CAPTURE_WIDTH
                ):
                    return None
                return source.readframes(source.getnframes())
        except (OSError, ValueError, wave.Error):
            return None

    def _write(self, path: str, pcm: bytes) -> str:
        """One gated sibling file, or the original when it cannot be made."""

        gated = os.path.join(os.path.dirname(path), "capture.gated.wav")
        try:
            with wave.open(gated, "wb") as sink:
                sink.setnchannels(CAPTURE_CHANNELS)
                sink.setsampwidth(CAPTURE_WIDTH)
                sink.setframerate(SAMPLE_RATE)
                sink.writeframes(pcm)
        except (OSError, ValueError, wave.Error):
            return path
        return gated
