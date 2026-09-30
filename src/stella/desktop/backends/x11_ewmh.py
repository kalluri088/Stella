"""The X11/EWMH adapter — root-window properties read, focus proven by re-query.

Every rule below is either a fact measured on a machine or an explicitly
labelled ``UNVERIFIED`` item that a machine still owes. Reads go through
``xprop`` (never ``subprocess`` directly), acts through ``xdotool``, pixels
through ImageMagick ``import``; the shared :class:`Runner` runs all of them
from an argv list, so no shell ever sees a window id.

MEASURED on this box's XWayland root (``DISPLAY=:0``, Hyprland 0.56.2,
``xprop`` from x11-utils; read-only queries only, nothing was focused,
moved or typed):

* ``xprop -root _NET_SUPPORTING_WM_CHECK`` →
  ``_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x200005``, and that id
  answers as a real window: ``xprop -id 0x200005 _NET_WM_NAME`` →
  ``_NET_WM_NAME(UTF8_STRING) = "Hyprland :D"``. So "the check names a
  window that talks back" is this probe's proof of an EWMH manager.
* Window ids are ``0x`` + lowercase hex, never decimal, and follow
  ``window id #`` — ``xprop -notype -root _NET_CLIENT_LIST`` →
  ``_NET_CLIENT_LIST: window id # 0xa00033``; the zero-window form is
  ``_NET_CLIENT_LIST: window id # `` with nothing after the marker.
* An unset *known* property answers ``NAME:  not found.`` and an unknown
  name answers ``NAME:  no such atom on any window.`` — **both at rc 0**,
  so the words and not the exit code are the answer (obligation 1).
* ``-notype`` only strips the ``(TYPE)`` part; string lines stay
  ``NAME = "text"`` (measured: ``WM_CLASS = "!toplevel", "Toplevel"``,
  ``_NET_WM_NAME = "Welcome to Stella"``) and window lines stay
  ``NAME: window id # 0x…``. Parsing one line format is why this adapter
  always passes ``-notype``.
* **The active-window id can be dead.** ``_NET_ACTIVE_WINDOW`` answered
  ``0xa02259``, then ``xprop -id 0xa02259 _NET_WM_NAME`` failed with
  ``BadWindow (invalid Window parameter)`` at **rc 1**, the error on stderr
  and a *half-written* property name on stdout. A focused-window read that
  cannot describe its own id is therefore ``None`` ("the desktop did not
  answer") — never a synthetic window, never a crash.
* The list is live and hints are sparse: ``_NET_CLIENT_LIST`` was empty at
  one query and held ``0xa00033`` minutes later, and that window carried no
  ``_NET_WM_PID``, no ``_NET_WM_DESKTOP`` and no ``_NET_WM_WINDOW_TYPE``
  (Hyprland maps some XWayland windows as xdg-external and does not fill
  those hints). A second, ordinary client did answer them:
  ``_NET_WM_PID = 31990`` (unquoted **decimal**) and ``WM_CLASS =
  "spotify", "Spotify"`` (instance first, class second — which is why the
  adapter takes the *last* quoted field as the class). So an absent pid or
  desktop is a *normal* answer that gets a default, while an absent
  ``_NET_CLIENT_LIST`` means "this manager does not speak EWMH" and gets
  ``None``.
* When no X window has focus the answer is the NoneWindow:
  ``_NET_ACTIVE_WINDOW: window id # 0x0`` — and ``xprop`` itself refuses to
  query it (``xprop: error: Invalid window id format: 0x0.``, rc 1). The
  adapter therefore reads ``0x0`` as "no focused window to describe" and
  never spawns for it; the contract cannot express that as anything but
  ``None``, which is also what a confused tool answers, so the two stay
  honestly indistinguishable *to the caller*.
* ``import`` here is ImageMagick 7.1.2-31 and produced **no image at all**
  from the XWayland root: ``-window root`` with ``png:-``, ``PNG:-``, ``-``
  or ``/dev/stdout`` each exited 1 with ``import: missing an image filename
  `png:-' @ error/import.c/ImportImageCommand/1291`` — and the same
  sentence appears with no filename given at all (bare ``import`` blames
  ``import``), which proves it reports a failed capture while naming the
  last argument. ``xwd`` is not installed here. So capture on this machine
  is a ``CaptureUnavailable`` path, and no pixels ever touched disk.

WHAT THIS DOES NOT PROVE: this is XWayland inside a Wayland session, not an
X11 window-manager session. An EWMH reply here is evidence about **tool
output shape only** — never about focus behaviour, per-window hints,
geometry or capture, because the thing answering is Hyprland's XWayland
bridge and the real input focus lives on the Wayland side. On this laptop
``probe`` registers nothing: ``xdotool`` is absent (and must not be
installed), the session is marked Wayland, and the seam still requires a
complete ``Desktop``. That is the correct honest outcome, not a bug.

UNVERIFIED — measured elsewhere, by someone with a real X11 session:

* ``xdotool``'s window-argument grammar. This adapter hands it **decimal**
  (``str(int(id, 16))``) derived from a validated ``0x…`` id, because
  decimal is what ``xdotool search`` prints; whether it also accepts the
  ``0x`` spelling was not testable here, and nothing invalid is converted.
* Whether ``xdotool windowactivate --sync`` moves input focus on a given WM.
  Some managers honour the request and refuse the focus change, which is
  precisely why ``activate`` ignores the command's own success and
  re-queries ``_NET_ACTIVE_WINDOW`` (obligation 2). Whether
  ``xdotool getwindowfocus`` follows keyboard or input focus is unmeasured
  too, so this adapter never uses it.
* ``xdotool type``'s option parsing: whether ``--`` ends its options, what
  it does with the full Unicode range and multi-key compose sequences, and
  how many characters become a keystroke storm. Until measured, text that
  could be read as an option (leading ``-``) is refused outright.
* The separator of a **multi-entry** ``WINDOW`` list: zero and one entries
  were measured; ``, `` between ids is assumed from ``WM_CLASS`` and
  ``_NET_SUPPORTED`` and never proven.
* Which windows ``_NET_CLIENT_LIST`` omits (override-redirect, dock,
  splash) and whether its order is per-desktop or global.
* ``import``'s PNG-to-stdout spelling and its region syntax (``-crop
  WxH+X+Y``, which differs from grim's measured ``X,Y WxH``). This build
  captured nothing, so both are unproven — a real session must confirm
  that ``png:-`` writes to stdout and never creates a file, because the
  adapter's argv contains no path to delete.
* Window geometry: ``xwininfo`` is absent here and ``xprop`` does not hand
  over a box, so ``Window.region`` is always ``None`` and ``screen_read``
  refuses its window scope rather than quietly widening it to the whole
  screen (obligation 4). A real X11 session should decide whether to add a
  geometry source, not a wider capture.
* Whether a manager ever answers ``_NET_ACTIVE_WINDOW`` or
  ``_NET_SUPPORTING_WM_CHECK`` with a list instead of one id; anything but
  exactly one id is treated as unusable here.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Mapping

from stella.desktop.capabilities import (
    MAX_KEY_TEXT_CHARS,
    CaptureUnavailable,
    Desktop,
    Region,
    SendUnavailable,
    Window,
)
from stella.desktop.ocr import TesseractRecognizer
from stella.desktop.runner import (
    Completed,
    Runner,
    error_detail,
    subprocess_runner,
)

WINDOW_ID_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
"""X11's window id as ``xprop`` prints it: ``0x`` plus hex digits.

