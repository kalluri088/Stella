"""The desktop seam: the data and the protocols, and no OS code at all.

Everything an adapter implements, and everything the three tools in
``stella.desktop.tools`` may rely on, lives here. The rules below are the
invariants the Hyprland measurements forced (research reports 03, 11,
13); an adapter that breaks one of them is broken, however tidy its code:

1. **Reads trust shape, never exit codes.** ``hyprctl`` answers garbage
   with rc 0 on stdout; a compositor can say nothing useful and still
   exit 0. So an unusable answer and an empty answer are different facts
   and are never collapsed: ``None`` means "the answer was unusable",
   ``[]`` means "definitely no windows".
2. **Acts are followed by an independent re-query.** ``WindowActivator``
   returns True only after the backend has asked the compositor again.
3. **Keystrokes require a verified focus** — see the ``KeySender`` and
   ``stella.desktop.tools`` contract.
4. **Pixels never persist; OCR text is masked and bounded.** The capture
   surface hands PNG bytes straight to the recognizer and drops them.
5. **An unusable environment registers no tools** — ``Desktop`` is only
   ever built by a probe that checked its own session marker and its own
   binaries.

The window id is deliberately *opaque*: Hyprland's is a hex address,
X11's a window id, sway's a container id. Nothing outside an adapter may
assume a shape, which is why the tools only bound the string and the
adapter validates it against its own compositor before use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# ---------------------------------------------------------------------------
# the bounds every layer must agree on
# ---------------------------------------------------------------------------

MAX_WINDOW_ID_CHARS = 64
"""The backend-neutral length bound on an opaque window id."""

MAX_SCREEN_TEXT_CHARS = 6_000
"""The privacy bound on OCR text reaching the model (report 11)."""

MAX_KEY_TEXT_CHARS = 2_000
"""One bounded typing action."""

OCR_WINDOW = "window"
OCR_SCREEN = "screen"
"""Capture hints for :meth:`Recognizer.recognize`.

