"""Wake word: the ear Stella keeps open while she is *not* speaking.

Wake activation is barge-in's mirror image and inherits its doctrine.
The detector is *just another button*: its entire authority is one
callback that starts the ordinary push-to-talk capture, so a woken
utterance runs through the identical ``StellaSession.run_turn`` path —
same Brain, dispatcher, risk checks, approvals and audit. It decides
nothing, approves nothing, and never injects text. Nothing audio is
ever recorded to disk or stored here: the frames feed one local
voiced/not-voiced decision and one score in, one boolean out.

The spotter is a hand-ported slice of the openWakeWord pipeline
(Apache-2.0, https://github.com/dscripka/openWakeWord): the
melspectrogram → speech-embedding → keyword-classifier chain, executed
as three small ONNX files under onnxruntime — deliberately NOT the pip
``openwakeword`` package, whose ``tflite-runtime`` dependency has no
wheel for this Python and whose streaming state machine is the only
part worth keeping. This mirrors the Silero VAD precedent in
``barge_in.py``: the models are data the user places on disk, and the
repo stays a pure-Python package with one optional extra.

Echo safety follows the measured barge-in lesson (research reports 01
and 08): Stella's own voice must not wake Stella, so the application
suspends this ear whenever she speaks or is already capturing. The
echo-cancelled source (``ec_mic``) is still the recommended capture.

Every failure here degrades wake only: a missing extra, a missing model
file or a dead capture process reports one friendly line (or retires
the ear silently), and push-to-talk, text and speech all keep working.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from stella.barge_in import (
    SAMPLE_RATE,
    capture_command,
)
from stella.childproc import guarded_popen
from stella.voice import VoiceError, _cancel_process_tree

if TYPE_CHECKING:
    import numpy as np

__all__ = [
    "WakeEndpoint",
    "WakeListener",
    "WakeSpotter",
    "WakeUtteranceEar",
    "default_wake_model_dir",
]

# One capture frame is 512 samples (32 ms), matching barge-in's reader.
FRAME_BYTES = 512 * 2

#: The openWakeWord pipeline consumes audio in 80 ms chunks (1280
#: samples @ 16 kHz); the melspectrogram ONNX additionally reads 3
#: frames of left context (480 samples) beyond each chunk window.
CHUNK_SAMPLES = 1280
MEL_LEFT_CONTEXT = 480

#: Frame counts of the shared buffers (all ported values, not tuned):
#: 10 s of raw audio, the melspec buffer at 97 frames/s capped at 10 s,
#: and ~10 s of 96-dimensional embedding frames.
RAW_BUFFER_SECONDS = 10
MEL_FRAMES_PER_SECOND = 97
MEL_BUFFER_MAX = 10 * MEL_FRAMES_PER_SECOND
MEL_WINDOW = 76
MEL_STEP = 8
FEATURE_BUFFER_MAX = 120

#: Classifier scores at or above this fire the wake (per-model guidance
#: from the project; every shipped model scores near 0/1 for real hits).
WAKE_THRESHOLD = 0.5


def default_wake_model_dir() -> str:
    """Where the user places the openWakeWord ONNX files."""

    return os.path.join(
        os.path.expanduser("~"), "models", "openwakeword"
    )


#: The name that works out of the box; a trained "hey stella" (or any
#: openWakeWord-format classifier) is one STELLA_WAKE_MODEL away.
DEFAULT_WAKE_MODEL = "hey_jarvis_v0.1.onnx"


class WakeSpotter:
    """Continuous raw frames in, one wake boolean out.

    The streaming buffers replicate the upstream pipeline exactly, with
    one deliberate difference: the feature buffer starts empty instead
    of pre-seeded with four seconds of noise embeddings, so the
    detector simply cannot fire during its first ~1.3 s of audio.
    Imports live in the constructor: without the ``wake`` extra this
    module must keep importing so wake can fail as a message, not a
    crash.
    """

    def __init__(
        self,
        *,
        model_dir: str | None = None,
        model_name: str | None = None,
        threshold: float = WAKE_THRESHOLD,
    ) -> None:
        directory = model_dir if model_dir is not None else default_wake_model_dir()
        classifier = model_name or DEFAULT_WAKE_MODEL
        if not os.path.isabs(classifier):
            classifier = os.path.join(directory, classifier)
        melspec_path = os.path.join(directory, "melspectrogram.onnx")
        embedding_path = os.path.join(directory, "embedding_model.onnx")
        for path, label in (
            (melspec_path, "melspectrogram"),
            (embedding_path, "embedding"),
            (classifier, "wake word classifier"),
        ):
            if not os.path.isfile(path):
                raise VoiceError(
                    f"Wake word found no {label} model at {path}. "
                    "Place the openWakeWord ONNX files in "
                    f"{directory} (docs/VOICE.md has the one-line "
                    "download) and restart Stella."
                )
        if not 0.0 < threshold < 1.0:
            raise ValueError("the wake threshold must be between 0 and 1")
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as error:
            raise VoiceError(
                "The wake word ear needs the 'wake' extra: run "
                "'uv sync --extra wake' and restart Stella."
            ) from error
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        options.log_severity_level = 4

        def session(path: str) -> ort.InferenceSession:
            try:
                return ort.InferenceSession(
                    path,
                    sess_options=options,
                    providers=["CPUExecutionProvider"],
                )
            except Exception as error:  # friendly text, never a trace
                detail = " ".join(str(error).split()) or type(error).__name__
                raise VoiceError(
                    f"Wake word could not load {path} ({detail[:120]})."
                ) from error

        self._np = np
        self._melspec = session(melspec_path)
        self._embedding = session(embedding_path)
        self._classifier = session(classifier)
        self._melspec_input = self._melspec.get_inputs()[0].name
        self._embedding_input = self._embedding.get_inputs()[0].name
        self._classifier_input = self._classifier.get_inputs()[0].name
        wanted = self._classifier.get_inputs()[0].shape
        if not isinstance(wanted[1], int) or wanted[1] < 1:
            raise VoiceError(
                f"Wake word classifier {classifier} has an unknown "
                "feature-frame count; expected a fixed (1, N, 96) input."
            )
        self._feature_frames = wanted[1]
        self.threshold = threshold
        self._raw = deque(maxlen=RAW_BUFFER_SECONDS * SAMPLE_RATE)
        self._mel = np.ones((MEL_WINDOW, 32), dtype=np.float32)
        self._features = np.empty((0, 96), dtype=np.float32)
        self._accumulated = 0
        self._remainder = np.empty(0, dtype=np.int16)
        self._fired = False

    def reset(self) -> None:
        """Clear every buffer and re-arm the one-shot latch."""

        self._raw.clear()
        self._mel = self._np.ones((MEL_WINDOW, 32), dtype=self._np.float32)
        self._features = self._np.empty((0, 96), dtype=self._np.float32)
        self._accumulated = 0
        self._remainder = self._np.empty(0, dtype=self._np.int16)
        self._fired = False

    def feed(self, frame: bytes) -> bool:
        """One raw s16le mono frame in; True exactly once on a wake."""

        if self._fired:
            return False
        if len(frame) % 2:
            raise ValueError("a wake frame must be whole s16le samples")
        np = self._np
        samples = np.frombuffer(frame, dtype=np.int16)
        if samples.size == 0:
            return False

        if self._remainder.size:
            samples = np.concatenate((self._remainder, samples))
            self._remainder = np.empty(0, dtype=np.int16)
        if self._accumulated + samples.size >= CHUNK_SAMPLES:
            leftover = (self._accumulated + samples.size) % CHUNK_SAMPLES
            if leftover:
                self._raw.extend(samples[:-leftover].tolist())
                self._accumulated += samples.size - leftover
                self._remainder = samples[-leftover:]
            else:
                self._raw.extend(samples.tolist())
                self._accumulated += samples.size
        else:
            self._raw.extend(samples.tolist())
            self._accumulated += samples.size
            return False

        self._stream_melspectrogram(self._accumulated)
        for i in np.arange(self._accumulated // CHUNK_SAMPLES - 1, -1, -1):
            ndx = -MEL_STEP * i
            end = len(self._mel) if ndx == 0 else ndx
            window = self._mel[-MEL_WINDOW + end : end]
            if window.shape[0] == MEL_WINDOW:
                self._features = np.vstack(
                    (self._features, self._embed(window))
                )
        if self._features.shape[0] > FEATURE_BUFFER_MAX:
            self._features = self._features[-FEATURE_BUFFER_MAX:, :]
        self._accumulated = 0

        if self._features.shape[0] < self._feature_frames:
            return False
        batch = self._features[-self._feature_frames :, :][None, :].astype(
            np.float32
        )
        probability = float(
            self._classifier.run(None, {self._classifier_input: batch})[0][0][0]
        )
        if probability >= self.threshold:
            self._fired = True
            return True
        return False

    def _stream_melspectrogram(self, n_samples: int) -> None:
        np = self._np
        if len(self._raw) < 400:  # pragma: no cover - first chunk is 1280
            return
        window = np.array(
            list(self._raw)[-(n_samples + MEL_LEFT_CONTEXT) :], dtype=np.int16
        )
        spec = self._melspec.run(
            None,
            {self._melspec_input: window.astype(np.float32)[None, :]},
        )[0]
        spec = np.squeeze(spec) / 10.0 + 2.0
        self._mel = np.vstack((self._mel, spec.astype(np.float32)))
        if self._mel.shape[0] > MEL_BUFFER_MAX:
            self._mel = self._mel[-MEL_BUFFER_MAX:, :]

    def _embed(self, window: np.ndarray) -> np.ndarray:
        np = self._np
        out = self._embedding.run(
            None,
            {
                self._embedding_input: window.astype(np.float32)[
                    None, :, :, None
                ]
            },
        )[0]
        return np.squeeze(out).astype(np.float32)


@dataclass
class WakeEndpoint:
    """Pure hands-free capture state machine after a wake fires.

    ``feed(voiced, elapsed_seconds)`` returns ``"pending"`` until one
    terminal state: the user finished speaking (enough trailing
    silence), nothing was said at all (``"timeout"``), or the cap was
    reached (``"cap"``). Terminal is sticky so a late frame cannot
    re-open a finished capture.
    """

    confirm_frames: int = 3  # ~96 ms of speech starts the utterance
    silence_frames: int = 25  # ~800 ms of silence ends it
    speech_timeout: float = 8.0  # nothing said at all -> timeout
    max_capture: float = 20.0  # hard cap, however talkative
    _voiced_streak: int = field(default=0, init=False)
    _silent_streak: int = field(default=0, init=False)
    _started: bool = field(default=False, init=False)
    _done: str | None = field(default=None, init=False)

    def reset(self) -> None:
        self._voiced_streak = 0
        self._silent_streak = 0
        self._started = False
        self._done = None

    @property
    def finished(self) -> str | None:
        return self._done

    def feed(self, voiced: bool, elapsed: float) -> str:
        if self._done is not None:
            return self._done
        if elapsed >= self.max_capture:
            self._done = "cap"
            return self._done
        if voiced:
            self._voiced_streak += 1
            self._silent_streak = 0
            if self._voiced_streak >= self.confirm_frames:
                self._started = True
        else:
            self._voiced_streak = 0
            if self._started:
                self._silent_streak += 1
                if self._silent_streak >= self.silence_frames:
                    self._done = "complete"
                    return self._done
        if not self._started and elapsed >= self.speech_timeout:
            self._done = "timeout"
            return self._done
        return "pending"


class WakeListener:
    """Continuous capture -> spotter -> one wake callback, daemon thread.

    Unlike the barge-in ear (bracketed by one speaking episode), this
    ear is armed whenever Stella is idle and suspended by the
    application whenever she listens or speaks. After firing it latches
    until the next :meth:`start`, so one "hey jarvis" is at most one
    wake however long the phrase echoes. A faulting detector retires
    the ear permanently without touching anything else.
    """

    def __init__(
        self,
        *,
        feed: Callable[[bytes], bool],
        command: Sequence[str] | None = None,
        on_wake: Callable[[], None] | None = None,
        reset: Callable[[], None] | None = None,
    ) -> None:
        self._feed = feed
        self._command = (
            list(command) if command is not None else capture_command(None)
        )
        self.on_wake = on_wake
        self._reset = reset
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.failed = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Arm the ear. Idempotent; re-arms the one-shot latch."""

        if self.failed:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._reset is not None:
                self._reset()
            try:
                process = guarded_popen(
                    self._command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as error:
                raise VoiceError(
                    f"Wake word could not start listening ({error}). "
                    "Push-to-talk voice and everything else are unaffected."
                ) from error
            self._process = process
            self._thread = threading.Thread(
                target=self._run,
                args=(process.stdout,),
                name="stella-wake",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Suspend the ear: kill only this capture, join the thread."""

        with self._lock:
            process, thread = self._process, self._thread
            self._process = self._thread = None
        if process is not None and process.poll() is None:
            _cancel_process_tree(process)
        if thread is not None and thread.is_alive() and thread is not (
            threading.current_thread()
        ):
            thread.join(timeout=2)

    def _run(self, stream) -> None:
        if stream is None:  # pragma: no cover - Popen always gives a pipe
            return
        try:
            while True:
                frame = self._read_frame(stream)
                if frame is None:
                    return
                try:
                    fired = self._feed(frame)
                except Exception:  # noqa: BLE001 - a broken ear retires
                    self.failed = True
                    return
                if fired and self.on_wake is not None:
                    self.on_wake()
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


class WakeUtteranceEar:
    """The silence-watcher that endpoints one wake-initiated capture.

    A second, short-lived listener with exactly one authority: telling
    the bridge when the woken utterance has finished (or never
    arrived), which is precisely what the Stop and Cancel button
    presses already do. It records nothing: frames feed the local
    voiced/not-voiced decision and are dropped.
    """

    def __init__(
        self,
        *,
        features: Callable[[bytes], tuple[float, float]],
        endpoint: WakeEndpoint,
        command: Sequence[str] | None = None,
        threshold: float = 0.5,
        energy_floor: float = 0.01,
        clock: Callable[[], float] | None = None,
        on_finish: Callable[[str], None] | None = None,
    ) -> None:
        self._features = features
        self._endpoint = endpoint
        self._command = (
            list(command) if command is not None else capture_command(None)
        )
        self._threshold = threshold
        self._energy_floor = energy_floor
        self._clock = clock if clock is not None else _monotonic
        self.on_finish = on_finish
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._start_time = 0.0
        self.failed = False

    def start(self) -> None:
        if self.failed:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._endpoint.reset()
            self._start_time = self._clock()
            try:
                process = guarded_popen(
                    self._command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as error:
                raise VoiceError(
                    f"Wake word could not watch the utterance ({error}). "
                    "Push-to-talk voice and everything else are unaffected."
                ) from error
            self._process = process
            self._thread = threading.Thread(
                target=self._run,
                args=(process.stdout,),
                name="stella-wake-utterance",
                daemon=True,
            )
            self._thread.start()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        """Silence the watcher without reporting a finish."""

        with self._lock:
            process, thread = self._process, self._thread
            self._process = self._thread = None
        if process is not None and process.poll() is None:
            _cancel_process_tree(process)
        if thread is not None and thread.is_alive() and thread is not (
            threading.current_thread()
        ):
            thread.join(timeout=2)

    def _run(self, stream) -> None:
        if stream is None:  # pragma: no cover - Popen always gives a pipe
            return
        terminal: str | None = None
        try:
            while True:
                frame = WakeListener._read_frame(stream)
                if frame is None:
                    break  # capture ended (stopped, or died on its own)
                try:
                    probability, rms = self._features(frame)
                except Exception:  # noqa: BLE001 - a broken watcher retires
                    self.failed = True
                    return
                voiced = probability >= self._threshold and rms >= (
                    self._energy_floor
                )
                terminal = self._endpoint.feed(
                    voiced, self._clock() - self._start_time
                )
                if terminal != "pending":
                    break
        finally:
            try:
                stream.close()
            except OSError as error:
                del error
            # The watcher outlives nothing: tear down its own capture.
            with self._lock:
                process = self._process
                self._process = self._thread = None
            if process is not None and process.poll() is None:
                _cancel_process_tree(process)
            if terminal is not None and terminal != "pending" and (
                self.on_finish is not None
            ):
                self.on_finish(terminal)


def _monotonic() -> float:
    import time

    return time.monotonic()
