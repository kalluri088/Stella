"""Desktop tools for the Hyprland session, built on measured facts only.

Research reports 03, 11 and 12/13 settled this surface on the real
machine, and every rule below is one of their measurements, not a
style choice:

* ``hyprctl`` answers garbage with **rc 0 and "unknown request" on
  stdout**, and real dispatch errors arrive as rc 7 **on stdout** with
  an empty stderr (report 03). So no tool here trusts a return code:
  reads require parseable JSON of the expected shape, and every act is
  followed by a compositor re-query — the verified-outcome pattern the
  filesystem mutations already use.
* The instance signature is passed explicitly (``-i``/``--instance``)
  to every ``hyprctl`` call rather than relying on inherited env, so the
  approval dialog can state which compositor session is touched, and a
  missing signature is a clean, detectable failure (report 03 rule 4).
  (``-r`` is *refresh*, not instance; the live 0.56.2 build read the
  signature as a request name and answered "unknown request".)
* Focusing a window goes through this session's Lua dispatch API:
  ``hl.dispatch(hl.dsp.focus({window=w}))`` with ``w`` resolved from
  ``hl.get_windows()`` by address. The classic
  ``dispatch focuswindow "(address:0x…)"`` string form report 13
  measured is rejected here ("expected a dispatcher"); this is the form
  that actually moved and restored focus on the machine.
* ``screen_read`` is OCR-first: ``grim`` (~70 ms window capture) piped
  straight into ``tesseract --psm 6`` (~1.4 s) gives the honest answer
  to "what does this window say?" with zero cloud and zero GPU
  (report 11); pixels never persist, and the bounded OCR text — passed
  through a secrets mask first — is the privacy control.
* ``key_send`` uses ``wtype``, which is proven working unprivileged on
  this box (report 13). It injects into the *focused* window only, so
  the tool focuses an allowlisted, explicitly-addressed target,
  confirms the focus with a re-query, and only then types; a target is
  never guessed by title when several windows share one.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from stella.tools import (
    ActionPreview,
    ActionReceipt,
    ApprovalRequest,
    RiskLevel,
    Tool,
    ToolResult,
    _truncate_text,
)

# --------------------------------------------------------------------------
# the compositor client
# --------------------------------------------------------------------------

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")

HYPRCTL_TIMEOUT_SECONDS = 5.0
CAPTURE_TIMEOUT_SECONDS = 10.0
OCR_TIMEOUT_SECONDS = 30.0
KEYSEND_TIMEOUT_SECONDS = 15.0

MAX_SCREEN_TEXT_CHARS = 6_000  # the privacy bound on OCR text (report 11)
MAX_KEY_TEXT_CHARS = 2_000  # one bounded typing action


@dataclass(frozen=True)
class Completed:
    """The three facts of a subprocess run that any decision may use."""

    returncode: int
    stdout: bytes
    stderr: bytes


Runner = Callable[..., Completed]
"""``callable(argv: Sequence[str], *, timeout: float, stdin: bytes | None)``."""


def _subprocess_runner(
    argv: Sequence[str],
    *,
    timeout: float,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> Completed:
    process = subprocess.run(
        list(argv),
        input=stdin,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=dict(env) if env is not None else None,
    )
    return Completed(process.returncode, process.stdout, process.stderr)


@dataclass(frozen=True)
class Window:
    """One window as ``hyprctl -j`` describes it (report 03 fields)."""

    address: str
    class_name: str
    title: str
    pid: int
    workspace: int


def _window(payload: object) -> Window | None:
    """Parse one hyprctl window object; anything odd is None (rule 1)."""

    if not isinstance(payload, Mapping):
        return None
    address = payload.get("address")
    class_name = payload.get("class")
    title = payload.get("title")
    pid = payload.get("pid")
    workspace = payload.get("workspace")
    if (
        not isinstance(address, str)
        or ADDRESS_RE.match(address) is None
        or not isinstance(class_name, str)
        or not isinstance(title, str)
        or not isinstance(pid, int)
        or not isinstance(workspace, Mapping)
        or not isinstance(workspace.get("id"), int)
    ):
        return None
    return Window(address, class_name, title, pid, workspace["id"])


class ScreenCapturer(Protocol):
    """Writes one PNG of the requested region to stdout."""

    def capture(
        self, geometry: str | None, signature: str, timeout: float
    ) -> Completed:
        """``geometry`` is a grim ``X,Y WxH`` string, or None for the
        whole screen."""


class TextRecognizer(Protocol):
    """Turns PNG bytes into text; report 11's tesseract step."""

    def recognize(
        self, png: bytes, psm: str, timeout: float
    ) -> Completed:
        ...


