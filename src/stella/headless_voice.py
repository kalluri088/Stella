"""`stella voice`: one hands-free turn for a keyboard shortcut.

The owner presses a key (Super+D) and Stella answers from the most recent
saved settings — voice in, voice out, real work in between — with no
window at all. This module is *only* orchestration: every primitive it
uses is the same one the on-screen application already relies on.

It runs one turn and exits, so nothing is resident and the next press
sees the next save. A transcript leaves here as ordinary text and goes
through the exact same ``StellaSession.run_turn`` path as typed input:
headless voice gains no reasoning path, no approval, and no authority
(rule 3, rule 10). Risky actions are approved *out loud*, and every
answer that is not a clear yes is a denial (fail closed). A short chime
marks listening and working, so the user knows which silence is theirs.

``stella voice --serve`` is the resident form of the same promise: the
saved wake ear stays armed, and a wake phrase or a ``--toggle`` opens a
*conversation* — turn after turn with no button between them — until
silence, a spoken stop, or the shortcut again returns the process to the
idle ear. ``stella voice --toggle`` is what the keyboard shortcut runs:
it starts the server when none is up and otherwise sends one word over a
private 0600 unix socket. The socket carries exactly two commands and no
arguments, so it is a doorbell, never a control channel: it cannot
start work, only open Stella's ears — and the approvals inside a
conversation are the same spoken, fail-closed ones.

The security posture that must never loosen here:
  * raw tool arguments are never spoken — a file-write body would read a
    document aloud — only ``action_summary``'s bounded description;
  * a saved key or the transcript is never printed; failures are honest
    lines, never a fake success;
  * exactly one microphone handle is open at a time (``MicTap`` shared
    by the recorder and the silence-watcher), and it is released on exit;
  * the control socket carries no payloads — no command that a spoken
    approval would not already gate.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections.abc import Callable

from stella.app import build_application, build_wake, build_wake_ear
from stella.barge_in import capture_command
from stella.config import resolve_settings
from stella.mic_tap import MicTap
from stella.tools import (
    ActionPreview,
    ApprovalRequest,
    ToolApproval,
    action_summary,
)
from stella.voice import VoiceError, is_transcription_junk

__all__ = [
    "run_headless_voice",
    "run_voice_server",
    "run_voice_stop",
    "run_voice_toggle",
]

#: Longest a capture may run before the endpoint is considered lost. The
#: wake endpoint caps real speech at 20 s and times out silence at 8 s,
#: so waiting past its own horizon only ever means a dead microphone —
#: which is then read as "nothing heard", not a hang.
CAPTURE_WAIT_SECONDS = 22.0

#: How long ``--toggle`` waits for a server it just started to answer.
#: Coming up is resolve + build + bind; twelve seconds is generous, and
#: a key that never got its server says so rather than hanging.
SERVER_START_WAIT_SECONDS = 12.0

#: Words that mean "yes". Approval needs one of these AND no denial word.
_APPROVE_WORDS = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "approve",
        "allow",
        "affirmative",
        "absolutely",
        "definitely",
        "go",
    }
)
#: Words that mean "no", or "hold off". Any one of these denies, even
#: beside an affirmative ("yes... no"), because the answer is not clear.
_DENY_WORDS = frozenset(
    {
        "no",
        "nope",
        "nah",
        "not",
        "dont",
        "cant",
        "cancel",
        "stop",
        "deny",
        "never",
        "reject",
        "negative",
        "abort",
        "wait",
    }
)
#: Whole-utterance replies that end a conversation. Anything longer is a
#: request, never a dismissal — "stop the timer" must not silence Stella.
_STOP_PHRASES = frozenset(
    {
        "stop",
        "quit",
        "exit",
        "done",
        "goodbye",
        "bye",
        "that's all",
        "that's it",
        "never mind",
        "nevermind",
    }
)

# (frequency Hz, duration s) per chime; "listen" is one rising tone,
# "working" a two-tone acknowledgement. A stdlib sine, no asset, no dep.
_CHIME_TONES: dict[str, tuple[tuple[float, float], ...]] = {
    "listen": ((740.0, 0.10), (988.0, 0.12)),
    "working": ((587.0, 0.09),),
}

_NOT_CONFIGURED = (
    "Stella is not configured yet. Open the setup window once (stella-ui) "
    "to pick a model, then use the voice shortcut."
)
_VOICE_SETUP_NEEDED = (
    "Voice in this mode needs both a microphone that Stella can record "
    "and spoken replies turned on. Enable voice input and speech in "
    "Stella's Settings once, then press the key again."
)
_ALREADY_RUNNING = (
    "A Stella voice session server is already running; this one exits "
    "instead of taking the microphone from it."
)
_NO_SERVER = "No Stella voice session server is running."
_SERVER_START_FAILED = (
    "Stella's voice session did not come up. Run 'stella voice --serve' "
    "in a terminal — or read the log beside the control socket — to see "
    "why."
)


def headless_settings(settings):
    """Force the continuous-ear and the risky tools off for one-shot voice.

    Wake, shell and browser now default ON in the saved config so the desktop
    UI offers them. ``stella voice`` is a single utterance per press with no
    continuous wake loop and — per the owner's "headless stays off" scope —
    must not silently gain an always-open spotter, a shell, or a browser. Each
    of those would only be reachable through a spoken approval this shortcut
    was never designed to host. So the headless wrapper disarms all three no
    matter what was saved, and leaves every other field intact (a pure
    override, not a mutation). The voice shortcut's own silence endpointer
    (``build_wake_ear``) is independent of the wake mode, so hands-free
    cut-off is unaffected — the command stays byte-for-byte what it was before
    these defaults flipped.
    """

    return dataclasses.replace(
        settings,
        wake_word="off",
        wake_word_enabled=False,
        shell_tools_enabled=False,
        browser_tools_enabled=False,
    )


def serve_settings(settings):
    """Resident mode: wake exactly as saved, shell and browser still off.

    ``headless_settings`` is the *one-shot's* promise — no always-open
    ear behind a button that fires once. ``--serve`` exists to host the
    continuous wake the owner chose, so it honours the saved wake fields
    and forces off only the other two: a spoken approval inside a
    conversation must not smuggle in the shell or the browser this
    shortcut was never meant to carry. Desktop tools are not a setting
    at all (the registry offers them only when it recognises the
    session), so "open Chromium in workspace 1" still works here.
    """

    return dataclasses.replace(
        settings,
        shell_tools_enabled=False,
        browser_tools_enabled=False,
    )


def run_headless_voice() -> int:
    """Run exactly one voice turn from the latest settings; return a code.

    Exit codes are deliberate and distinct so a launcher or a terminal
    can tell the cases apart: 0 ran (or honestly heard nothing), 2 is
    unconfigured, 3 is voice-capable-but-not-set-up, 4 is a build failure.
    Never does a missing peripheral become a silent success.
    """

    settings = resolve_settings()
    if settings is None:
        print(_NOT_CONFIGURED, file=sys.stderr)
        return 2
    # A SystemExit from the build is a configuration error the owner can
    # read verbatim ("STELLA_MODEL is required"), so let it stand.
    application = build_application(headless_settings(settings))
    try:
        return _run_one_turn(application)
    finally:
        application.close()


def _run_one_turn(application) -> int:
    voice = application.voice
    if voice is None or not (voice.input_available and voice.output_available):
        print(_VOICE_SETUP_NEEDED, file=sys.stderr)
        return 3

    # Warm the resident speech worker now, during the first capture, so
    # the spoken answer is not the model load.
    voice.speech_enabled = True
    voice.prewarm_speech()

    try:
        capture = _make_capture(application.settings, voice)
    except VoiceError as error:
        # The silence-watcher needs the local VAD model; without it a
        # hands-free capture could not know when to stop. Say so plainly.
        print(str(error), file=sys.stderr)
        return 4

    _chime(voice, "listen")
    transcript, terminal = capture()
    if terminal != "complete" or not transcript or is_transcription_junk(
        transcript
    ):
        # Nothing usable was heard — no turn, no invented request.
        _speak_phrase(voice, "I didn't catch that.")
        return 0

    application.session.stella.approval_provider = _make_approver(voice, capture)
    _chime(voice, "working")
    outcome = application.session.run_turn(transcript, spoken=True)
    if outcome.error_message is not None:
        _speak_phrase(voice, "Sorry, I couldn't do that.")
        return 1
    if outcome.cancelled or outcome.result is None:
        return 0
    _speak_result(voice, outcome)
    return 0


# ------------------------------------------------------------- resident mode


def run_voice_server() -> int:
    """Run the resident ear until told to stop; return a code.

    The codes mean what ``run_headless_voice``'s mean, plus 5: a live
    control socket already belongs to another server, and a second
    process says so instead of hijacking the microphone.
    """

    settings = resolve_settings()
    if settings is None:
        print(_NOT_CONFIGURED, file=sys.stderr)
        return 2
    application = build_application(serve_settings(settings))
    try:
        return _serve(application)
    finally:
        application.close()


def _serve(application) -> int:
    voice = application.voice
    if voice is None or not (voice.input_available and voice.output_available):
        print(_VOICE_SETUP_NEEDED, file=sys.stderr)
        return 3
    settings = application.settings
    opening = threading.Event()
    ending = threading.Event()
    shutdown = threading.Event()
    active = threading.Event()

    def handle(command: str) -> str:
        # Runs on the accept thread: it sets flags and nothing else.
        # No voice, tool, or model work ever happens on a stranger's
        # connection.
        if command == "stop":
            shutdown.set()
            return "stopping"
        if command == "toggle":
            if active.is_set():
                ending.set()
                return "closing"
            opening.set()
            return "opening"
        return "unknown"

    try:
        server = _ControlServer(control_path(), handle)
    except OSError as error:
        print(f"{_ALREADY_RUNNING} ({error})", file=sys.stderr)
        return 5
    try:
        # Bind first, warm second: the socket is what tells ``--toggle``
        # a server exists, and loading a speech model is the slow part.
        voice.speech_enabled = True
        tap = MicTap(command=capture_command(settings.wake_source))
        voice.attach_tap(tap)
        try:
            ear = build_wake_ear(settings, tap=tap)
        except VoiceError as error:
            print(str(error), file=sys.stderr)
            return 4
        try:
            wake = build_wake(settings, tap=tap)
        except VoiceError as error:
            # A missing spotter is said once and the button still opens
            # conversations: honest, and no ear spinning on nothing.
            print(str(error), file=sys.stderr)
            wake = None
        if wake is not None:
            wake.on_wake = opening.set
        voice.prewarm_speech()
        _conversation_loop(
            application,
            voice,
            _capture_from(ear, voice),
            wake,
            opening,
            ending,
            shutdown,
            active,
        )
        return 0
    finally:
        server.close()


def _conversation_loop(
    application,
    voice,
    capture: Callable[[], tuple[str, str]],
    wake,
    opening: threading.Event,
    ending: threading.Event,
    shutdown: threading.Event,
    active: threading.Event,
) -> None:
    """Wake or button -> conversation -> back to the ear. Forever.

    The always-open spotter is suspended while a conversation owns the
    microphone — Stella must never wake on her own voice — and re-armed
    only when she is idle again. The wait for an open is a polled event,
    not a blocking call on the ear: a faulted listener simply never
    calls back, and the toggle still works beside it.
    """

    while not shutdown.is_set():
        if wake is not None:
            wake.start()
        while not opening.is_set() and not shutdown.is_set():
            opening.wait(0.2)
        if shutdown.is_set():
            break
        opening.clear()
        ending.clear()
        if wake is not None:
            wake.stop()
        active.set()
        try:
            _run_session(application, voice, capture, ending, shutdown)
        finally:
            active.clear()
    if wake is not None:
        wake.stop()


def _run_session(
    application,
    voice,
    capture: Callable[[], tuple[str, str]],
    ending: threading.Event,
    shutdown: threading.Event,
) -> None:
    """Turn after turn until silence, a spoken stop, or the button.

    The approval seam gets ``_make_approver`` unchanged: conversation
    mode is a weaker trigger, never a weaker gate. Silence is the natural
    end of a spoken exchange, so a capture that hears nothing just
    closes the session — no turn, no invented request, nothing said.
    """

    application.session.stella.approval_provider = _make_approver(
        voice, capture
    )
    while not ending.is_set() and not shutdown.is_set():
        _chime(voice, "listen")
        transcript, terminal = capture()
        if terminal != "complete" or not transcript or is_transcription_junk(
            transcript
        ):
            return
        if _is_stop_phrase(transcript):
            _speak_phrase(voice, "Okay, I'm done here.")
            return
        _chime(voice, "working")
        outcome = application.session.run_turn(transcript, spoken=True)
        if outcome.error_message is not None:
            _speak_phrase(voice, "Sorry, I couldn't do that.")
        elif outcome.result is not None:
            _speak_result(voice, outcome)


def _is_stop_phrase(transcript: str) -> bool:
    """Whether the whole utterance is just a dismissal."""

    return (
        " ".join(re.findall(r"[a-z']+", transcript.casefold()))
        in _STOP_PHRASES
    )


def control_path() -> str:
    """The resident server's private doorbell, one per user.

    XDG_RUNTIME_DIR is the standard per-user scratch space (the system
    creates it 0700); the temp directory is the fallback and carries the
    uid in the name. The socket itself is 0600 either way.
    STELLA_VOICE_SOCKET is the test seam, not a shortcut-facing knob.
    """

    override = os.environ.get("STELLA_VOICE_SOCKET")
    if override:
        return override
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, f"stella-voice-{os.getuid()}.sock")


def _socket_alive(path: str) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


class _ControlServer:
    """One word per connection over a 0600 unix socket: the ear's doorbell.

    The protocol is exactly ``toggle`` and ``stop`` — no arguments, no
    transcripts, no payload that could smuggle in work a spoken approval
    would not already gate. The handler runs on the accept thread and
    only sets events; all voice, model and tool work stays on the main
    one. A live socket means another server already owns this run and is
    reported, never hijacked; a file left by a crashed run is cleared.
    """

    def __init__(self, path: str, on_command: Callable[[str], str]) -> None:
        if _socket_alive(path):
            raise OSError(f"a live server already holds {path}")
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self._path = path
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._socket.bind(path)
            os.chmod(path, 0o600)
            self._socket.listen(4)
        except OSError:
            self._socket.close()
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise
        self._on_command = on_command
        threading.Thread(
            target=self._accept, name="stella-voice-control", daemon=True
        ).start()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return  # the socket is closed; this thread's job is done
            with connection:
                try:
                    connection.settimeout(2.0)
                    request = connection.recv(64)
                    answer = self._on_command(
                        request.decode("utf-8", "replace").strip()
                    )
                    connection.sendall((answer + "\n").encode())
                except OSError:
                    continue

    def close(self) -> None:
        self._socket.close()
        with contextlib.suppress(OSError):
            os.unlink(self._path)


def _send_command(command: str, *, timeout: float = 2.0) -> str | None:
    """One word to the server, or None when nothing answered.

    None means "no server there", and the caller decides what that is
    worth: a toggle that starts one, a stop that says so. It is never
    retry-transparent — the command may have run before the answer was
    lost, and a blind retry could flip an open session closed.
    """

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(timeout)
            channel.connect(control_path())
            channel.sendall((command + "\n").encode())
            answer = channel.recv(64)
    except OSError:
        return None
    return answer.decode("utf-8", "replace").strip()


def _wait_for_server(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _socket_alive(control_path()):
            return True
        time.sleep(0.2)
    return False


def _spawn_server() -> bool:
    """Start ``voice --serve`` detached from this process.

    A server that started and failed must be diagnosable, so its stderr
    goes to a 0600-mode log beside the control socket — honest
    VoiceError and configuration lines only, never keys or transcripts.
    Unlike Stella's helper subprocesses, this child is *meant* to outlive
    its parent, so it gets its own session and deliberately no
    parent-death guard (``guarded_popen`` would kill it when the
    shortcut's process exits seconds later).
    """

    stack = contextlib.ExitStack()
    try:
        try:
            # The descriptor must be live at the fork so the child can
            # inherit it; ``stack`` closes this process's copy below.
            log = stack.enter_context(
                open(  # noqa: SIM115
                    control_path() + ".log",
                    "w",
                    encoding="utf-8",
                )
            )
            os.chmod(log.fileno(), 0o600)
        except OSError:
            log = None
        subprocess.Popen(
            [sys.executable, "-m", "stella.cli", "voice", "--serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log if log is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    finally:
        # The child keeps its inherited descriptor; this process's copy
        # goes away with the stack.
        stack.close()
    return True


def run_voice_toggle() -> int:
    """The keyboard shortcut's word: open a conversation, or close one.

    No server is not an error, it is "start one" — the owner's key is
    one press. A server that never came up is said to stderr, not
    swallowed: a dead key must be diagnosable, and the code says so.
    """

    if _send_command("toggle") is not None:
        return 0
    if not _spawn_server():
        print(_SERVER_START_FAILED, file=sys.stderr)
        return 4
    if not _wait_for_server(SERVER_START_WAIT_SECONDS):
        print(_SERVER_START_FAILED, file=sys.stderr)
        return 4
    if _send_command("toggle") is None:
        print(_SERVER_START_FAILED, file=sys.stderr)
        return 4
    return 0


def run_voice_stop() -> int:
    """Tell the resident server to let go of the microphone."""

    if _send_command("stop") is None:
        print(_NO_SERVER, file=sys.stderr)
        return 0
    print("Stella's voice session server is stopping.")
    return 0


def _make_capture(settings, voice) -> Callable[[], tuple[str, str]]:
    """A closure that records one utterance and returns (text, terminal).

    One microphone tap and one silence-watcher are built here and reused
    for every capture in the run (the request, and any approval answer),
    so the device is opened once and handed back exactly once. The
    resident server builds the tap itself — the wake listener needs a
    subscriber on the same handle — and reaches the same closure through
    :func:`_capture_from`.
    """

    tap = MicTap(command=capture_command(settings.wake_source))
    voice.attach_tap(tap)
    ear = build_wake_ear(settings, tap=tap)
    return _capture_from(ear, voice)


def _capture_from(ear, voice) -> Callable[[], tuple[str, str]]:
    """The capture closure over an already-built silence-watcher."""

    def capture() -> tuple[str, str]:
        heard: dict[str, str] = {}
        finished = threading.Event()

        def on_finish(kind: str) -> None:
            heard["kind"] = kind
            finished.set()

        ear.on_finish = on_finish
        try:
            voice.start_listening()
        except VoiceError:
            # The microphone could not be opened at all: read it exactly
            # as "nothing heard" rather than raising out of a shortcut.
            return "", "timeout"
        terminal = "timeout"
        try:
            ear.start()
            if finished.wait(timeout=CAPTURE_WAIT_SECONDS):
                terminal = heard.get("kind", "timeout")
        except VoiceError:
            ear.stop()
            voice.abandon_listening()
            return "", "timeout"
        ear.stop()
        if terminal != "complete":
            # A wake-less silence or an over-long run: nothing was heard,
            # so the recording is discarded rather than half-trusted.
            voice.abandon_listening()
            return "", terminal
        try:
            return voice.stop_and_transcribe(), terminal
        except VoiceError:
            # A transcription that failed or returned no words is not a
            # request to invent: no turn runs, and the caller says so.
            voice.abandon_listening()
            return "", "timeout"

    return capture


def _make_approver(
    voice, capture: Callable[[], tuple[str, str]]
) -> Callable[..., ToolApproval]:
    """Turn the pluggable approval seam into a spoken yes/no.

    The prompt is ``action_summary`` only — never the raw arguments and
    never the preview body — because a preview line can be a document's
    contents, and this is read aloud to a room.
    """

    def approve(
        request: ApprovalRequest, preview: ActionPreview | None = None
    ) -> ToolApproval:
        del preview  # display-only, and unsafe to speak
        summary = _spoken_summary(request)
        _speak_phrase(voice, f"Stella wants to {summary}. Say yes or no.")
        transcript, terminal = capture()
        approved = terminal == "complete" and _is_affirmative(transcript)
        return ToolApproval(request=request, approved=approved)

    return approve


def _spoken_summary(request: ApprovalRequest) -> str:
    """A single bounded line describing the action, safe to read aloud.

    ``action_summary`` is already argument-aware for known capabilities,
    but its unknown-capability fallback can echo the whole argument dict
    — which for a file write is the file's contents. Collapsing to one
    line and capping the length keeps a body from ever being spoken whole
    while still naming the action and its target.
    """

    return " ".join(action_summary(request).split())[:160]


def _is_affirmative(transcript: str) -> bool:
    """A clear spoken yes, and nothing that could be a no. Fail closed."""

    words = set(re.findall(r"[a-z']+", transcript.casefold()))
    if words & _DENY_WORDS:
        return False
    return bool(words & _APPROVE_WORDS)


def _speak_phrase(voice, text: str) -> None:
    _voice_and_dispose(voice, lambda: voice.synthesize_phrase(text))


def _speak_result(voice, outcome) -> None:
    _voice_and_dispose(voice, lambda: voice.synthesize(outcome.result))


def _voice_and_dispose(voice, produce: Callable[[], str]) -> None:
    """Produce one artifact, play it to the end, and remove it.

    A speech failure is reported on stderr and swallowed: the turn already
    happened (or was honestly refused), and a silent speaker is not a
    reason to crash a shortcut-triggered process.
    """

    try:
        path = produce()
    except VoiceError as error:
        print(str(error), file=sys.stderr)
        return
    try:
        voice.play(path)
    except VoiceError as error:
        print(str(error), file=sys.stderr)
    finally:
        voice.dispose_artifact(path)


def _chime(voice, kind: str) -> None:
    """Play a short tone so the user knows the microphone just opened.

    Best-effort: a missing chime never stops a turn that the peripherals
    can otherwise carry. The tones are generated with the standard
    library, so this adds no asset and no dependency.
    """

    tones = _CHIME_TONES.get(kind)
    if not tones:
        return
    # mkstemp creates the file exclusively (no symlink/TOCTOU race, and the
    # deprecated mktemp would leak the name); the WAV is written straight
    # through the returned descriptor so the fd is never reopened by name.
    descriptor, path = tempfile.mkstemp(prefix="stella-chime-", suffix=".wav")
    try:
        try:
            _write_chime(descriptor, tones)
        except OSError:
            return
        try:
            voice.play(path)
        except VoiceError:
            pass
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _write_chime(
    descriptor: int, tones: tuple[tuple[float, float], ...], rate: int = 16000
) -> None:
    """Render a tiny sine WAV into an already-created fd, amplitude-shaped so
    it never clicks. ``wave.open`` on a file object does not close it, so the
    owning ``os.fdopen`` context below does."""

    with os.fdopen(descriptor, "wb") as raw, wave.open(raw, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for frequency, duration in tones:
            total = int(rate * duration)
            for index in range(total):
                envelope = math.sin(math.pi * index / max(total - 1, 1))
                sample = int(
                    12000
                    * envelope
                    * math.sin(2.0 * math.pi * frequency * index / rate)
                )
                frames += sample.to_bytes(2, "little", signed=True)
        handle.writeframes(bytes(frames))