The canonical spelling is the lowercase ``0x…`` string the tool itself
emits (measured above), because that is exactly what ``windows()`` hands
the tools and what the tools hand back; reformatting on the way out would
only invent a second spelling to mismatch on. Anything else is rejected
*here*, before it reaches argv: this id is the one model-influenced string
this adapter puts into a command line, and a strict shape check plus an
integer conversion is what stands between a hostile window title and a
compositor action.
"""

XPROP_TIMEOUT_SECONDS = 5.0
ACTIVATE_TIMEOUT_SECONDS = 10.0
CAPTURE_TIMEOUT_SECONDS = 10.0
KEYSEND_TIMEOUT_SECONDS = 15.0

NO_DISPLAY_MESSAGE = (
    "No X11 display is set for this session, so there is no desktop to "
    "read or act on."
)

NO_FOCUS_WINDOW_ID = "0x0"
"""EWMH's NoneWindow, measured as the ``_NET_ACTIVE_WINDOW`` answer when
nothing has focus. ``xprop`` refuses to query it, so it is recognised here
rather than sent out as a doomed read.
"""

WAYLAND_MARKERS = ("WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK")
"""Markers that the user is really typing into a Wayland compositor.

Any one of them vetoes this probe. XWayland hands every X11 client a
perfectly good ``$DISPLAY``, and a stale ``XDG_SESSION_TYPE=x11`` in a shell
profile must not win a probe against the compositor in front of the user.
"""


class _Sentinel:
    """A named non-value, so "absent" and "unusable" stay two separate facts."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return self.name