class Hyprland:
    """A read-trusts-JSON, act-then-re-query client for one session."""

    def __init__(
        self,
        signature: str | None,
        *,
        wayland_display: str | None = None,
        xdg_runtime_dir: str | None = None,
        runner: Runner = _subprocess_runner,
        hyprctl: str = "hyprctl",
        grim: str = "grim",
        tesseract: str = "tesseract",
        wtype: str = "wtype",
    ) -> None:
        self.signature = signature
        self.wayland_display = wayland_display
        self.xdg_runtime_dir = xdg_runtime_dir
        self._runner = runner
        self._binaries = (hyprctl, grim, tesseract, wtype)
        self.hyprctl = hyprctl
        self.grim = grim
        self.tesseract = tesseract
        self.wtype = wtype

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
        return self._runner(argv, timeout=timeout)

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
        return self._runner(argv, timeout=timeout)

    def active_window(self) -> Window | None:
        result = self._hyprctl("activewindow")
        return self._json_window(result)

    def clients(self) -> list[Window] | None:
        """All windows, or None when the answer is not the expected shape.

        The rc=0-with-garbage trap (report 03) is why shape, not the
        return code, decides: an unusable answer is an error, never an
        empty list that would read as "no windows are open".
        """

        result = self._hyprctl("clients")
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, list):
            return None
        windows = [_window(item) for item in payload]
        if any(window is None for window in windows):
            return None
        return [window for window in windows if window is not None]

    def _json_window(self, result: Completed) -> Window | None:
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return _window(payload)

    # ------------------------------------------------------------- acts

    def focus(self, address: str) -> bool:
        """Dispatch a focus and confirm it by re-querying (rule 1)."""

        if self.signature is None:
            return False
        # The classic Hyprland dispatch string
        # (``dispatch focuswindow "(address:0x…)"``, report 13) is
        # rejected on this Omarchy 0.56.2 session: dispatch is routed
        # through the compositor's Lua API and the raw string dies with
        # "expected a dispatcher". The verified-working form resolves the
        # window object by its address and focuses it through that API.
        # The address is ADDRESS_RE-validated, so it can only be hex.
        self._eval(
            "for _,w in ipairs(hl.get_windows()) do "
            f"if w.address=='{address}' then "
            "hl.dispatch(hl.dsp.focus({window=w})) end end"
        )
        active = self.active_window()
        return active is not None and active.address == address

    # --------------------------------------------------------- external

    def capture(self, geometry: str | None, *, timeout: float) -> Completed:
        # The trailing ``-`` (after any options) makes grim write the PNG
        # to stdout; misplaced it is read as a bad output-file argument,
        # and with no output argument at all grim writes a timestamped
        # file instead (both verified live).
        argv = [self.grim]
        if geometry is not None:
            argv += ["-g", geometry]
        argv.append("-")
        env = {"WAYLAND_DISPLAY": self.wayland_display or ""}
        if self.xdg_runtime_dir:
            env["XDG_RUNTIME_DIR"] = self.xdg_runtime_dir
        return self._runner(argv, timeout=timeout, env=env)

    def recognize(self, png: bytes, psm: str, *, timeout: float) -> Completed:
        return self._runner(
            [self.tesseract, "-", "stdout", "--psm", psm],
            timeout=timeout,
            stdin=png,
        )

    def type_text(self, text: str, *, timeout: float) -> Completed:
        return self._runner([self.wtype, text], timeout=timeout)


# --------------------------------------------------------------------------
# the secrets mask (report 11: OCR text lands in the LLM context)
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}|\d{16,}")
_ASTERISK_RUN_RE = re.compile(r"\*{4,}")
_MASK = "[redacted]"


