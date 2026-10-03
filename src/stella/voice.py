"""Local-first voice hardware and speech providers for the desktop UI.

This module supplies the physical edge of the voice interface: one-shot
microphone recording, audio playback, and concrete implementations of the
existing provider-neutral ``TranscriptionProvider`` and ``SpeechProvider``
boundaries. Nothing here decides, executes tools, touches memory, or grants
authority; a transcript produced here is ordinary untrusted user text and
enters Stella through the same application-layer session path as typed
input. Recordings are written to a private temporary directory and removed
as soon as transcription is done, so nothing is persisted by default.
"""

from __future__ import annotations

import array
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import wave
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from stella.audio import TranscriptionProvider
from stella.audio_output import (
    MAX_SPEECH_TEXT_CHARS,
    SpeechArtifact,
    SpeechOutput,
    SpeechProvider,
)
from stella.childproc import guarded_popen, recording_finalized_ok
from stella.context import InputModality, InputPart

if TYPE_CHECKING:
    from collections.abc import Callable

    # The tap imports VoiceError from here, so this direction stays a
    # typing-only edge: the recorder takes frames from a subscriber, it
    # never reaches back into capture management.
    from stella.mic_tap import MicTap, TapClient

__all__ = [
    "CommandSpeechProvider",
    "CommandTranscriptionProvider",
    "OpenAISpeechProvider",
    "OpenAITranscriptionProvider",
    "Player",
    "Recorder",
    "ResidentSpeechProvider",
    "SubprocessPlayer",
    "SubprocessRecorder",
    "TapRecorder",
    "VoiceError",
    "default_speech_worker",
    "is_transcription_junk",
    "voxtype_transcript",
]

RECORD_BINARIES = ("pw-record", "arecord")
# Every capture on this machine is the same shape: the 16 kHz mono 16-bit
# stream the local transcribers expect and the shared microphone tap
# delivers. Pinning it explicitly is the difference between "a WAV file"
# and a file the rest of Stella can actually read.
CAPTURE_RATE = 16000
CAPTURE_CHANNELS = 1
CAPTURE_WIDTH = 2
PLAY_COMMANDS = (
    lambda path: ["pw-play", path],
    lambda path: ["paplay", path],
    lambda path: ["aplay", "-q", path],
)
PLAY_BINARIES = ("pw-play", "paplay", "aplay")


class VoiceError(RuntimeError):
    """One friendly, user-facing voice failure. Never carries a trace."""


def default_speech_worker() -> str:
    """The resident synthesis worker Stella looks for on this machine.

    The same home-relative convention as the wake models: a user places
    the worker under ``~/tools`` and Stella finds it without a setting, a
    search path or a package dependency. Nothing is installed here — the
    probe only asks whether the file is executable.
    """

    return os.path.join(
        os.path.expanduser("~"), "tools", "stella-speak-server"
    )


def _cancel_process_tree(process: subprocess.Popen) -> None:
    """Best-effort SIGINT → terminate → kill ladder for one command.

    Runs on the cancelling thread and is bounded to about a second: a
    well-behaved command exits on the first signal, and anything that
    ignores the polite ones is killed. ``cancel_requested`` on the
    provider turns the resulting non-zero exit into an honest
    "cancelled" error rather than a "failed" one.
    """

    steps = (
        (lambda: process.send_signal(signal.SIGINT), 0.25),
        (process.terminate, 0.25),
        (process.kill, 0.5),
    )
    for action, wait_seconds in steps:
        try:
            action()
        except OSError:
            pass  # already gone (or unkillable): the wait below decides
        try:
            process.wait(timeout=wait_seconds)
            return
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return


class Recorder(ABC):
    """One explicit start/stop recording session. Never continuous."""

    @abstractmethod
    def available(self) -> bool:
        """Whether a recording session could be started at all."""

    @abstractmethod
    def start(self) -> None:
        """Begin recording after an explicit user action."""

    @abstractmethod
    def stop(self) -> str:
        """Finish recording and return the temporary audio file path."""

    @abstractmethod
    def cancel(self) -> None:
        """Abort a recording without producing a usable file."""

    def dispose(self) -> None:
        """Remove any files left by the most recent recording."""


class Player(ABC):
    """Blocking playback of one synthesized artifact, cancellable."""

    @abstractmethod
    def available(self) -> bool:
        """Whether audio playback could be attempted at all."""

    @abstractmethod
    def play(self, path: str) -> None:
        """Play one file to completion (or until stopped)."""

    @abstractmethod
    def stop(self) -> None:
        """Cancel current playback without touching anything else."""


