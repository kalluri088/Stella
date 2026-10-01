"""The sway adapter — an i3-IPC client that believes the reply, not the socket.

Same contract as the Hyprland adapter (see ``backends/__init__.py``),
same shape of decisions, but every concrete fact carries a different
kind of evidence. The machine this was written on runs Hyprland, so
nothing below is measured; it is the sway/i3-IPC protocol as documented,
deliberately read through the traps the Hyprland measurements exposed.
The probe gate at the bottom is what makes shipping this safe: nothing
ever registers unless a live session *answers as sway specifically*,
so an unverified command that would be wrong on a real sway box can only
ever be wrong inside a session that proved it is sway first — and on an
absent desktop the probe spawns nothing useful and returns ``None``.

Decisions this module owes the invariants:

* **Proof, not env-var faith** (registry rule: a marker is a hint).
  ``$SWAYSOCK`` gates the probe (missing → ``None`` without a single
  spawn — a nested session's leftover socket in the environment is a
  real thing), and ``swaymsg -t get_version`` then has to answer with
  the shape that identifies sway: a mapping whose ``variant`` is the
  string ``"sway"`` with integer ``major``/``minor``. A bare i3 reply
  is *not* sway — labwc, Wayfire and friends speak i3-IPC too and
  belong to a different adapter — so it reads as "not this desktop".
* **Reads trust shape, never exit codes** (rule 1): ``windows()`` and
  ``active_window()`` parse ``swaymsg -t get_tree`` and return ``None``
  for anything that is not the JSON array-of-root-nodes form.
* **``None`` versus ``[]``, deliberately**: a reply that parses as a
  JSON list whose every element is an object — including ``[]`` and
  including a root tree with no window nodes inside — is sway
  *answering in the trusted form and reporting nothing open*, which is
  a real ``[]``. Anything else (non-JSON, a bare object, a list
  containing a non-object, empty output) is unusable → ``None``. The
  two are never collapsed, in either direction.
* **The window id is the *leaf* node's ``id``** (rule 5): sway's tree
  carries an ``id`` on containers too, and ``[con_id=<id>]`` on a split
  container focuses whichever window that container last held — a
  different window from the one the tools showed the user. A window
  here is exactly a node with ``app_id`` (native Wayland) or
  ``window`` (the X11 id under XWayland, whose class lives in
  ``window_properties.class``); both forms carry a ``shell`` and layer
  surfaces (bars, wallpapers) are excluded — they are not focusable
  windows. Only these leaf nodes' ids become ``Window.id``, and the
  ``^[0-9]{1,9}$`` validation is repeated *here* before an id touches
  any command line.
* **Act, then re-query** (rule 2): ``activate`` ignores swaymsg's own
  answer entirely — send ``[con_id=<id>] focus``, ask for the tree
  again, and return True only while the focused window node is that id
  (the Hyprland adapter's pattern; swaymsg exiting 0 proves nothing).
* **Keystrokes reach the focused surface only** (rule 3): the one
  typing route is ``wtype`` with no target argument at all; focus is
  verified by the caller in ``tools.py`` before ``send_text`` runs, and
  there is deliberately no route that could type into a window by id.
* **Pixels never persist** (rule 4): capture is ``grim`` (sway is
  wlroots, so ``zwlr-screencopy-v1`` is the expected backend) writing
  to stdout via a trailing ``-`` placed *after* the options, with the
  Wayland session passed explicitly; non-PNG or failure is
  ``CaptureUnavailable``. The active-window scope depends on the node
  ``rect``: a window whose geometry sway did not report keeps
  ``region=None``, which makes ``screen_read`` fail rather than widen
  to a full-screen capture nobody approved.
* **The approval dialog names the session** (rule 6): ``session_note``
  carries the exact ``$SWAYSOCK`` path and the version string sway
  answered with, and the socket is passed explicitly in the env of
  every ``swaymsg`` call rather than relying on the inherited one.
* Even the injected seam is untrusted input: a runner that raises or
  does not answer with a :class:`Completed` reads as an unusable
  answer (or a clean refusal), never a crash into the registry. That
  is also what keeps sway's entry in the stub-registration test honest
  now that this adapter is written: against a seam that cannot answer
  in a trusted form, every environment still probes to ``None``.

UNVERIFIED — every fact below is documentation-derived, not measured,
and a real sway machine must confirm each one (the Hyprland adapter's
docstring shows what that review does to an adapter):

* that sway's ``get_version`` reply is the mapping with
  ``"variant": "sway"`` + integer ``major``/``minor``/``patch`` as
  assumed here, and — equally — that i3's reply does *not* carry
  ``variant`` (i3 is the false positive this gate exists to catch);
* that ``get_tree``'s window ``id`` round-trips through
  ``[con_id=<id>] focus``: i3 documents ids as valid per tree layout,
  so a real pass must confirm ids survive between the two calls the
  focus path makes (a stale id must make sway answer "no node matches"
  and the re-query must then read False — safe but worth proving);
* whether ``[con_id=N] focus`` on a window on another workspace
  switches to it (assumed) or silently no-ops (the re-query saves the
  answer either way, same as Hyprland);
* the assumed ``get_tree`` field names and shapes: ``nodes`` /
  ``floating_nodes`` child lists, ``focused``, ``name`` (nullable for
  untitled windows → ``title=""``), ``app_id``, ``window`` +
  ``window_properties.class``, ``pid`` (null → 0), ``shell`` with
  value ``"layer"`` marking non-windows, ``workspace_info.num``/``.name``,
  ``rect`` as ``[x, y, width, height]``;
* that ``swaymsg`` needs no ``-r``/``--raw`` for the JSON to parse
  (assumed: its default pretty-printed JSON parses as-is);
* that ``swaymsg`` selects its session from ``$SWAYSOCK`` passed in
  the call's env (the i3/sway docs say so; Hyprland measured the
  analogous explicit-instance rule live, this has not been);
* whether this build's ``grim`` accepts the geometry string copied
  verbatim from the Hyprland measurement (``X,Y WxH`` after ``-g``) —
  grim is the same family of tool but the form was measured on
  Hyprland's build, and multi-monitor may additionally want ``-o``;
* whether ``wtype`` is allowed under sway's default config (no input
  restriction assumed; ``ydotool``'s daemon/group requirements are
  deliberately not the route);
* the timeout numbers, copied from Hyprland's measurements.

Binaries gate the probe through the injected ``which``: ``swaymsg``,
``grim``, ``wtype``, ``tesseract`` — any missing means ``None`` and no
tool reaches the model (invariant 5).
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence

from stella.desktop.capabilities import (
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

WINDOW_ID_RE = re.compile(r"^[0-9]{1,9}$")
"""Sway's own id shape: the leaf window node's decimal container id.