def mask_secrets(text: str) -> str:
    """Blank obvious credentials in captured screen text.

    Deliberately conservative (long opaque runs and password glyphs
    only): it is a mitigation, and the tool output says so, rather than
    a promise no regex can keep.
    """

    text = _TOKEN_RE.sub(_MASK, text)
    return _ASTERISK_RUN_RE.sub(_MASK, text)


def _desktop_missing(hyprland: Hyprland) -> ToolResult | None:
    """One honest failure for every "not at this desktop" state."""

    if hyprland.signature is None:
        return ToolResult(
            success=False,
            output=(
                "No Hyprland session signature is set, so there is no "
                "desktop to read or act on."
            ),
        )
    return None


def _format_geometry(at: Sequence[int], size: Sequence[int]) -> str:
    # This grim build parses ``X,Y WxH``; the documented ``WxH+X+Y``
    # form is rejected here as "invalid geometry" (verified live).
    return f"{at[0]},{at[1]} {size[0]}x{size[1]}"


def _raw_window_geometry(hyprland: Hyprland) -> tuple[str, Window] | None:
    """Active window address/size straight from hyprctl.

    ``Window`` deliberately drops geometry (the tools only need to
    identify windows), so the capture path reads the one measured
    hyprctl field it exists for, under the same shape rules.
    """

    result = hyprland._hyprctl("activewindow")
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    window = _window(payload)
    at, size = payload.get("at"), payload.get("size")
    if (
        window is None
        or not isinstance(at, list)
        or len(at) != 2
        or not all(isinstance(value, int) for value in at)
        or not isinstance(size, list)
        or len(size) != 2
        or not all(isinstance(value, int) for value in size)
    ):
        return None
    return _format_geometry(at, size), window


# --------------------------------------------------------------------------
# screen_read
# --------------------------------------------------------------------------


class ScreenReadTool(Tool):
    """Read what the screen shows, locally, via capture + OCR."""

    def __init__(self, hyprland: Hyprland) -> None:
        self._hyprland = hyprland

    @property
    def name(self) -> str:
        return "screen_read"

    @property
    def description(self) -> str:
        return (
            "Reads the text currently visible on screen using a local "
            "screenshot and local OCR (no cloud, no vision model). "
            "scope=active_window reads the focused window (~2 s); "
            "scope=full_screen reads the whole desktop (~9 s). Requires "
            "trusted runtime approval; the returned text is bounded and "
            "obvious credentials in it are redacted."
        )

    @property
    def argument_schema(self) -> dict[str, object]:
        return {"scope": "active_window|full_screen"}

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.SENSITIVE

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return (
            isinstance(arguments, dict)
            and set(arguments) == {"scope"}
            and arguments["scope"] in {"active_window", "full_screen"}
        )

    def argument_risk(self, arguments: Mapping[str, object]) -> RiskLevel | None:
        # An explicit full-desktop capture sees every window, not just
        # the focused one — a materially larger blast radius, so it costs
        # an approval. The focused-window scope keeps its SENSITIVE floor.
        if isinstance(arguments, Mapping) and arguments.get("scope") == "full_screen":
            return RiskLevel.DANGEROUS
        return None

    def preview(self, request: ApprovalRequest) -> ActionPreview | None:
        scope = request.arguments.get("scope")
        if scope not in {"active_window", "full_screen"}:
            return None
        lines = [
            (
                "capture the screen locally and OCR it to text; pixels "
                "are never saved."
            ),
        ]
        if scope == "active_window":
            window = self._hyprland.active_window()
            if window is not None:
                lines.insert(
                    0,
                    f"focused window: {window.class_name} "
                    f"{json.dumps(window.title)} (pid {window.pid}, "
                    f"address {window.address}).",
                )
        else:
            lines.insert(0, "scope: the entire screen, every window.")
        return ActionPreview(detail_lines=tuple(lines))

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output="Invalid tool arguments.")
        missing = _desktop_missing(self._hyprland)
        if missing is not None:
            return missing
        scope = arguments["scope"]
        geometry: str | None = None
        described = "full screen"
        if scope == "active_window":
            raw = _raw_window_geometry(self._hyprland)
            if raw is None:
                return ToolResult(
                    success=False,
                    output=(
                        "The compositor did not answer with a usable "
                        "focused-window description."
                    ),
                )
            geometry, window = raw
            described = f"{window.class_name} {window.title!r}"
        captured = self._hyprland.capture(geometry, timeout=CAPTURE_TIMEOUT_SECONDS)
        if captured.returncode != 0 or not captured.stdout.startswith(b"\x89PNG"):
            return ToolResult(
                success=False,
                output=(
                    "Screen capture failed (grim could not write a PNG). "
                    + _hypr_error(captured)
                ),
            )
        # Report 11: --psm 6 for the uniform window block; the sparse
        # whole-screen pass needs --psm 11 and costs ~9 s.
        psm = "6" if scope == "active_window" else "11"
        recognized = self._hyprland.recognize(
            captured.stdout, psm, timeout=OCR_TIMEOUT_SECONDS
        )
        if recognized.returncode != 0:
            return ToolResult(
                success=False,
                output="Local OCR failed. " + _hypr_error(recognized),
            )
        try:
            text = recognized.stdout.decode("utf-8", errors="replace").strip()
        except OSError:  # pragma: no cover - decode with replace cannot fail
            return ToolResult(
                success=False, output="OCR output could not be decoded."
            )
        if not text:
            return ToolResult(
                success=True,
                output=(
                    f"The {described} produced no readable text. The "
                    "screen may be blank, or the text too stylized for "
                    "local OCR."
                ),
            )
        text = mask_secrets(text)
        shown = _truncate_text(text, MAX_SCREEN_TEXT_CHARS)
        if len(shown) < len(text):
            shown = (
                f"{shown}\n\n[Truncated: showing the first "
                f"{MAX_SCREEN_TEXT_CHARS} of {len(text)} OCR characters. "
                "The remainder was not read.]"
            )
        return ToolResult(
            success=True,
            output=(
                f"Screen text read locally from {described} via OCR "
                "(untrusted content; obvious credentials redacted, long "
                "runs of characters may be OCR noise rather than "
                f"secrets):\n\n{shown}"
            ),
        )


