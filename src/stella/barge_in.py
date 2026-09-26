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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from stella.voice import VoiceError, _cancel_process_tree

__all__ = [
    "BargeInListener",
    "SileroVad",
    "capture_command",
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
                "Place silero_vad.onnx there (or point STELLA_VAD_MODEL "
                "at it) and restart Stella."
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
        command: Sequence[str] | None = None,
        threshold: float = VAD_THRESHOLD,
        confirm_frames: int = CONFIRM_FRAMES,
        on_speech: Callable[[], None] | None = None,
    ) -> None:
        self._features = features
        self._command = list(command) if command is not None else capture_command(None)
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
                process = subprocess.Popen(
                    self._command,
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
        data = b""
        while len(data) < FRAME_BYTES:
            try:
                part = stream.read(FRAME_BYTES - len(data))
            except OSError:
                return None
            if not part:
                return None  # EOF
            data += part
        return data
