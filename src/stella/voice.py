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

import os
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

    def transcribe(self, audio: InputPart) -> str:
        if audio.modality is not InputModality.AUDIO:
            raise ValueError("transcription expects an audio input part")
        if audio.reference is None:
            raise VoiceError("the recording has no readable reference.")
        argv = [part.replace("{input}", audio.reference) for part in self._template]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as error:
            raise VoiceError(
                "The configured transcription command was not found."
            ) from error
        except OSError as error:
            raise VoiceError(
                f"Local transcription failed ({error})."
            ) from error
        except subprocess.TimeoutExpired:
            raise VoiceError("Local transcription took too long.")
        if completed.returncode != 0:
            raise VoiceError("Local transcription failed.")
        return completed.stdout


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

    def speak(self, output: SpeechOutput) -> SpeechArtifact:
        bounded = output.text[:MAX_SPEECH_TEXT_CHARS]
        path = os.path.join(self._directory, f"reply-{os.getpid()}.wav")
        argv = [
            part.replace("{text}", bounded).replace("{output}", path)
            for part in self._template
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as error:
            raise VoiceError(
                "The configured speech command was not found."
            ) from error
        except OSError as error:
            raise VoiceError(f"Local speech failed ({error}).") from error
        except subprocess.TimeoutExpired:
            raise VoiceError("Local speech took too long.")
        if completed.returncode != 0 or not os.path.exists(path):
            raise VoiceError("Local speech failed.")
        return SpeechArtifact(reference=path)

    def dispose(self) -> None:
        shutil.rmtree(self._directory, ignore_errors=True)


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