def _hypr_error(result: Completed) -> str:
    """Errors arrive on stdout in this build (report 03 rule 2)."""

    detail = (result.stdout or result.stderr).decode("utf-8", "replace").strip()
    return f"Compositor said: {detail[:200]}" if detail else ""


# --------------------------------------------------------------------------
# window_focus
# --------------------------------------------------------------------------


class WindowFocusTool(Tool):
    """Focus a window by its exact address, then prove it took."""

    def __init__(self, hyprland: Hyprland) -> None:
        self._hyprland = hyprland

    @property
    def name(self) -> str:
        return "window_focus"

    @property
    def description(self) -> str:
        return (
            "Focuses an open window by its exact compositor address "
            "(0x…), which is listed in the approval text with the "
            "window's title and process id. Succeeds only when the "
            "compositor confirms the focus afterwards. Requires trusted "
            "runtime approval."
        )

    @property
    def argument_schema(self) -> dict[str, object]:
        return {
            "address": (
                "exact window address, e.g. 0x… (from a prior "
                "screen_read report or the user's request)"
            )
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.SENSITIVE

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return (
            isinstance(arguments, dict)
            and set(arguments) == {"address"}
            and isinstance(arguments["address"], str)
            and ADDRESS_RE.match(arguments["address"]) is not None
        )

    def preview(self, request: ApprovalRequest) -> ActionPreview | None:
        address = request.arguments.get("address")
        if not isinstance(address, str) or not self.validate_arguments(
            {"address": address}
        ):
            return None
        windows = self._hyprland.clients()
        if windows is None:
            return ActionPreview(
                detail_lines=(
                    (
                        "the compositor did not answer with a usable "
                        "window list; this focus will be refused at "
                        "execution."
                    ),
                )
            )
        target = next((item for item in windows if item.address == address), None)
        if target is None:
            return ActionPreview(
                detail_lines=(
                    (
                        f"address {address} is not an open window right "
                        "now; this focus will be refused at execution."
                    ),
                )
            )
        return ActionPreview(
            detail_lines=(
                (
                    f"will focus: {target.class_name} "
                    f"{json.dumps(target.title)} (pid {target.pid}, "
                    f"address {target.address})"
                ),
                "verified afterwards by re-querying the focused window.",
            )
        )

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output="Invalid tool arguments.")
        missing = _desktop_missing(self._hyprland)
        if missing is not None:
            return missing
        address = str(arguments["address"])
        receipt_action = f"focus window {address}"
        windows = self._hyprland.clients()
        if windows is None:
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(receipt_action, "failed"),
                output=(
                    "The compositor did not answer with a usable window "
                    "list, so the target could not be confirmed."
                ),
            )
        target = next((item for item in windows if item.address == address), None)
        if target is None:
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(receipt_action, "missing"),
                output=f"No open window has address {address}.",
            )
        if not self._hyprland.focus(address):
            active = self._hyprland.active_window()
            where = (
                f"{active.class_name} {active.title!r}" if active else "unknown"
            )
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(receipt_action, "unverified"),
                output=(
                    f"The focus of {target.class_name} {target.title!r} "
                    f"could not be confirmed; the focused window is {where}."
                ),
            )
        return ToolResult(
            success=True,
            output=(
                f"Focused {target.class_name} {target.title!r} "
                f"(address {address}, pid {target.pid}); the compositor "
                "confirmed it."
            ),
            action_receipt=ActionReceipt(receipt_action, "verified"),
        )