class SubprocessRecorder(Recorder):
    """Records one clip through a local capture command (Pipewire/ALSA)."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._directory: str | None = None
        self._path: str | None = None
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        return any(shutil.which(binary) for binary in RECORD_BINARIES)

    def start(self) -> None:
        if not self.available():
            raise VoiceError(
                "No local recording command (pw-record or arecord) is "
                "available, so the microphone cannot be used."
            )
        with self._lock:
            if self._process is not None:
                raise VoiceError("Stella is already listening.")
            self._directory = tempfile.mkdtemp(prefix=f"stella-voice-{os.getpid()}-")
            self._path = os.path.join(self._directory, "capture.wav")
            argv = (
                [
                    "pw-record",
                    "--rate",
                    str(CAPTURE_RATE),
                    "--channels",
                    str(CAPTURE_CHANNELS),
                    self._path,
                ]
                if shutil.which("pw-record")
                else [
                    "arecord",
                    "-q",
                    "-f",
                    "S16_LE",
                    "-r",
                    str(CAPTURE_RATE),
                    "-c",
                    str(CAPTURE_CHANNELS),
                    "-t",
                    "wav",
                    self._path,
                ]
            )
            try:
                self._process = guarded_popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as error:
                self._directory = None
                self._path = None
                raise VoiceError(
                    f"Stella could not start recording ({error})."
                ) from error

    def stop(self) -> str:
        with self._lock:
            process, path = self._process, self._path
            self._process = None
        if process is None or path is None:
            raise VoiceError("Stella is not listening.")
        try:
            process.send_signal(signal.SIGINT)
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            self.dispose()
            raise VoiceError("Stella could not finish the recording.")
        except OSError as error:
            self.dispose()
            raise VoiceError(
                f"Stella could not finish the recording ({error})."
            ) from error
        # One call decides whether the recorder finalized its file. The
        # exit status is interpreted with this platform's conventions
        # (POSIX reports death-by-signal as a negative code; Windows
        # reports a console interrupt as an unsigned exit code) and the
        # capture itself has to exist and be larger than a bare 44-byte
        # RIFF/WAVE header. An unrecognised status fails closed.
        if not recording_finalized_ok(returncode, path):
            self.dispose()
            raise VoiceError(
                "The microphone produced no recording. Check that an input "
                "device is connected and not busy."
            )
        return path

    def cancel(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is not None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        self.dispose()

    def dispose(self) -> None:
        with self._lock:
            directory, self._directory = self._directory, None
            self._path = None
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)


class TapRecorder(Recorder):
    """Records one clip from the shared microphone tap.

    When Stella already keeps one capture open for the ears, a second
    ``pw-record`` for push-to-talk is one more handle on the same device
    and the classic "microphone is busy". This recorder takes frames from
    the tap instead and writes the WAV itself, so the contract every
    caller already relies on is unchanged: a private temporary path on
    ``stop()``, removed by ``dispose()``, and the same honest failure
    when nothing was captured.
    """

    def __init__(self, tap: MicTap) -> None:
        self._tap = tap
        self._client: TapClient | None = None
        self._writer: wave.Wave_write | None = None
        self._thread: threading.Thread | None = None
        self._directory: str | None = None
        self._path: str | None = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        # The tap reports its own faults; a dead capture cannot record.
        return not self._tap.failed

    def start(self) -> None:
        if not self.available():
            raise VoiceError(
                "The microphone is not available to Stella, so it cannot "
                "be used for voice input."
            )
        with self._lock:
            if self._thread is not None:
                raise VoiceError("Stella is already listening.")
            directory = tempfile.mkdtemp(prefix=f"stella-voice-{os.getpid()}-")
            path = os.path.join(directory, "capture.wav")
            client: TapClient | None = None
            try:
                client = self._tap.subscribe("recorder")
                # The handle is not a local: the capture thread owns it
                # until stop() or cancel() finalizes the file.
                writer = wave.open(path, "wb")  # noqa: SIM115
                writer.setnchannels(CAPTURE_CHANNELS)
                writer.setsampwidth(CAPTURE_WIDTH)
                writer.setframerate(CAPTURE_RATE)
            except (OSError, ValueError, wave.Error, VoiceError) as error:
                if client is not None:
                    client.close()  # a failed open must not hold the tap
                shutil.rmtree(directory, ignore_errors=True)
                raise VoiceError(
                    f"Stella could not start recording ({error})."
                ) from error
            self._directory = directory
            self._path = path
            self._client = client
            self._writer = writer
            self._thread = threading.Thread(
                target=self._run,
                args=(client, writer),
                name="stella-tap-recorder",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> str:
        with self._lock:
            thread, client, writer, path = (
                self._thread,
                self._client,
                self._writer,
                self._path,
            )
            self._thread = self._client = self._writer = None
        if thread is None or writer is None or path is None:
            raise VoiceError("Stella is not listening.")
        # Leaving the tap ends the frame stream, so the writer thread
        # finishes on its own and cannot be writing while the header is
        # finalized.
        if client is not None:
            client.close()
        thread.join(timeout=2)
        if thread.is_alive():  # pragma: no cover - a wedged frame source
            self.dispose()
            raise VoiceError("Stella could not finish the recording.")
        try:
            writer.close()
        except OSError as error:
            self.dispose()
            raise VoiceError(
                f"Stella could not finish the recording ({error})."
            ) from error
        # Same conclusion the subprocess recorder reaches, same fail
        # closed rule: no usable file means nothing goes to the model.
        if not recording_finalized_ok(0, path):
            self.dispose()
            raise VoiceError(
                "The microphone produced no recording. Check that an input "
                "device is connected and not busy."
            )
        return path

    def cancel(self) -> None:
        with self._lock:
            thread, client, writer = self._thread, self._client, self._writer
            self._thread = self._client = self._writer = None
        if client is not None:
            client.close()
        if thread is not None:
            thread.join(timeout=2)
        if writer is not None:
            try:
                writer.close()
            except OSError as error:
                del error  # the file is being thrown away anyway
        self.dispose()

    def dispose(self) -> None:
        with self._lock:
            directory, self._directory = self._directory, None
            self._path = None
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)

    @staticmethod
    def _run(client: TapClient, writer: wave.Wave_write) -> None:
        # One subscriber reads one frame at a time, so a whole frame is
        # what ``read()`` returns; ``b""`` is the tap saying it is done.
        while True:
            frame = client.read()
            if not frame:
                return
            try:
                writer.writeframes(frame)
            except (OSError, wave.Error):  # pragma: no cover - disk gone
                return


class SubprocessPlayer(Player):
    """Plays one artifact through a local playback command."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        return any(shutil.which(binary) for binary in PLAY_BINARIES)

    def play(self, path: str) -> None:
        if not self.available():
            raise VoiceError(
                "No local playback command (pw-play, paplay, or aplay) is "
                "available."
            )
        if not os.path.exists(path):
            raise VoiceError("The audio to play has already been removed.")
        argv = next(
            command(path)
            for binary, command in zip(
                PLAY_BINARIES, PLAY_COMMANDS, strict=True
            )
            if shutil.which(binary)
        )
        with self._lock:
            if self._process is not None:
                raise VoiceError("Stella is already speaking.")
            try:
                process = guarded_popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as error:
                raise VoiceError(
                    f"Stella could not start playback ({error})."
                ) from error
            self._process = process
        try:
            returncode = process.wait()
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
        # A cancelled stream exits non-zero; that is an intentional stop.
        if returncode != 0:
            if getattr(process, "_stella_cancelled", False):
                return
            raise VoiceError("Stella could not play the response audio.")

    def stop(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            # Mark intent first: playback cancellation must never be
            # reported as a failure, and it touches nothing but audio.
            process._stella_cancelled = True
            try:
                process.terminate()
            except OSError as error:
                del error


# The lines a local Whisper model invents over silence or over the tail of
# a recording. Matching is on the whole transcript, never a substring, so a
# real request that happens to contain one of these words survives: no
# confidence score, no model, no new dependency. Trailing punctuation is
# stripped on both sides, so these are written in their bare form.
_TRANSCRIPTION_NOISE_LINES = frozenset(
    {
        "",
        "you",
        "thanks",
        "thank you",
        "thanks for watching",
        "thank you for watching",
        "[music]",
        "(upbeat music)",
        "[applause]",
        "[inaudible]",
    }
)


def is_transcription_junk(text: str) -> bool:
    """True for the filler a transcriber produces when nobody spoke."""

    heard = text.strip().casefold().rstrip(".!?")
    return heard in _TRANSCRIPTION_NOISE_LINES


def voxtype_transcript(stdout: str) -> str:
    """Return only the transcript from a ``voxtype -q`` run.

    voxtype's quiet flag moves its ``INFO`` log to stderr but still prints
    a progress block on stdout — the file it loaded, the format it read it
    as, the resample and processing lines — then a blank line and the
    words. That block is the tool narrating its own work, not something
    anybody said; read verbatim it would enter the conversation as user
    text on every spoken turn. Output without a blank line is passed
    through unchanged, since a run that printed no block is already only
    the transcript (possibly empty, which the junk filter handles).
    """

    _progress, separator, words = stdout.partition("\n\n")
    return words if separator else stdout


class CommandTranscriptionProvider(TranscriptionProvider):
    """Runs a local command over the recorded file and reads its stdout.

    ``template`` is an argv list where ``"{input}"`` is replaced by the
    audio path, e.g. ``["whisper-cli", "-m", "model.bin", "{input}"]``.
    Arguments are passed without a shell, so no quoting can be injected.
    ``name`` is what the user is told is transcribing them.

    ``extract`` is optional and only ever supplied for the built-in
    engine. A command the owner wrote themselves is documented as
    "prints the transcript on stdout", so its stdout is the transcript
    and nothing else; a tool that interleaves its own progress reporting
    is Stella's problem to solve, not a licence to reinterpret the
    owner's bytes.
    """

    def __init__(
        self,
        template: list[str],
        timeout: float = 120.0,
        name: str = "a local command",
        extract: Callable[[str], str] | None = None,
    ) -> None:
        if not template or not any("{input}" in part for part in template):
            raise ValueError(
                "a transcription command must reference {input}"
            )
        self._template = list(template)
        self._timeout = timeout
        self.name = name
        self._extract = extract
        self._process: subprocess.Popen[str] | None = None
        self._cancel_requested = False
        self._lock = threading.Lock()

    def cancel(self) -> None:
        """Abort a running transcription command (A8: cancel reaches voice).

        Safe to call from any thread while :meth:`transcribe` blocks on the
        worker thread; it touches only this command's own process.
        """

        with self._lock:
            process = self._process
            # Mark intent first: a killed command must report honestly that
            # it was cancelled, not that it failed.
            self._cancel_requested = True
        if process is not None:
            _cancel_process_tree(process)

    def transcribe(self, audio: InputPart) -> str:
        if audio.modality is not InputModality.AUDIO:
            raise ValueError("transcription expects an audio input part")
        if audio.reference is None:
            raise VoiceError("the recording has no readable reference.")
        argv = [part.replace("{input}", audio.reference) for part in self._template]
        try:
            process = guarded_popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as error:
            raise VoiceError(
                "The configured transcription command was not found."
            ) from error
        except OSError as error:
            raise VoiceError(
                f"Local transcription failed ({error})."
            ) from error
        with self._lock:
            self._process = process
        timed_out = False
        try:
            stdout, _stderr = process.communicate(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            stdout, _stderr = process.communicate()
        except OSError as error:
            raise VoiceError(
                f"Local transcription failed ({error})."
            ) from error
        finally:
            with self._lock:
                self._process = None
                cancelled = self._cancel_requested
                # One intent lives for one run: a cancel that arrived
                # before the spawn still discards this result (honest,
                # never invented), and the next run starts unmarked.
                self._cancel_requested = False
        if timed_out:
            raise VoiceError("Local transcription took too long.")
        if cancelled:
            raise VoiceError("Local transcription was cancelled.")
        if process.returncode != 0:
            raise VoiceError("Local transcription failed.")
        return stdout if self._extract is None else self._extract(stdout)


class OpenAITranscriptionProvider(TranscriptionProvider):
    """Optional cloud transcription through the OpenAI-compatible endpoint.

    Only used when the user selects it (default ``auto`` falls back to it
    when an API key is configured); ``off`` disables it entirely.

    ``timeout`` bounds the request (``STELLA_TRANSCRIPTION_TIMEOUT``):
    without it a stalled cloud call held the microphone's turn open
    indefinitely, which is exactly the wait a user cannot cancel.
    """

    def __init__(
        self,
        client: object,
        model: str = "whisper-1",
        timeout: float = 30.0,
    ) -> None:
        self._client = client
        self._model = model
        self._timeout = timeout
        self.name = f"cloud transcription ({model})"

    def transcribe(self, audio: InputPart) -> str:
        if audio.modality is not InputModality.AUDIO:
            raise ValueError("transcription expects an audio input part")
        if audio.reference is None:
            raise VoiceError("the recording has no readable reference.")
        try:
            with open(audio.reference, "rb") as handle:
                result = self._client.audio.transcriptions.create(  # type: ignore[attr-defined]
                    model=self._model,
                    file=handle,
                    timeout=self._timeout,
                )
        except VoiceError:
            raise
        except Exception as error:  # friendly text, never a trace
            detail = " ".join(str(error).split()) or type(error).__name__
            raise VoiceError(
                f"Cloud transcription failed ({detail[:120]})."
            ) from error
        text = getattr(result, "text", None)
        return text if isinstance(text, str) else ""


class CommandSpeechProvider(SpeechProvider):
    """Renders text with a local command (espeak-ng, piper, ...).

    ``template`` replaces ``"{text}"`` and ``"{output}"``; the command must
    write a playable file to ``{output}``.
    """

    def __init__(self, template: list[str], timeout: float = 60.0) -> None:
        if (
            not template
            or not any("{text}" in part for part in template)
            or not any("{output}" in part for part in template)
        ):
            raise ValueError(
                "a speech command must reference {text} and {output}"
            )
        self._template = list(template)
        self._timeout = timeout
        self._directory = tempfile.mkdtemp(prefix=f"stella-speech-{os.getpid()}-")
        self._counter = 0
        self._process: subprocess.Popen[str] | None = None
        self._cancel_requested = False
        self._lock = threading.Lock()

    def cancel(self) -> None:
        """Abort a running synthesis command; touches only its own process."""

        with self._lock:
            process = self._process
            self._cancel_requested = True
        if process is not None:
            _cancel_process_tree(process)

    def speak(self, output: SpeechOutput) -> SpeechArtifact:
        bounded = output.text[:MAX_SPEECH_TEXT_CHARS]
        # One fresh file per call: an earlier artifact may still be queued
        # for or occupying playback when the next sentence is synthesized.
        self._counter += 1
        path = os.path.join(
            self._directory, f"reply-{self._counter}-{os.getpid()}.wav"
        )
        argv = [
            part.replace("{text}", bounded).replace("{output}", path)
            for part in self._template
        ]
        try:
            process = guarded_popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as error:
            raise VoiceError(
                "The configured speech command was not found."
            ) from error
        except OSError as error:
            raise VoiceError(f"Local speech failed ({error}).") from error
        with self._lock:
            self._process = process
        timed_out = False
        try:
            process.communicate(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.communicate()
        except OSError as error:
            raise VoiceError(f"Local speech failed ({error}).") from error
        finally:
            with self._lock:
                self._process = None
                cancelled = self._cancel_requested
                self._cancel_requested = False
        if timed_out:
            raise VoiceError("Local speech took too long.")
        if cancelled:
            raise VoiceError("Local speech was cancelled.")
        if process.returncode != 0 or not os.path.exists(path):
            raise VoiceError("Local speech failed.")
        return SpeechArtifact(reference=path)

    def dispose(self) -> None:
        shutil.rmtree(self._directory, ignore_errors=True)


class ResidentSpeechProvider(SpeechProvider):
    """Render text through ONE long-lived synthesis worker process.

    ``command`` names a line-JSON server: it prints ``{"ready": true}``
    once its model is loaded, then answers one request per line —
    ``{"id": int, "text": str, "output": str}`` in, ``{"id": int,
    "ok": bool, "error": str}`` out — and writes a playable file to
    ``output``. The worker starts lazily and survives between
    sentences, so the interpreter start-up and model load that a
    per-sentence command pays every time (~3–4 s measured for Kokoro,
    research report 24) are paid once per session instead. The same
    ``{text}``/``{output}`` template understood by
    :class:`CommandSpeechProvider` is NOT valid here: a worker that
    speaks this protocol is a different program by design, and the
    two providers stay separately selectable so a machine without one
    keeps the old behaviour (D2 degradation rule: a broken worker
    degrades to no speech, and the text reply stays available).

    A worker that dies, times out or answers badly is retired on the
    spot; the next sentence starts a fresh one. Nothing a worker says
    is trusted beyond "the file exists at the path we chose": the
    artifact reference is this provider's own bounded temp directory,
    never a path from the worker's reply. That file is then opened
    and shape-checked before the artifact is returned: unreadable,
    zero-frame, sub-frame or peak-silent output raises VoiceError
    the same way a broken worker would, and a well-shaped PCM s16
    mono file has its leading and trailing silence trimmed and its
    edges linearly faded so the first syllable lands without a
    click. A valid-but-unsupported shape is passed through untouched
    rather than rejected — this check exists to catch a broken
    worker, not to police every WAV a future worker might produce.
    """

    def __init__(
        self,
        command: list[str],
        *,
        timeout: float = 60.0,
        ready_timeout: float = 180.0,
        voice: str | None = None,
        speed: float | None = None,
    ) -> None:
        if not command:
            raise ValueError("a resident speech worker needs a command")
        self._command = list(command)
        self._timeout = timeout
        self._ready_timeout = ready_timeout
        # Additive request fields, sent only when set. A worker that
        # already chooses its own voice ignores the extra keys, so naming
        # one here can never break an existing installation; it only makes
        # a worker that reads them able to.
        self._voice = voice
        self._speed = speed
        self._disposed = False
        self._directory = tempfile.mkdtemp(prefix=f"stella-speech-{os.getpid()}-")
        self._counter = 0
        self._request_id = 0
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] | None = None
        self._cancel_requested = False
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()

    def cancel(self) -> None:
        """Abort a running synthesis; the worker is retired, not reused."""

        with self._lock:
            process = self._process
            self._cancel_requested = True
        if process is not None:
            _cancel_process_tree(process)
            with self._lock:
                if self._process is process:
                    self._process = None
                    self._lines = None

    def speak(self, output: SpeechOutput) -> SpeechArtifact:
        bounded = output.text[:MAX_SPEECH_TEXT_CHARS]
        with self._request_lock:
            self._counter += 1
            path = os.path.join(
                self._directory, f"reply-{self._counter}-{os.getpid()}.wav"
            )
            with self._lock:
                self._ensure_worker_locked()
                process = self._process
                lines = self._lines
                assert process is not None and lines is not None
                assert process.stdin is not None
                self._request_id += 1
                request_id = self._request_id
                request = {
                    "id": request_id,
                    "text": bounded,
                    "output": path,
                }
                # Additive: a worker that knows nothing about these keys
                # ignores them and speaks as it always has, so naming a
                # voice here is a request, never a requirement.
                if self._voice is not None:
                    request["voice"] = self._voice
                if self._speed is not None:
                    request["speed"] = self._speed
                try:
                    process.stdin.write(json.dumps(request) + "\n")
                    process.stdin.flush()
                except OSError as error:
                    self._retire_locked()
                    raise VoiceError(
                        f"Resident speech worker broke ({error})."
                    ) from error
            reply = self._await_reply(lines, process, request_id)
            with self._lock:
                cancelled = self._cancel_requested
                self._cancel_requested = False
            if cancelled:
                raise VoiceError("Resident speech was cancelled.")
            if reply is None:
                with self._lock:
                    self._retire_locked()
                raise VoiceError(
                    "Resident speech took too long or stopped answering."
                )
            if not reply.get("ok"):
                detail = str(reply.get("error", ""))[:120] or "worker refused"
                raise VoiceError(f"Resident speech failed ({detail}).")
            if not os.path.exists(path):
                raise VoiceError("Resident speech produced no audio file.")
            _inspect_speech_artifact(path)
            return SpeechArtifact(reference=path)

    def dispose(self) -> None:
        with self._lock:
            self._disposed = True
            self._retire_locked()
        shutil.rmtree(self._directory, ignore_errors=True)

    def prewarm(self) -> None:
        """Pay the model load now, so the first reply does not wait for it.

        Deliberately silent about failure: a worker that will not start is
        reported by the first real synthesis, which has a user to tell. A
        background thread that raised here would only print a traceback
        nobody asked for. The disposed check is under the same lock
        ``dispose()`` takes to retire the worker, so a warm-up that loses
        the race cannot leave a process behind.
        """

        try:
            with self._lock:
                if self._disposed:
                    return
                self._ensure_worker_locked()
        except Exception:  # noqa: BLE001 - best effort, never user-visible
            # Includes VoiceError: a worker that will not start is the
            # first real synthesis's message to deliver, not this
            # thread's — and a warm-up thread has no one to tell.
            return

    def _ensure_worker_locked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if self._process is not None:
            self._retire_locked()
        try:
            process = guarded_popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
            )
        except FileNotFoundError as error:
            raise VoiceError(
                "The configured resident speech command was not found."
            ) from error
        except OSError as error:
            raise VoiceError(f"Resident speech could not start ({error}).") from error
        assert process.stdout is not None
        self._process = process
        lines: queue.Queue[str | None] = queue.Queue()
        self._lines = lines
        threading.Thread(
            target=self._pump_stdout, args=(process.stdout, lines), daemon=True
        ).start()
        ready = self._await_line(lines, process, self._ready_timeout)
        payload = _json_line(ready)
        if payload is None or not payload.get("ready"):
            reason = str((payload or {}).get("error", ""))[:120]
            self._retire_locked()
            suffix = f": {reason}" if reason else ""
            raise VoiceError(
                "The resident speech worker did not become ready" + suffix
            )

    def _await_reply(
        self,
        lines: queue.Queue[str | None],
        process: subprocess.Popen[str],
        request_id: int,
    ) -> dict | None:
        """Next good reply for this id; stale or unreadable lines are skipped."""

        while True:
            raw = self._await_line(lines, process, self._timeout)
            if raw is None:
                return None
            payload = _json_line(raw)
            if payload is None:
                continue
            if payload.get("id") == request_id:
                return payload

    def _await_line(
        self,
        lines: queue.Queue[str | None],
        process: subprocess.Popen[str],
        timeout: float,
    ) -> str | None:
        try:
            line = lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:  # worker EOF mid-request
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()
            return None
        return line

    def _pump_stdout(self, stream, sink: queue.Queue[str | None]) -> None:
        for line in stream:
            sink.put(line)
        sink.put(None)

    def _retire_locked(self) -> None:
        process, self._process = self._process, None
        self._lines = None
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()
                process.wait(timeout=5)


def _json_line(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


# A resident worker's reply is a promise about a file, not the file
# itself: the shape and loudness checks below are what turn "the worker
# said ok" into "the user heard something". The numbers match what the
# local voice research surfaced for Kokoro-shaped output — 10 ms of
# leading silence is inside the audible onset, 25/15 ms of safety pad
# keeps the trim from clipping consonants, and 15/10 ms linear fades
# kill the click a hard boundary would make. They are fixed here, not
# configurable: this is a correctness rule, not a personality setting.
_ARTIFACT_MIN_SECONDS = 0.01
_ARTIFACT_PEAK_SILENCE_RATIO = 0.005
_ARTIFACT_TRIM_SILENCE_RATIO = 0.01
_ARTIFACT_LEADING_PAD_SECONDS = 0.025
_ARTIFACT_TRAILING_PAD_SECONDS = 0.015
_ARTIFACT_FADE_IN_SECONDS = 0.015
_ARTIFACT_FADE_OUT_SECONDS = 0.01
_ARTIFACT_MIN_SURVIVING_SECONDS = 0.05


def _full_scale(sampwidth: int) -> int | None:
    """Signed peak for a PCM sampwidth, or None if we do not decode it."""

    return {1: 128, 2: 32768, 4: 2147483648}.get(sampwidth)


def _decode_pcm(data: bytes, sampwidth: int):
    """Return a signed sequence for peak scanning, or None if unsupported."""

    if sampwidth == 1:
        return [b - 128 for b in data]
    if sampwidth == 2:
        arr = array.array("h")
        arr.frombytes(data)
        if sys.byteorder != "little":
            arr.byteswap()
        return arr
    if sampwidth == 4:
        arr = array.array("i")
        arr.frombytes(data)
        if sys.byteorder != "little":
            arr.byteswap()
        return arr
    return None


def _apply_fade(samples, start: int, count: int, direction: str) -> None:
    """Ramp `count` samples linearly, in from silence or out to silence.

    A one-sample or zero-sample fade is a no-op: the click it prevents is
    at least as loud as the fade would be, and dividing by a zero-length
    ramp is a bug, not a feature.
    """

    if count <= 1:
        return
    if direction == "in":
        for k in range(count):
            samples[start + k] = (samples[start + k] * k) // count
    else:
        for k in range(count):
            gain = count - 1 - k
            samples[start + k] = (samples[start + k] * gain) // (count - 1)


def _inspect_speech_artifact(path: str) -> None:
    """Reject silent or truncated worker output; trim/fade PCM s16 mono.

    The output side of the microphone: a WAV the pipeline can play is
    not the same claim as audio the user can hear, and today's silent
    bug class is a worker that returns ``ok`` with a zero-byte body, a
    bare RIFF header, or a full file of samples below audibility. Each
    of those is reported as a VoiceError so app.py's D2 degradation
    rule takes over (text reply, no false "speaking" state) instead of
    the pipeline playing nothing while the dot says Stella is talking.

    For a well-shaped PCM s16 mono file — which is what Kokoro
    actually produces — the artifact is additionally trimmed of
    leading and trailing silence and given linear edge fades. Any
    other shape (stereo, 24-bit, compressed) is validated and passed
    through: this is a correctness fix, not a rewriting service.
    """

    try:
        with wave.open(path, "rb") as source:
            framerate = source.getframerate()
            nchannels = source.getnchannels()
            sampwidth = source.getsampwidth()
            nframes = source.getnframes()
            comptype = source.getcomptype()
            data = source.readframes(nframes)
    except (wave.Error, OSError, EOFError) as error:
        raise VoiceError(
            "Resident speech produced an unreadable audio file."
        ) from error

    if (
        framerate <= 0
        or nframes == 0
        or nframes < framerate * _ARTIFACT_MIN_SECONDS
    ):
        raise VoiceError("Resident speech produced only silence.")

    scale = _full_scale(sampwidth)
    if scale is None:
        return  # shape we do not decode: file exists and is a real WAV
    samples = _decode_pcm(data, sampwidth)
    if samples is None:
        return
    peak = max((abs(int(s)) for s in samples), default=0)
    if peak < scale * _ARTIFACT_PEAK_SILENCE_RATIO:
        raise VoiceError("Resident speech produced only silence.")

    if nchannels != 1 or sampwidth != 2 or comptype != "NONE":
        return  # validated but not the trimmable shape

    arr = samples  # already an array("h") at native byte order for us
    trim_threshold = int(scale * _ARTIFACT_TRIM_SILENCE_RATIO)
    first = next(
        (i for i, s in enumerate(arr) if abs(int(s)) >= trim_threshold),
        None,
    )
    if first is None:
        # Peak scan already said audible, so this is unreachable in
        # practice; raise anyway rather than rewrite a file we cannot
        # locate the boundaries of.
        raise VoiceError("Resident speech produced only silence.")
    last = next(
        i
        for i in range(len(arr) - 1, -1, -1)
        if abs(int(arr[i])) >= trim_threshold
    )
    leading_pad = int(framerate * _ARTIFACT_LEADING_PAD_SECONDS)
    trailing_pad = int(framerate * _ARTIFACT_TRAILING_PAD_SECONDS)
    start = max(0, first - leading_pad)
    end = min(len(arr), last + 1 + trailing_pad)
    min_surviving = int(framerate * _ARTIFACT_MIN_SURVIVING_SECONDS)
    if end - start < min_surviving:
        return  # too short for a trim to leave anything worth fading

    trimmed = arr[start:end]
    fade_in = min(
        int(framerate * _ARTIFACT_FADE_IN_SECONDS), len(trimmed) // 2
    )
    fade_out = min(
        int(framerate * _ARTIFACT_FADE_OUT_SECONDS), len(trimmed) // 2
    )
    _apply_fade(trimmed, 0, fade_in, "in")
    _apply_fade(trimmed, len(trimmed) - fade_out, fade_out, "out")
    if sys.byteorder != "little":
        trimmed.byteswap()
    try:
        with wave.open(path, "wb") as sink:
            sink.setnchannels(nchannels)
            sink.setsampwidth(sampwidth)
            sink.setframerate(framerate)
            sink.writeframes(trimmed.tobytes())
    except (wave.Error, OSError) as error:
        raise VoiceError(
            "Resident speech artifact could not be finalized."
        ) from error


class OpenAISpeechProvider(SpeechProvider):
    """Optional cloud text-to-speech through the OpenAI-compatible SDK."""

    def __init__(
        self, client: object, model: str = "tts-1", voice: str = "alloy"
    ) -> None:
        self._client = client
        self._model = model
        self._voice = voice
        self._directory = tempfile.mkdtemp(prefix=f"stella-speech-{os.getpid()}-")
        self._counter = 0

    def speak(self, output: SpeechOutput) -> SpeechArtifact:
        self._counter += 1
        path = os.path.join(
            self._directory, f"reply-{self._counter}-{os.getpid()}.wav"
        )
        try:
            stream = self._client.audio.speech.create(  # type: ignore[attr-defined]
                model=self._model,
                voice=self._voice,
                input=output.text[:MAX_SPEECH_TEXT_CHARS],
            )
            with open(path, "wb") as handle:
                handle.writelines(stream.response.iter_bytes())
        except VoiceError:
            raise
        except Exception as error:  # friendly text, never a trace
            detail = " ".join(str(error).split()) or type(error).__name__
            raise VoiceError(
                f"Cloud speech synthesis failed ({detail[:120]})."
            ) from error
        return SpeechArtifact(reference=path)

    def dispose(self) -> None:
        shutil.rmtree(self._directory, ignore_errors=True)
