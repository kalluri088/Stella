"""The Hyprland adapter — a read-trusts-JSON, act-then-re-query client.

Every rule in this module is a measurement on the real machine (research
reports 03, 11, 12/13), not a style choice. The next agent to write an
adapter should treat this file as the standard of evidence: each quirk
below was found by being wrong first.

* ``hyprctl`` answers garbage with **rc 0 and "unknown request" on
  stdout**, and real dispatch errors arrive as rc 7 **on stdout** with an
  empty stderr (report 03). So nothing here trusts a return code: reads
  require parseable JSON of the expected shape, and every act is followed
  by a compositor re-query — the verified-outcome pattern the filesystem
  mutations already use.
* The instance signature is passed explicitly (``-i``/``--instance``) to
  every ``hyprctl`` call rather than relying on inherited env, so the
  approval dialog can state which compositor session is touched, and a
  missing signature is a clean, detectable failure (report 03 rule 4).
  (``-r`` is *refresh*, not instance; the live 0.56.2 build read the
  signature as a request name and answered "unknown request".)
* Focusing a window goes through this session's Lua dispatch API:
  ``hl.dispatch(hl.dsp.focus({window=w}))`` with ``w`` resolved from
  ``hl.get_windows()`` by address. The classic
  ``dispatch focuswindow "(address:0x…)"`` string form report 13 measured
  is rejected here ("expected a dispatcher"); this is the form that
  actually moved and restored focus on the machine.
* ``grim`` writes the PNG to stdout only with a trailing ``-`` placed
  *after* the options, and this build parses geometry as ``X,Y WxH`` — the
  documented ``WxH+X+Y`` form is rejected as "invalid geometry" (both
  verified live). With no output argument at all grim writes a timestamped
  file instead, which is exactly what the no-pixels rule forbids.
* ``grim`` is a Wayland client and needs ``WAYLAND_DISPLAY`` (and
  usually ``XDG_RUNTIME_DIR``) passed explicitly: Stella's process
  environment is not guaranteed to carry a usable session.
* ``wtype`` is proven working unprivileged on this box (report 13). It
  injects into the *focused* window only, which is why the tool that uses
  it focuses an explicitly-id-addressed target, confirms the focus with a
  re-query, and only then types.
* No signature means no spawn at all (report 03, fail closed): a
  ``hyprctl`` without an instance can reach a compositor that is not this
  session's.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable, Mapping, Sequence

from stella.desktop.capabilities import (
    CaptureUnavailable,
    Desktop,
    DesktopUnavailable,
    Region,
    SendUnavailable,
    Window,
    launch_command_usable,
)
from stella.desktop.ocr import TesseractRecognizer
from stella.desktop.runner import (
    Completed,
    Runner,
    error_detail,
    subprocess_runner,
)

WINDOW_ID_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
"""Hyprland's own id shape: the window's hex address.