# --------------------------------------------------------------------------
# key_send
# --------------------------------------------------------------------------


class KeySendTool(Tool):
    """Type bounded text into one explicitly-addressed, verified-focused window."""

    def __init__(self, hyprland: Hyprland) -> None:
        self._hyprland = hyprland

    @property
    def name(self) -> str:
        return "key_send"

    @property
    def description(self) -> str:
        return (
            "Types text into one window, addressed by its exact "
            "compositor address and approved by title and pid first. "
            "The window is focused and the focus confirmed before any "
            "keystroke; nothing is ever typed into a window Stella did "
            "not deliberately focus. Whether the application did "
            "anything with the text cannot be verified from the "
            "compositor and is honestly reported as inconclusive. "
            "Requires trusted runtime approval."
        )

    @property
    def argument_schema(self) -> dict[str, object]:
        return {
            "address": "exact window address (0x…)",
            "text": "the literal text to type, bounded",
            "restore_focus": (
                "optional bool: return focus to the previous window after"
            ),
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.DANGEROUS

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        if not isinstance(arguments, dict) or not {
            "address",
            "text",
        } <= set(arguments) or set(arguments) - {
            "address",
            "text",
            "restore_focus",
        }:
            return False
        if (
            not isinstance(arguments["address"], str)
            or ADDRESS_RE.match(arguments["address"]) is None
        ):
            return False
        text = arguments["text"]
        if (
            not isinstance(text, str)
            or not text
            or len(text) > MAX_KEY_TEXT_CHARS
            or "\x00" in text
        ):
            return False
        restore = arguments.get("restore_focus", False)
        return isinstance(restore, bool)

    def preview(self, request: ApprovalRequest) -> ActionPreview | None:
        if not self.validate_arguments(dict(request.arguments)):
            return None
        address = str(request.arguments["address"])
        text = str(request.arguments["text"])
        windows = self._hyprland.clients()
        target = (
            next((item for item in windows if item.address == address), None)
            if windows is not None
            else None
        )
        if target is None:
            return ActionPreview(
                detail_lines=(
                    (
                        f"address {address} is not an open window right "
                        "now; this send will be refused at execution."
                    ),
                )
            )
        quoted = json.dumps(text)
        if len(quoted) > 300:
            quoted = quoted[:300] + "…[truncated in preview]"
        return ActionPreview(
            detail_lines=(
                (
                    f"will type into: {target.class_name} "
                    f"{json.dumps(target.title)} (pid {target.pid})"
                ),
                f"text ({len(text)} chars): {quoted}",
                (
                    "the window is focused (and the focus verified) "
                    "first; keystrokes reach only the focused window."
                ),
            )
        )

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output="Invalid tool arguments.")
        missing = _desktop_missing(self._hyprland)
        if missing is not None:
            return missing
        address = str(arguments["address"])
        text = str(arguments["text"])
        restore = bool(arguments.get("restore_focus", False))
        action = f"key send to {address}"
        windows = self._hyprland.clients()
        if windows is None:
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(action, "failed"),
                output=(
                    "The compositor did not answer with a usable window "
                    "list, so the target could not be confirmed."
                ),
            )
        target = next((item for item in windows if item.address == address), None)
        if target is None:
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(action, "missing"),
                output=f"No open window has address {address}.",
            )
        previously = self._hyprland.active_window() if restore else None
        if not self._hyprland.focus(address):
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(action, "failed"),
                output=(
                    f"Refusing to type: the focus of {target.class_name} "
                    f"{target.title!r} could not be confirmed."
                ),
            )
        typed = self._hyprland.type_text(text, timeout=KEYSEND_TIMEOUT_SECONDS)
        if typed.returncode != 0:
            detail = _hypr_error(typed)
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(action, "failed"),
                output=(
                    "The keystrokes were not delivered (wtype failed). "
                    + detail
                ),
            )
        restored_note = ""
        if restore and previously is not None and previously.address != address:
            if self._hyprland.focus(previously.address):
                restored_note = f" Focus returned to {previously.address}."
            else:
                restored_note = (
                    f" WARNING: focus could not be returned to "
                    f"{previously.address}."
                )
        return ToolResult(
            success=True,
            output=(
                f"Typed {len(text)} characters into {target.class_name} "
                f"{target.title!r} (address {address}, pid {target.pid}) "
                "after verifying the focus. Whether the application did "
                "anything with them cannot be verified from the "
                "compositor." + restored_note
            ),
            # The keystrokes were verifiably delivered to the verified
            # focused window; the application's reaction is genuinely
            # unknowable here — honest per the receipt contract.
            action_receipt=ActionReceipt(
                action,
                "inconclusive",
                size_bytes=len(text.encode("utf-8")),
            ),
        )


