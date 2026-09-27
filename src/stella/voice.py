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

import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
from abc import ABC, abstractmethod

from stella.audio import TranscriptionProvider
from stella.audio_output import (
    MAX_SPEECH_TEXT_CHARS,
    SpeechArtifact,
    SpeechOutput,
    SpeechProvider,
)
from stella.context import InputModality, InputPart

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
    "VoiceError",
]

RECORD_BINARIES = ("pw-record", "arecord")
PLAY_COMMANDS = (
    lambda path: ["pw-play", path],
    lambda path: ["paplay", path],
    lambda path: ["aplay", "-q", path],
)
PLAY_BINARIES = ("pw-play", "paplay", "aplay")


class VoiceError(RuntimeError):
    """One friendly, user-facing voice failure. Never carries a trace."""


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
            self._directory = tempfile.mkdtemp(prefix="stella-voice-")
            self._path = os.path.join(self._directory, "capture.wav")
            argv = (
                ["pw-record", self._path]
                if shutil.which("pw-record")
                else ["arecord", "-q", "-f", "cd", "-t", "wav", self._path]
            )
            try:
                self._process = subprocess.Popen(
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
        # 44 bytes is a bare RIFF/WAVE header: nothing was captured.
        if returncode not in (0, -signal.SIGINT, 2) or not os.path.exists(
            path
        ) or os.path.getsize(path) <= 44:
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
                process = subprocess.Popen(
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


class CommandTranscriptionProvider(TranscriptionProvider):
    """Runs a local command over the recorded file and reads its stdout.

    ``template`` is an argv list where ``"{input}"`` is replaced by the
    audio path, e.g. ``["whisper-cli", "-m", "model.bin", "{input}"]``.
    Arguments are passed without a shell, so no quoting can be injected.
    """

    def __init__(self, template: list[str], timeout: float = 120.0) -> None:
        if not template or not any("{input}" in part for part in template):
            raise ValueError(
                "a transcription command must reference {input}"
            )
        self._template = list(template)
        self._timeout = timeout
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
            process = subprocess.Popen(
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
        return stdout


class OpenAITranscriptionProvider(TranscriptionProvider):
    """Optional cloud transcription through the OpenAI-compatible endpoint.

    Only used when the user selects it (default ``auto`` falls back to it
    when an API key is configured); ``off`` disables it entirely.
    """

    def __init__(self, client: object, model: str = "whisper-1") -> None:
        self._client = client
        self._model = model

    def transcribe(self, audio: InputPart) -> str:
        if audio.modality is not InputModality.AUDIO:
            raise ValueError("transcription expects an audio input part")
        if audio.reference is None:
            raise VoiceError("the recording has no readable reference.")
        try:
            with open(audio.reference, "rb") as handle:
                result = self._client.audio.transcriptions.create(  # type: ignore[attr-defined]
                    model=self._model, file=handle
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
        self._directory = tempfile.mkdtemp(prefix="stella-speech-")
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
            process = subprocess.Popen(
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
    never a path from the worker's reply.
    """

    def __init__(
        self,
        command: list[str],
        *,
        timeout: float = 60.0,
        ready_timeout: float = 180.0,
    ) -> None:
        if not command:
            raise ValueError("a resident speech worker needs a command")
        self._command = list(command)
        self._timeout = timeout
        self._ready_timeout = ready_timeout
        self._directory = tempfile.mkdtemp(prefix="stella-speech-")
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
                try:
                    process.stdin.write(
                        json.dumps(
                            {"id": request_id, "text": bounded, "output": path}
                        )
                        + "\n"
                    )
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
            return SpeechArtifact(reference=path)

    def dispose(self) -> None:
        with self._lock:
            self._retire_locked()
        shutil.rmtree(self._directory, ignore_errors=True)

    def _ensure_worker_locked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if self._process is not None:
            self._retire_locked()
        try:
            process = subprocess.Popen(
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


class OpenAISpeechProvider(SpeechProvider):
    """Optional cloud text-to-speech through the OpenAI-compatible SDK."""

    def __init__(
        self, client: object, model: str = "tts-1", voice: str = "alloy"
    ) -> None:
        self._client = client
        self._model = model
        self._voice = voice
        self._directory = tempfile.mkdtemp(prefix="stella-speech-")
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