ABSENT = _Sentinel("ABSENT")
"""The property is not set there — a real answer with real defaults."""

UNUSABLE = _Sentinel("UNUSABLE")
"""The tool did not answer in a form trusted — never collapsed into ``ABSENT``."""

ReadResult = str | _Sentinel

_LINE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_]+)\s*(?P<sep>[:=])\s*(?P<rest>.*)$")
_ABSENT_RE = re.compile(r"^(?:not found|no such atom on any window)\.?$")
_WINDOW_MARKER = "window id #"
_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_INT_RE = re.compile(r"^-?\d+$")
_UNESCAPE = {"n": "\n", "t": "\t", "r": "\r"}

_X_CLIENT_ENV_KEYS = ("DISPLAY", "XAUTHORITY", "HOME", "XDG_RUNTIME_DIR")
"""What an X client needs in order to find *this* server.

The runner replaces the whole environment when an ``env`` mapping is given,
so an X client handed nothing would also lose ``XAUTHORITY`` and the
``$HOME/.Xauthority`` fallback and fail to authenticate. Passing exactly
these keys (rather than the full environment) also keeps a stray ``DISPLAY``
inherited from elsewhere out of the command.
"""


def _canonical_window_id(value: object) -> str | None:
    """Validate an opaque id and reduce it to the one spelling we emit."""

    if not isinstance(value, str) or WINDOW_ID_RE.match(value) is None:
        return None
    return f"0x{int(value, 16):x}"


def _quoted_fields(value: str) -> list[str]:
    """The quoted tokens of an xprop string list; ``[]`` when none match.

    xprop escapes an embedded quote as ``\\"`` and a newline as ``\\n``, so
    a title can never smuggle a second line into this parse.
    """

    return [
        re.sub(r"\\(.)", lambda m: _UNESCAPE.get(m.group(1), m.group(1)), match)
        for match in _QUOTED_RE.findall(value)
    ]