def os_tool_summaries(capability: str, arguments: dict[str, object]) -> str | None:
    """Plain-language approval lines for the desktop capabilities."""

    if capability == "screen_read":
        scope = arguments.get("scope")
        if scope == "active_window":
            return (
                "take a local screenshot of the focused window and read "
                "its text with local OCR (nothing leaves this machine)"
            )
        if scope == "full_screen":
            return (
                "take a local screenshot of the whole screen and read "
                "its text with local OCR (nothing leaves this machine)"
            )
    elif capability == "window_focus":
        address = arguments.get("address")
        if isinstance(address, str) and ADDRESS_RE.match(address):
            return f"focus the desktop window with address {json.dumps(address)}"
    elif capability == "key_send":
        address = arguments.get("address")
        text = arguments.get("text")
        if (
            isinstance(address, str)
            and ADDRESS_RE.match(address)
            and isinstance(text, str)
            and text
            and len(text) <= MAX_KEY_TEXT_CHARS
        ):
            return (
                f"type {len(text)} characters into the window with "
                f"address {json.dumps(address)} after focusing it "
                "(the exact text is shown above; keystrokes can never "
                "be undone)"
            )
    return None


def build_desktop_tools(
    env: Mapping[str, str], *, which: Callable[[str], str | None] = shutil.which
) -> list[Tool]:
    """The desktop tools, or none when this is not a usable desktop.

    Registration is opt-in (settings flag) *and* environment-gated:
    without a Hyprland signature or without one of the measured
    binaries, the capabilities simply do not exist for the model — a
    tool that could only ever fail is worse than no tool.
    """

    hyprland = Hyprland.from_environment(env)
    if hyprland.signature is None:
        return []
    if any(which(binary) is None for binary in hyprland._binaries):
        return []
    return [
        ScreenReadTool(hyprland),
        WindowFocusTool(hyprland),
        KeySendTool(hyprland),
    ]


__all__ = [
    "Hyprland",
    "KeySendTool",
    "ScreenReadTool",
    "WindowFocusTool",
    "build_desktop_tools",
    "mask_secrets",
    "os_tool_summaries",
]