Validation lives *here*, not in the tools: the id is interpolated into a
Lua chunk, so a value that is not plain hex must never reach it.
"""

HYPRCTL_TIMEOUT_SECONDS = 5.0
CAPTURE_TIMEOUT_SECONDS = 10.0
KEYSEND_TIMEOUT_SECONDS = 15.0
SETTLE_SECONDS = 0.4
"""Gap between re-queries while waiting for a dispatched act to land."""

NO_SESSION_MESSAGE = (
    "No Hyprland session signature is set, so there is no desktop to "
    "read or act on."
)


def _region(payload: Mapping[str, object]) -> Region | None:
    """The active window's geometry from hyprctl's ``at``/``size``.

    Both fields are two-element int lists on this build; anything else is
    "no geometry", which the tools treat as an unusable window scope
    rather than an excuse to capture the whole screen.
    """

    at, size = payload.get("at"), payload.get("size")
    if (
        not isinstance(at, Sequence)
        or isinstance(at, (str, bytes))
        or len(at) != 2
        or not all(isinstance(value, int) for value in at)
        or not isinstance(size, Sequence)
        or isinstance(size, (str, bytes))
        or len(size) != 2
        or not all(isinstance(value, int) for value in size)
    ):
        return None
    return Region(int(at[0]), int(at[1]), int(size[0]), int(size[1]))


def _window(payload: object) -> Window | None:
    """Parse one hyprctl window object; anything odd is None (rule 1)."""

    if not isinstance(payload, Mapping):
        return None
    window_id = payload.get("address")
    class_name = payload.get("class")
    title = payload.get("title")
    pid = payload.get("pid")
    workspace = payload.get("workspace")
    if (
        not isinstance(window_id, str)
        or WINDOW_ID_RE.match(window_id) is None
        or not isinstance(class_name, str)
        or not isinstance(title, str)
        or not isinstance(pid, int)
        or not isinstance(workspace, Mapping)
        or not isinstance(workspace.get("id"), int)
    ):
        return None
    # Geometry is optional here: ``clients`` answers may or may not carry
    # it, and a window that exists but reports no box is still a window.
    return Window(
        window_id,
        class_name,
        title,
        pid,
        str(workspace["id"]),
        _region(payload),
    )


class Hyprland:
    """One Hyprland session: reader, activator, capturer, typist, manager."""

    def __init__(
        self,
        signature: str | None,
        *,
        wayland_display: str | None = None,
        xdg_runtime_dir: str | None = None,
        runner: Runner = subprocess_runner,
        hyprctl: str = "hyprctl",
        grim: str = "grim",
        wtype: str = "wtype",
        sleep: Callable[[float], None] = time.sleep,
        settle_seconds: float = SETTLE_SECONDS,
    ) -> None:
        self.signature = signature
        self.wayland_display = wayland_display
        self.xdg_runtime_dir = xdg_runtime_dir
        self._runner = runner
        self.hyprctl = hyprctl
        self.grim = grim
        self.wtype = wtype
        self._sleep = sleep
        self._settle_seconds = settle_seconds

    def required_binaries(self) -> tuple[str, ...]:
        return (self.hyprctl, self.grim, self.wtype)

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> Hyprland:
        return cls(
            env.get("HYPRLAND_INSTANCE_SIGNATURE") or None,
            wayland_display=env.get("WAYLAND_DISPLAY") or None,
            xdg_runtime_dir=env.get("XDG_RUNTIME_DIR") or None,
        )

    # ------------------------------------------------------------- reads

    def _hyprctl(self, *request: str, timeout: float = HYPRCTL_TIMEOUT_SECONDS):
        argv = self._hyprctl_argv(*request)
        if argv is None:
            return Completed(1, b"", b"")
        return self._spawn(argv, timeout=timeout)

    def _hyprctl_argv(self, *request: str) -> list[str] | None:
        if self.signature is None:
            # Fail closed without spawning (report 03): no signature,
            # no session, no touch of a possibly foreign compositor.
            return None
        # ``-i``/``--instance`` selects the session; ``-r`` is *refresh*,
        # not the instance flag, and treating the signature as it makes
        # hyprctl read the next word as a request ("unknown request").
        return [self.hyprctl, "-i", self.signature, "-j", *request]

    def _eval(self, code: str, *, timeout: float = HYPRCTL_TIMEOUT_SECONDS):
        if self.signature is None:
            return Completed(1, b"", b"")
        # ``eval`` takes a Lua chunk, not a JSON request, so -j is absent.
        argv = [self.hyprctl, "-i", self.signature, "eval", code]
        return self._spawn(argv, timeout=timeout)

    def _spawn(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Completed:
        """A run that could not even start reads as an unusable answer.

        Returning a shaped failure rather than letting ``OSError`` escape
        keeps rule 1 intact for the read path: the JSON shape check turns
        it into ``None`` ("the compositor did not answer"), which is the
        honest meaning of "the binary disappeared since registration".
        """

        try:
            return self._runner(argv, timeout=timeout, stdin=stdin, env=env)
        except OSError as failure:
            return Completed(1, str(failure).encode("utf-8", "replace"), b"")

    def active_window(self) -> Window | None:
        result = self._hyprctl("activewindow")
        return self._json_window(result)

    def windows(self) -> list[Window] | None:
        """Every window, or None when the answer is not the expected shape.

        The rc=0-with-garbage trap (report 03) is why shape, not the
        return code, decides: an unusable answer is an error, never an
        empty list that would read as "no windows are open".
        """

        result = self._hyprctl("clients")
        payload = self._json(result)
        if not isinstance(payload, list):
            return None
        windows = [_window(item) for item in payload]
        if any(window is None for window in windows):
            return None
        return [window for window in windows if window is not None]

    @staticmethod
    def _json(result: Completed) -> object:
        try:
            return json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _json_window(self, result: Completed) -> Window | None:
        return _window(self._json(result))

    # ------------------------------------------------------------- acts

    def activate(self, window_id: str) -> bool:
        """Dispatch a focus and confirm it by re-querying (rule 1)."""

        if self.signature is None:
            return False
        if WINDOW_ID_RE.match(window_id) is None:
            # The id reaches a Lua chunk, so this adapter validates it
            # itself rather than trusting whatever the tool was given.
            return False
        # The classic Hyprland dispatch string
        # (``dispatch focuswindow "(address:0x…)"``, report 13) is
        # rejected on this Omarchy 0.56.2 session: dispatch is routed
        # through the compositor's Lua API and the raw string dies with
        # "expected a dispatcher". The verified-working form resolves the
        # window object by its address and focuses it through that API.
        # The answer of the dispatch itself is ignored on purpose: the
        # re-query below is the only proof that counts.
        self._dispatch_accepted(
            self._eval(
                "for _,w in ipairs(hl.get_windows()) do "
                f"if w.address=='{window_id}' then "
                "hl.dispatch(hl.dsp.focus({window=w})) end end"
            )
        )
        active = self.active_window()
        return active is not None and active.id == window_id

    # ------------------------------------------------------ window mgmt

    def _dispatch_accepted(self, result: Completed) -> None:
        """Raise unless the compositor visibly accepted the chunk.

        Report 03 measured that real dispatch errors arrive as rc 7 with
        the text on **stdout**; ``ok`` on rc 0 means accepted, not done —
        only the re-query after it proves anything.
        """

        text = result.stdout.decode("utf-8", errors="replace").casefold()
        if result.returncode != 0 or "error:" in text:
            raise DesktopUnavailable(
                "Hyprland rejected the request. " + error_detail(result, label="Said")
            )

    def _validated_window(self, window_id: str) -> str:
        if self.signature is None:
            raise DesktopUnavailable(NO_SESSION_MESSAGE)
        if WINDOW_ID_RE.match(window_id) is None:
            raise DesktopUnavailable("That is not a usable Hyprland window id.")
        return window_id

    def _windows_or_empty(self) -> list[Window]:
        windows = self.windows()
        return [] if windows is None else windows

    def _dispatch_window(self, window_id: str, body: str) -> Completed:
        # Resolve the live window object by address (the proven focus
        # form) and hand it to the dispatcher chunk in ``body``.
        return self._eval(
            "for _,w in ipairs(hl.get_windows()) do "
            f"if w.address=='{window_id}' then {body} end end"
        )

    def close(self, window_id: str) -> bool:
        self._validated_window(window_id)
        self._dispatch_accepted(
            self._dispatch_window(window_id, "hl.dispatch(hl.dsp.window.close{window=w})")
        )
        # A close is verified by absence; give the compositor a moment and
        # re-query. Still present is False ("unverified"), never a fake ok.
        return self._wait_for(
            lambda: all(w.id != window_id for w in self._windows_or_empty())
        )

    def move(self, window_id: str, workspace: int) -> bool:
        self._validated_window(window_id)
        if not 1 <= workspace <= 1000:
            raise DesktopUnavailable("The workspace number is out of range.")
        self._dispatch_accepted(
            self._dispatch_window(
                window_id,
                "hl.dispatch(hl.dsp.window.move{window=w,"
                + f"workspace='{workspace}'"
                + ",follow=false})",
            )
        )
        return self._wait_for(
            lambda: any(
                w.id == window_id and w.workspace == str(workspace)
                for w in self._windows_or_empty()
            )
        )

    def launch(self, command: str, workspace: int | None = None) -> bool:
        if self.signature is None:
            raise DesktopUnavailable(NO_SESSION_MESSAGE)
        # ``hl.exec_cmd`` is the top-level Lua launcher verified live on
        # this 0.56.2 session (the dsp.exec_cmd member does not exist
        # here; classic string dispatch is rejected). The command reaches
        # a Lua single-quoted string, so it must already be metacharacter-
        # free — the shared fence is re-checked here rather than trusted
        # from above, and quotes/backslashes are refused even though the
        # shared charset already excludes them.
        if "'" in command or "\\" in command or not launch_command_usable(command):
            raise DesktopUnavailable("The program name is not a safe single command.")
        before = {window.id for window in self._windows_or_empty()}
        self._dispatch_accepted(self._eval(f"hl.exec_cmd('{command}')"))
        appeared = self._wait_for_new_window(before)
        if appeared is None:
            return False
        if workspace is None:
            return True
        return self.move(appeared.id, workspace)

    def _settle(self) -> None:
        self._sleep(self._settle_seconds)

    def _wait_for(self, settled: Callable[[], bool], *, probes: int = 3) -> bool:
        for _ in range(probes):
            if settled():
                return True
            self._settle()
        return settled()

    def _wait_for_new_window(self, before: set[str]) -> Window | None:
        for _ in range(6):
            fresh = [
                window
                for window in self._windows_or_empty()
                if window.id not in before
            ]
            if fresh:
                return fresh[0]
            self._settle()
        return None

    # --------------------------------------------------------- external

    @staticmethod
    def _geometry(region: Region) -> str:
        # This grim build parses ``X,Y WxH``; the documented ``WxH+X+Y``
        # form is rejected here as "invalid geometry" (verified live).
        return f"{region.x},{region.y} {region.width}x{region.height}"

    def capture(self, region: Region | None) -> bytes:
        if self.signature is None:
            raise CaptureUnavailable(NO_SESSION_MESSAGE)
        # The trailing ``-`` (after any options) makes grim write the PNG
        # to stdout; misplaced it is read as a bad output-file argument,
        # and with no output argument at all grim writes a timestamped
        # file instead (both verified live).
        argv = [self.grim]
        if region is not None:
            argv += ["-g", self._geometry(region)]
        argv.append("-")
        env = {"WAYLAND_DISPLAY": self.wayland_display or ""}
        if self.xdg_runtime_dir:
            env["XDG_RUNTIME_DIR"] = self.xdg_runtime_dir
        try:
            result = self._runner(argv, timeout=CAPTURE_TIMEOUT_SECONDS, env=env)
        except OSError as failure:
            raise CaptureUnavailable(
                f"Screen capture failed ({self.grim} could not run: {failure})."
            ) from failure
        if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
            raise CaptureUnavailable(
                f"Screen capture failed ({self.grim} could not write a PNG). "
                + error_detail(result, label="Compositor said")
            )
        return result.stdout

    def send_text(self, text: str) -> None:
        if self.signature is None:
            raise SendUnavailable(NO_SESSION_MESSAGE)
        try:
            result = self._runner([self.wtype, text], timeout=KEYSEND_TIMEOUT_SECONDS)
        except OSError as failure:
            raise SendUnavailable(
                f"The keystrokes were not delivered ({self.wtype} could not "
                f"run: {failure})."
            ) from failure
        if result.returncode != 0:
            raise SendUnavailable(
                f"The keystrokes were not delivered ({self.wtype} failed). "
                + error_detail(result, label="Compositor said")
            )


def probe(
    env: Mapping[str, str],
    runner: Runner = subprocess_runner,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> Desktop | None:
    """This machine's Hyprland desktop, or None when it is not one.

    Usable means both the session marker *and* every binary the adapter
    will spawn: a tool that could only ever fail must never reach the
    model (rule 5).
    """

    hyprland = Hyprland.from_environment(env)
    if hyprland.signature is None:
        return None
    recognizer = TesseractRecognizer(runner)
    needed = (*hyprland.required_binaries(), *recognizer.required_binaries())
    if any(which(binary) is None for binary in needed):
        return None
    return Desktop(
        name="hyprland",
        reader=hyprland,
        activator=hyprland,
        capture=hyprland,
        keys=hyprland,
        recognizer=recognizer,
        manager=hyprland,
        # The approval dialog prints this: which exact compositor session
        # is about to be read or typed into (report 03 rule 4).
        session_note=f"Hyprland session {hyprland.signature}",
    )


__all__ = [
    "WINDOW_ID_RE",
    "Hyprland",
    "probe",
]