The tools say *what kind of picture* they are handing over; only the
recognizer knows which OCR setting that implies (tesseract's ``--psm 6``
for a uniform window block, ``--psm 11`` for a sparse whole-screen pass —
the measured choice from report 11). No tool argument names a binary.
"""


# ---------------------------------------------------------------------------
# the one failure type family: messages are safe to show the model
# ---------------------------------------------------------------------------


class DesktopUnavailable(Exception):
    """Base for adapter failures; ``str(exc)`` is displayable as-is."""


class CaptureUnavailable(DesktopUnavailable):
    """No PNG could be produced, for a reason worth telling the model."""


class RecognizeUnavailable(DesktopUnavailable):
    """Local OCR could not run or could not read the picture."""


class SendUnavailable(DesktopUnavailable):
    """Keystrokes could not be delivered to the focused surface."""


# ---------------------------------------------------------------------------
# the data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Region:
    """A rectangle in screen coordinates, in the backend's own pixels.

    Backend-neutral on purpose: grim wants ``X,Y WxH``, ImageMagick's
    ``import`` wants ``-geometry WxH+X+Y``, a portal wants extents. Each
    adapter formats this for its own capture tool; the string never
    appears above the adapter.
    """

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Window:
    """One window, as much as every desktop can describe one.

    ``id`` is the opaque, backend-validated handle the tools pass back
    (see the module docstring). ``region`` is optional because plenty of
    compositors will not hand over geometry cheaply: an adapter that
    cannot report it leaves it ``None``, and ``screen_read`` then refuses
    its window scope rather than quietly widening it to the whole screen.
    """

    id: str
    class_name: str
    title: str
    pid: int
    workspace: str
    region: Region | None = None


# ---------------------------------------------------------------------------
# the capabilities an adapter provides
# ---------------------------------------------------------------------------


class WindowReader(Protocol):
    """Asks the compositor which windows exist and which one is focused."""

    def active_window(self) -> Window | None:
        """The focused window; ``None`` when the answer was unusable.

        ``None`` is *not* "there is no focused window" — it means the
        compositor did not answer in a form this adapter trusts.
        """

    def windows(self) -> list[Window] | None:
        """Every window, or ``None`` when the answer was unusable.

        Never collapse the two: the rc-0-with-garbage trap (report 03) is
        exactly the case where an unusable answer would be read as
        "no windows are open" and silently refuse a real target.
        """


class WindowActivator(Protocol):
    """Moves focus, and proves it moved."""

    def activate(self, window_id: str) -> bool:
        """Focus one window by its opaque id.

        Must return True only when the backend has **re-queried** the
        compositor and confirmed this window is now the active one. A
        dispatch that the compositor merely accepted is not a success;
        the return code of a compositor command proves nothing (rule 1).
        The id is model-influenced text, so the adapter validates it
        against its own id shape and refuses anything else without
        spawning anything.
        """


class ScreenCapture(Protocol):
    """One PNG of a region, or of the whole screen."""

    def capture(self, region: Region | None) -> bytes:
        """PNG bytes for ``region``; ``None`` means the whole screen.

        Raises :class:`CaptureUnavailable` (message safe to show the
        model) on any failure, including a picture that is not a PNG.
        The bytes are handed straight to a recognizer and dropped; they
        are never written to disk.
        """


class KeySender(Protocol):
    """Types literal text into whatever currently has focus."""

    def send_text(self, text: str) -> None:
        """Delivers to the *focused* surface only.

        Raises :class:`SendUnavailable` when the keystrokes were not
        delivered. Note what this cannot say: whether the application
        *did* anything with the text is unknowable from a compositor, so
        the tool reports that honestly rather than claiming success.
        Because delivery lands wherever focus is, callers must have a
        verified focus first.
        """


class Recognizer(Protocol):
    """Turns PNG bytes into text, locally."""

    def recognize(self, png: bytes, hint: str) -> str:
        """OCR one picture; ``hint`` is :data:`OCR_WINDOW`/
        :data:`OCR_SCREEN`.

        Raises :class:`RecognizeUnavailable` on failure. Empty text is a
        valid answer ("the screen shows nothing readable"), not an error.
        """


# ---------------------------------------------------------------------------
# what the registry hands the tools
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Desktop:
    """One usable desktop: every capability an adapter could vouch for.

    A ``Desktop`` exists only when its probe was sure of itself, so the
    tools never have to re-derive "is there a desktop here".
    ``session_note`` names the exact session being touched (for Hyprland,
    the instance signature) and the tools print it in every approval
    preview — a dialog that says "type into this window" must say *which*
    compositor session it means.
    """

    name: str
    reader: WindowReader
    activator: WindowActivator
    capture: ScreenCapture
    keys: KeySender
    recognizer: Recognizer
    session_note: str


def window_id_usable(value: object) -> bool:
    """The backend-neutral half of the window-id contract.

    Only the adapter knows an id's shape, so the tools check that the
    value is a short, plain, single-token string and leave the real
    validation to the adapter — which must repeat it before the id goes
    anywhere near a command line or a compositor scripting API.
    """

    if not isinstance(value, str) or not value or len(value) > MAX_WINDOW_ID_CHARS:
        return False
    if not value.isprintable():
        return False
    return not any(character.isspace() for character in value)


__all__ = [
    "MAX_KEY_TEXT_CHARS",
    "MAX_SCREEN_TEXT_CHARS",
    "MAX_WINDOW_ID_CHARS",
    "OCR_SCREEN",
    "OCR_WINDOW",
    "CaptureUnavailable",
    "Desktop",
    "DesktopUnavailable",
    "KeySender",
    "RecognizeUnavailable",
    "Recognizer",
    "Region",
    "ScreenCapture",
    "SendUnavailable",
    "Window",
    "WindowActivator",
    "WindowReader",
    "window_id_usable",
]
