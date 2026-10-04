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

The security posture that must never loosen here:
  * raw tool arguments are never spoken — a file-write body would read a
    document aloud — only ``action_summary``'s bounded description;
  * a saved key or the transcript is never printed; failures are honest
    lines, never a fake success;
  * exactly one microphone handle is open at a time (``MicTap`` shared
    by the recorder and the silence-watcher), and it is released on exit.
"""

from __future__ import annotations

import dataclasses
import math
import os
import re
import sys
import tempfile
import threading
import wave
from collections.abc import Callable

from stella.app import build_application, build_wake_ear
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

__all__ = ["run_headless_voice"]

#: Longest a capture may run before the endpoint is considered lost. The
#: wake endpoint caps real speech at 20 s and times out silence at 8 s,
#: so waiting past its own horizon only ever means a dead microphone —
#: which is then read as "nothing heard", not a hang.
CAPTURE_WAIT_SECONDS = 22.0

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


def _make_capture(settings, voice) -> Callable[[], tuple[str, str]]:
    """A closure that records one utterance and returns (text, terminal).

    One microphone tap and one silence-watcher are built here and reused
    for every capture in the run (the request, and any approval answer),
    so the device is opened once and handed back exactly once. The recorder
    reads that same tap, so watching silence and recording words never
    become two handles on one microphone.
    """

    tap = MicTap(command=capture_command(settings.wake_source))
    voice.attach_tap(tap)
    ear = build_wake_ear(settings, tap=tap)

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