def _one_line(text: str, limit: int) -> str:
    """Server-supplied text, flattened and shortened for a dialog."""

    flat = " ".join(text.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


class X11Ewmh:
    """One X11 session, as reader, activator, capturer and typist."""

    def __init__(
        self,
        display: str | None,
        *,
        session_env: Mapping[str, str] | None = None,
        runner: Runner = subprocess_runner,
        xprop: str = "xprop",
        xdotool: str = "xdotool",
        capture_tool: str = "import",
    ) -> None:
        self.display = display or None
        self.session_env = {
            key: value
            for key, value in (session_env or {}).items()
            if key in _X_CLIENT_ENV_KEYS and value
        }
        if self.display:
            self.session_env.setdefault("DISPLAY", self.display)
        self._runner = runner
        self.xprop = xprop
        self.xdotool = xdotool
        self.capture_tool = capture_tool

    def required_binaries(self) -> tuple[str, ...]:
        return (self.xprop, self.xdotool, self.capture_tool)

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> X11Ewmh:
        session = {key: env.get(key, "") for key in _X_CLIENT_ENV_KEYS}
        return cls(env.get("DISPLAY") or None, session_env=session)

    # -------------------------------------------------------------- reads

    def _spawn(self, argv: list[str] | None, timeout: float) -> Completed | None:
        """A run that could not even start is an unusable answer, not a crash.

        Folding ``OSError`` (and "no display", and "no spawn allowed") into a
        shaped failure keeps rule 1 in charge of the read path: the shape
        check turns it into ``UNUSABLE``, which is the honest meaning of "the
        binary disappeared since registration".
        """

        if argv is None or self.display is None:
            return None
        try:
            return self._runner(argv, timeout=timeout, env=self.session_env)
        except OSError as failure:
            return Completed(1, str(failure).encode("utf-8", "replace"), b"")

    def _xprop_argv(self, target: str | None, name: str) -> list[str] | None:
        argv = [self.xprop, "-notype"]
        if target is None:
            argv.append("-root")
        else:
            canonical = _canonical_window_id(target)
            if canonical is None or canonical == NO_FOCUS_WINDOW_ID:
                # Never spawn on an id we would not have printed ourselves,
                # nor on the NoneWindow: measured, xprop refuses to query it.
                return None
            argv += ["-id", canonical]
        argv.append(name)
        return argv

    def _read(self, name: str, target: str | None = None) -> ReadResult:
        """The value text for one property, or :data:`ABSENT`/ :data:`UNUSABLE`.

        Shape decides, never the exit code alone (obligation 1): rc 0 with an
        unrecognised line is ``UNUSABLE``, and the measured ``not found`` /
        ``no such atom on any window`` wording is ``ABSENT`` **at rc 0**. A
        nonzero rc only says the connection or the window failed, which is
        ``UNUSABLE`` whatever stdout claims.
        """

        result = self._spawn(self._xprop_argv(target, name), XPROP_TIMEOUT_SECONDS)
        if result is None or result.returncode != 0:
            return UNUSABLE
        try:
            text = result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return UNUSABLE
        for line in text.splitlines():
            match = _LINE_RE.match(line.strip())
            if match is None or match["name"] != name:
                continue
            rest = match["rest"]
            if _ABSENT_RE.match(rest):
                return ABSENT
            if match["sep"] == ":":
                if not rest.startswith(_WINDOW_MARKER):
                    return UNUSABLE
                return rest[len(_WINDOW_MARKER) :].strip()
            return rest
        # Something answered, but not about the property asked: unusable.
        return UNUSABLE

    def _ids(self, value: ReadResult) -> list[str] | None:
        """Split a WINDOW list; a foreign token makes the whole answer unusable.

        An empty value is the measured zero-window form and gives ``[]``;
        one bad token gives ``None``, because half a trusted list is not a
        list — it would silently hide a window from target resolution.
        """

        if value is ABSENT or value is UNUSABLE or not isinstance(value, str):
            return None
        if not value:
            return []
        ids: list[str] = []
        for token in value.split(","):
            canonical = _canonical_window_id(token.strip())
            if canonical is None:
                return None
            ids.append(canonical)
        return ids

    def _text(self, name: str, target: str | None, *, last_field: bool = False) -> ReadResult:
        """One quoted string property: text, :data:`ABSENT`, or :data:`UNUSABLE`."""

        value = self._read(name, target)
        if value is ABSENT or value is UNUSABLE:
            return value
        fields = _quoted_fields(value)
        if not fields:
            return UNUSABLE
        return fields[-1] if last_field else fields[0]

    def _optional_int(self, name: str, target: str | None, default: int) -> int | None:
        """An integer hint where absence is normal (measured: no ``_NET_WM_PID``)."""

        value = self._read(name, target)
        if value is ABSENT:
            return default
        if value is UNUSABLE or not isinstance(value, str):
            return None
        text = value.strip()
        return int(text) if _INT_RE.match(text) else None

    def active_window(self) -> Window | None:
        value = self._read("_NET_ACTIVE_WINDOW")
        if value is ABSENT or value is UNUSABLE:
            return None
        ids = self._ids(value)
        if ids is None or len(ids) != 1:
            # Zero ids means "no window has focus" on some managers, which
            # this contract cannot express as a window; more than one id is
            # a shape this adapter has not measured and will not guess. The
            # measured ``0x0`` NoneWindow is a one-element list that
            # ``_describe`` answers without spawning anything.
            return None
        return self._describe(ids[0])

    def windows(self) -> list[Window] | None:
        """The managed windows, or ``None`` when the manager did not answer.

        ``xdotool search`` is deliberately not used for reads: it is absent
        on this box, and it is documented to exit **0 with empty output**
        when nothing matches — the inverse of ``hyprctl``'s rc-0-with-garbage
        trap, where "empty" would have to mean "definitely none" and a
        confused tool would have to be told apart by a second signal. The
        read path therefore stays on ``xprop``, where the two cases have
        different *wording*: an unset ``_NET_CLIENT_LIST`` is ``not found``
        (→ ``None``, "this manager does not speak EWMH") and a set-but-empty
        one is ``window id # `` (→ ``[]``, "nothing is managed right now"),
        both measured above.
        """

        ids = self._ids(self._read("_NET_CLIENT_LIST"))
        if ids is None:
            return None
        if not ids:
            return []
        windows = [self._describe(window_id) for window_id in ids]
        if any(window is None for window in windows):
            # The manager listed a window it cannot describe: that is not a
            # list to vouch for, so the answer is no list at all.
            return None
        return [window for window in windows if window is not None]

    def _describe(self, window_id: str) -> Window | None:
        """One window from its EWMH/ICCCM properties; ``None`` if unusable."""

        title = self._text("_NET_WM_NAME", window_id)
        if title is ABSENT:
            title = self._text("WM_NAME", window_id)
        if title is UNUSABLE or title is None:
            return None
        # ICCCM's WM_CLASS is instance then class (measured:
        # ``"!toplevel", "Toplevel"``); the class is the stable label
        # Hyprland reports as ``class``, so take the last quoted field.
        class_name = self._text("WM_CLASS", window_id, last_field=True)
        if class_name is UNUSABLE or class_name is None:
            return None
        pid = self._optional_int("_NET_WM_PID", window_id, 0)
        # An EWMH desktop index is optional per window; "" says "this manager
        # does not track windows by desktop", which is not "no desktop".
        desktop = self._optional_int("_NET_WM_DESKTOP", window_id, -1)
        if pid is None or desktop is None:
            return None
        return Window(
            id=window_id,
            class_name="" if class_name is ABSENT else class_name,
            title="" if title is ABSENT else title,
            pid=pid,
            workspace="" if desktop < 0 else str(desktop),
            # No geometry source here: xwininfo is absent and xprop gives no
            # box, so the tools refuse a window scope instead of widening it.
            region=None,
        )

    # --------------------------------------------------------------- acts

    def activate(self, window_id: str) -> bool:
        """Ask for focus, then believe only the re-queried answer."""

        canonical = _canonical_window_id(window_id)
        if canonical is None or canonical == NO_FOCUS_WINDOW_ID:
            # The NoneWindow is not a window to focus, and a foreign id
            # shape never reaches a command line (obligation 5).
            return False
        if self.display is None:
            return False
        # The request's own reply is ignored on purpose: a manager can accept
        # the request and still refuse the focus change.
        self._spawn(
            [self.xdotool, "windowactivate", "--sync", str(int(canonical, 16))],
            ACTIVATE_TIMEOUT_SECONDS,
        )
        active = self.active_window()
        return active is not None and active.id == canonical

    # ------------------------------------------------------------ external

    @staticmethod
    def _crop(region: Region) -> str:
        # ImageMagick's geometry, not grim's: ``WxH+X+Y``. UNVERIFIED on this
        # box — see the module docstring; a wrong form fails loudly below.
        return f"{region.width}x{region.height}+{region.x}+{region.y}"

    def capture(self, region: Region | None) -> bytes:
        if self.display is None:
            raise CaptureUnavailable(NO_DISPLAY_MESSAGE)
        # ``png:-`` is the stdout target and no argument here is ever a path,
        # so pixels cannot land on disk (obligation 4). Measured: this
        # ImageMagick build produced no image from the XWayland root by any
        # spelling, so on this machine the call raises below with the tool's
        # own words instead of quietly trying a file-and-delete fallback.
        argv = [self.capture_tool, "-window", "root"]
        if region is not None:
            argv += ["-crop", self._crop(region)]
        argv.append("png:-")
        try:
            result = self._runner(
                argv, timeout=CAPTURE_TIMEOUT_SECONDS, env=self.session_env
            )
        except OSError as failure:
            raise CaptureUnavailable(
                f"Screen capture failed ({self.capture_tool} could not run: {failure})."
            ) from failure
        if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
            raise CaptureUnavailable(
                f"Screen capture failed ({self.capture_tool} could not write a PNG to "
                "stdout; nothing was saved to disk). "
                + error_detail(result, label="Capture tool said")
            )
        return result.stdout

    def send_text(self, text: str) -> None:
        """Type into the focused surface only — the caller verified the focus."""

        if self.display is None:
            raise SendUnavailable(NO_DISPLAY_MESSAGE)
        if not text:
            raise SendUnavailable("There was no text to type.")
        if len(text) > MAX_KEY_TEXT_CHARS:
            raise SendUnavailable(
                f"The text is too long for one bounded typing action (over "
                f"{MAX_KEY_TEXT_CHARS} characters); nothing was typed."
            )
        if text.startswith("-"):
            # Whether xdotool stops reading options at ``--`` is UNVERIFIED
            # (the binary is absent here), so text that could be parsed as a
            # flag is refused rather than risk becoming a command.
            raise SendUnavailable(
                "The text begins with '-', which this xdotool's option parser "
                "might read as a flag; nothing was typed."
            )
        try:
            result = self._runner(
                [self.xdotool, "type", "--clearmodifiers", text],
                timeout=KEYSEND_TIMEOUT_SECONDS,
                env=self.session_env,
            )
        except OSError as failure:
            raise SendUnavailable(
                f"The keystrokes were not delivered ({self.xdotool} could not "
                f"run: {failure})."
            ) from failure
        if result.returncode != 0:
            raise SendUnavailable(
                f"The keystrokes were not delivered ({self.xdotool} failed). "
                + error_detail(result, label="Window manager said")
            )

    # --------------------------------------------------------------- probe

    def wm_check(self) -> tuple[str, str] | None:
        """The EWMH proof: the check window exists and names its manager.

        Measured here: root ``_NET_SUPPORTING_WM_CHECK`` → ``0x200005``, and
        that window answers ``_NET_WM_NAME`` = ``"Hyprland :D"``. An
        environment variable is a hint; this answer is proof (registry rule 1).
        """

        ids = self._ids(self._read("_NET_SUPPORTING_WM_CHECK"))
        if ids is None or len(ids) != 1:
            return None
        name = self._text("_NET_WM_NAME", ids[0])
        if name is UNUSABLE or name is None:
            return None
        return ids[0], "" if name is ABSENT else name


def probe(
    env: Mapping[str, str],
    runner: Runner = subprocess_runner,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> Desktop | None:
    """This machine's X11/EWMH desktop, or ``None`` when it is not one.

    Gated cheapest first, so a session that is not X11 costs no spawn at all:

    1. ``XDG_SESSION_TYPE`` must say ``x11`` **and** ``$DISPLAY`` be set.
    2. Any Wayland marker vetoes the probe (see :data:`WAYLAND_MARKERS`).
    3. Every binary it will spawn must exist — ``xprop``, ``xdotool``, the
       capture tool and ``tesseract``. Focus and typing have no substitute
       here (no ``wmctrl``, no Python Xlib, no new dependency) and the seam
       requires a complete :class:`Desktop`, so one missing tool means no
       tools at all: a tool that can only fail is worse than no tool.
    4. Only then ask the display whether it is EWMH-compliant at all.
    """

    if (env.get("XDG_SESSION_TYPE") or "").strip().lower() != "x11":
        return None
    if not (env.get("DISPLAY") or "").strip():
        return None
    if any((env.get(marker) or "").strip() for marker in WAYLAND_MARKERS):
        return None

    x11 = X11Ewmh.from_environment(env)
    recognizer = TesseractRecognizer(runner)
    needed = (*x11.required_binaries(), *recognizer.required_binaries())
    if any(which(binary) is None for binary in needed):
        return None
    checked = x11.wm_check()
    if checked is None:
        return None
    manager_name = checked[1]
    return Desktop(
        name="x11-ewmh",
        reader=x11,
        activator=x11,
        capture=x11,
        keys=x11,
        recognizer=recognizer,
        # The approval dialog prints this: which exact X server is about to
        # be read or typed into, plus what that server calls its manager.
        # The name is server-supplied text, so it is flattened and bounded.
        session_note=(
            f"X11 display {x11.display}"
            + (
                f", window manager {_one_line(manager_name, 60)}"
                if manager_name
                else ""
            )
        ),
    )


__all__ = ["WAYLAND_MARKERS", "WINDOW_ID_RE", "X11Ewmh", "probe"]