Validation lives *here*, not in the tools: the id is interpolated into
the ``[con_id=…]`` selector of a swaymsg command line, so anything that
is not a bounded plain number must never reach it (rule 5).
"""

IPC_TIMEOUT_SECONDS = 5.0
CAPTURE_TIMEOUT_SECONDS = 10.0
KEYSEND_TIMEOUT_SECONDS = 15.0

NO_SESSION_MESSAGE = (
    "No sway IPC socket is set, so there is no desktop to read or act on."
)


def _is_int(value: object) -> bool:
    """A real integer: ``bool`` is an ``int`` in Python and never a pid."""

    return isinstance(value, int) and not isinstance(value, bool)


def _region(node: Mapping[str, object]) -> Region | None:
    """The window's ``rect`` as [x, y, width, height], or no geometry.

    ``screen_read`` scope=active_window needs the box and must fail, not
    widen to a full-screen capture, when sway did not report one.
    """

    rect = node.get("rect")
    if (
        not isinstance(rect, Sequence)
        or isinstance(rect, (str, bytes))
        or len(rect) != 4
        or not all(_is_int(value) for value in rect)
    ):
        return None
    return Region(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))


def _is_window(node: Mapping[str, object]) -> bool:
    """Whether this tree node *is* a window rather than a container.

    sway marks a leaf window with ``app_id`` (native Wayland) or the
    X11 ``window`` id (XWayland); containers carry neither. Layer-shell
    surfaces (bars, wallpapers) have an ``app_id`` but are not windows
    anyone can focus, so ``shell == "layer"`` excludes them.
    """

    if node.get("shell") == "layer":
        return False
    return isinstance(node.get("app_id"), str) or _is_int(node.get("window"))


def _window(node: Mapping[str, object]) -> Window | None:
    """Parse one window leaf node; anything odd is None (rule 1).

    ``id`` is the *leaf* node's container id — the only id in sway's
    tree that ``[con_id=…]`` focuses back to this exact window. Missing
    or null-but-harmless fields read honestly (untitled window →
    ``title=""``, unknown pid → 0, no class → ``""``); a non-numeric or
    out-of-shape ``id`` is not harmless, because a window we cannot
    address is a window the tools must not offer.
    """

    window_id = node.get("id")
    if not _is_int(window_id) or WINDOW_ID_RE.match(str(window_id)) is None:
        return None
    class_name = node.get("app_id")
    class_name = class_name if isinstance(class_name, str) else ""
    if not class_name:
        properties = node.get("window_properties")
        if isinstance(properties, Mapping) and isinstance(properties.get("class"), str):
            class_name = properties["class"]
    title = node.get("name")
    pid = node.get("pid")
    workspace = ""
    info = node.get("workspace_info")
    if isinstance(info, Mapping):
        if _is_int(info.get("num")):
            workspace = str(info["num"])
        elif isinstance(info.get("name"), str):
            workspace = info["name"]
    return Window(
        str(window_id),
        class_name,
        title if isinstance(title, str) else "",
        pid if _is_int(pid) else 0,
        workspace,
        _region(node),
    )


def _collect_window_nodes(payload: Iterable[object]) -> list[Mapping] | None:
    """Walk ``nodes``/``floating_nodes`` recursively; odd shape is None.

    The traversal itself is shape-checked: a child list that is not a
    list, or a node that is not an object, means this is not a tree sway
    answered with, and the honest answer upstream is ``None``.
    """

    found: list[Mapping] = []
    stack: list[object] = list(payload)
    while stack:
        node = stack.pop(0)
        if not isinstance(node, Mapping):
            return None
        if _is_window(node):
            found.append(node)
        for key in ("nodes", "floating_nodes"):
            children = node.get(key)
            if children is None:
                continue
            if not isinstance(children, list):
                return None
            stack.extend(children)
    return found


class Sway:
    """One sway session, as reader, activator, capturer and typist."""

    def __init__(
        self,
        socket: str | None,
        *,
        wayland_display: str | None = None,
        xdg_runtime_dir: str | None = None,
        runner: Runner = subprocess_runner,
        swaymsg: str = "swaymsg",
        grim: str = "grim",
        wtype: str = "wtype",
    ) -> None:
        self.socket = socket
        self.wayland_display = wayland_display
        self.xdg_runtime_dir = xdg_runtime_dir
        self._runner = runner
        self.swaymsg = swaymsg
        self.grim = grim
        self.wtype = wtype

    def required_binaries(self) -> tuple[str, ...]:
        return (self.swaymsg, self.grim, self.wtype)

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str], *, runner: Runner = subprocess_runner
    ) -> Sway:
        # the runner is wired here because the probe itself asks the
        # session a question — the injected seam must answer it
        return cls(
            env.get("SWAYSOCK") or None,
            wayland_display=env.get("WAYLAND_DISPLAY") or None,
            xdg_runtime_dir=env.get("XDG_RUNTIME_DIR") or None,
            runner=runner,
        )

    # ------------------------------------------------------------- spawn

    def _spawn(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> Completed:
        """A run that cannot start — or cannot answer — is an unusable answer.

        ``OSError`` is shaped rather than allowed to escape, and so is a
        runner that returns something that is not a ``Completed``: the
        seam's output is input too, and rule 1 means the read path then
        sees empty stdout and returns ``None`` honestly.
        """

        try:
            result = self._runner(argv, timeout=timeout, env=env)
        except OSError as failure:
            return Completed(1, str(failure).encode("utf-8", "replace"), b"")
        if not isinstance(result, Completed):
            return Completed(1, b"", b"")
        return result

    def _ipc_env(self) -> dict[str, str] | None:
        """The socket is passed explicitly, never left to inheritance.

        This is sway's version of Hyprland's measured ``-i`` rule: the
        call must reach *this* session's socket, and the session_note
        can then truthfully name the one being touched.
        """

        if self.socket is None:
            return None
        return {"SWAYSOCK": self.socket}

    def _swaymsg(self, *arguments: str, timeout: float = IPC_TIMEOUT_SECONDS):
        if self.socket is None:
            # Fail closed without spawning: no socket, no session, no
            # touch of a possibly foreign compositor.
            return Completed(1, b"", b"")
        return self._spawn(
            [self.swaymsg, *arguments],
            timeout=timeout,
            env=self._ipc_env(),
        )

    @staticmethod
    def _json(result: Completed) -> object:
        try:
            return json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    # ----------------------------------------------------------- reads

    def version(self) -> Mapping[str, object] | None:
        """The get_version reply, only if it identifies sway specifically.

        The registry ranks sway second, so this probe must not steal an
        i3 (or labwc, or any i3-IPC WM) session: ``variant == "sway"``
        plus integer major/minor is the reply shape sway adds on top of
        i3's. Anything else — including a clean i3 answer, garbage, or
        no answer — is ``None`` and no further spawning happens.
        """

        payload = self._json(self._swaymsg("-t", "get_version"))
        if (
            not isinstance(payload, Mapping)
            or payload.get("variant") != "sway"
            or not _is_int(payload.get("major"))
            or not _is_int(payload.get("minor"))
        ):
            return None
        return payload

    def _window_nodes(self) -> list[Mapping] | None:
        payload = self._json(self._swaymsg("-t", "get_tree"))
        if not isinstance(payload, list):
            return None
        return _collect_window_nodes(payload)

    def windows(self) -> list[Window] | None:
        """Every window, or None when the answer is not the tree shape.

        ``[]`` here is real: sway answered as an array of root nodes
        and the walk found no window inside. An unusable answer never
        collapses into that ``[]`` (the rc-0-with-garbage trap).
        """

        nodes = self._window_nodes()
        if nodes is None:
            return None
        parsed = [_window(node) for node in nodes]
        if any(window is None for window in parsed):
            return None
        return [window for window in parsed if window is not None]

    def active_window(self) -> Window | None:
        """The node the tree reports as focused, if the answer is usable.

        Note the one honest ambiguity the reader contract leaves: a
        usable tree with no focused window (an empty workspace) and an
        unusable answer both read ``None`` here — the contract gives
        ``None`` for "did not answer in a trusted form" and has no
        third value for "answered, nothing is focused".
        """

        nodes = self._window_nodes()
        if nodes is None:
            return None
        for node in nodes:
            if node.get("focused") is True:
                return _window(node)
        return None

    # ------------------------------------------------------------- acts

    def activate(self, window_id: str) -> bool:
        """Focus by con_id and prove it by re-querying (rule 2)."""

        if self.socket is None:
            return False
        if WINDOW_ID_RE.match(window_id) is None:
            # The id reaches a swaymsg command line, so this adapter
            # validates it itself (rule 5); nothing malformed spawns.
            return False
        # swaymsg's own reply is ignored on purpose — the re-query below
        # is the only proof that counts (the Hyprland adapter's pattern;
        # a focus that silently no-ops still exits 0, UNVERIFIED here
        # but the verification is what makes it moot).
        self._swaymsg(f"[con_id={window_id}] focus")
        active = self.active_window()
        return active is not None and active.id == window_id

    # --------------------------------------------------------- external

    @staticmethod
    def _geometry(region: Region) -> str:
        # UNVERIFIED for sway: this copies Hyprland's *measured* grim
        # form (``X,Y WxH``; the documented ``WxH+X+Y`` was rejected
        # there). Same tool family, different machine — re-measure.
        return f"{region.x},{region.y} {region.width}x{region.height}"

    def _wayland_env(self) -> dict[str, str]:
        env = {"WAYLAND_DISPLAY": self.wayland_display or ""}
        if self.xdg_runtime_dir:
            env["XDG_RUNTIME_DIR"] = self.xdg_runtime_dir
        return env

    def capture(self, region: Region | None) -> bytes:
        if self.socket is None:
            raise CaptureUnavailable(NO_SESSION_MESSAGE)
        # The trailing ``-`` (after any options) is what makes grim
        # write the PNG to stdout instead of a timestamped file —
        # mirrored from the Hyprland measurement, pixels never persist.
        argv = [self.grim]
        if region is not None:
            argv += ["-g", self._geometry(region)]
        argv.append("-")
        result = self._spawn(
            argv,
            timeout=CAPTURE_TIMEOUT_SECONDS,
            env=self._wayland_env(),
        )
        if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
            raise CaptureUnavailable(
                f"Screen capture failed ({self.grim} could not write a PNG). "
                + error_detail(result, label="Compositor said")
            )
        return result.stdout

    def send_text(self, text: str) -> None:
        if self.socket is None:
            raise SendUnavailable(NO_SESSION_MESSAGE)
        # The only typing route: wtype with no target argument, so the
        # keystrokes land on whatever sway says is focused — which the
        # caller in tools.py has just verified. There is deliberately
        # no path that could type into a window addressed by id.
        result = self._spawn(
            [self.wtype, text],
            timeout=KEYSEND_TIMEOUT_SECONDS,
            env=self._wayland_env(),
        )
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
    """This machine's sway desktop, or None when it is not one.

    The gate in order: the session hint must be set (otherwise nothing
    spawns at all), every binary this adapter will ever need must exist
    (a tool that could only fail must never reach the model, rule 5),
    and only then does the one proving question get asked — and it must
    be answered *as sway*, because a leftover i3-IPC socket is a real
    thing and this adapter is not allowed to claim it.
    """

    sway = Sway.from_environment(env, runner=runner)
    if sway.socket is None:
        return None
    if any(which(binary) is None for binary in sway.required_binaries()):
        return None
    recognizer = TesseractRecognizer(runner)
    if any(which(binary) is None for binary in recognizer.required_binaries()):
        return None
    version = sway.version()
    if version is None:
        return None
    readable = version.get("human_readable")
    note = f"sway session on socket {sway.socket}"
    if isinstance(readable, str):
        note += f" (version {readable[:200]})"
    return Desktop(
        name="sway",
        reader=sway,
        activator=sway,
        capture=sway,
        keys=sway,
        recognizer=recognizer,
        # The approval dialog prints this: which exact sway session is
        # about to be read or typed into (rule 6).
        session_note=note,
    )


__all__ = [
    "WINDOW_ID_RE",
    "Sway",
    "probe",
]
