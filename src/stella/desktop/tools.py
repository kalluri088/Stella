"""The desktop tools, written against :class:`Desktop` and nothing else.

No rule in this module mentions a compositor command, a binary or an id
shape — those belong to the adapters. What stays here is the safety model
the reports forced, and it is the same whatever desktop is underneath:

* ``screen_read`` is capture plus local OCR: pixels go from the adapter to
  the recognizer and are dropped, the text is masked for credentials and
  bounded before the model reads it (report 11).
* ``window_focus`` and ``key_send`` resolve their target against the
  **live window list**, act, and believe only the adapter's re-queried
  answer. ``key_send`` in particular never types into a window it did not
  deliberately focus, and never guesses a target by title when several
  windows share one — the id is the only accepted address.
* Every failure keeps the ``None``-versus-``[]`` distinction: an unusable
  answer is reported as "the desktop did not answer", never as "no windows
  are open", because the second would refuse a real target in silence.
* Capabilities grow inside argument values, never as new verbs: the three
  original tools plus ``desktop_control`` — one verb-tool carrying
  launch/close/focus/move as argument values, added as a deliberate,
  owner-requested fourth surface (AGENTS.md rules 13/16) because opening
  and moving applications is what the owner asked for daily and no
  argument of the first three could honestly grow into it.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping

from stella.desktop.capabilities import (
    MAX_KEY_TEXT_CHARS,
    MAX_SCREEN_TEXT_CHARS,
    OCR_SCREEN,
    OCR_WINDOW,
    CaptureUnavailable,
    Desktop,
    DesktopUnavailable,
    RecognizeUnavailable,
    Region,
    SendUnavailable,
    Window,
    launch_command_usable,
    window_id_usable,
)
from stella.desktop.masking import mask_secrets
from stella.tools import (
    ActionPreview,
    ActionReceipt,
    ApprovalRequest,
    RiskLevel,
    Tool,
    ToolResult,
    _truncate_text,
)

_INVALID_ARGUMENTS = "Invalid tool arguments."
_NO_WINDOW_LIST = (
    "The desktop did not answer with a usable window list, so the "
    "target could not be confirmed."
)
_NO_FOCUSED_WINDOW = (
    "The desktop did not answer with a usable focused-window description."
)


def _session_line(desktop: Desktop) -> str:
    """Every approval preview says *which* session it means."""

    return f"desktop: {desktop.name} — {desktop.session_note}"


def _find(windows: list[Window], window_id: str) -> Window | None:
    return next((item for item in windows if item.id == window_id), None)


def _describe(window: Window) -> str:
    return f"{window.class_name} {window.title!r}"


def _focus_result(desktop: Desktop, window_id: str) -> ToolResult:
    """One focus: resolve against the live list, act, believe the re-query.

    Shared by ``window_focus`` and ``desktop_control``'s focus action so
    the two paths can never drift apart on the safety contract.
    """

    action = f"focus window {window_id}"
    windows = desktop.reader.windows()
    if windows is None:
        return ToolResult(
            success=False,
            action_receipt=ActionReceipt(action, "failed"),
            output=_NO_WINDOW_LIST,
        )
    target = _find(windows, window_id)
    if target is None:
        return ToolResult(
            success=False,
            action_receipt=ActionReceipt(action, "missing"),
            output=f"No open window has id {window_id}.",
        )
    if not desktop.activator.activate(window_id):
        active = desktop.reader.active_window()
        where = _describe(active) if active else "unknown"
        return ToolResult(
            success=False,
            action_receipt=ActionReceipt(action, "unverified"),
            output=(
                f"The focus of {_describe(target)} could not be "
                f"confirmed; the focused window is {where}."
            ),
        )
    return ToolResult(
        success=True,
        output=(
            f"Focused {_describe(target)} (id {window_id}, pid "
            f"{target.pid}); the desktop confirmed it."
        ),
        action_receipt=ActionReceipt(action, "verified"),
    )


# --------------------------------------------------------------------------
# screen_read
# --------------------------------------------------------------------------


class ScreenReadTool(Tool):
    """Read what the screen shows, locally, via capture + OCR."""

    def __init__(self, desktop: Desktop) -> None:
        self._desktop = desktop

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
        lines: list[str] = []
        if scope == "active_window":
            window = self._desktop.reader.active_window()
            if window is not None:
                lines.append(
                    f"focused window: {window.class_name} "
                    f"{json.dumps(window.title)} (pid {window.pid}, "
                    f"window id {window.id})."
                )
        else:
            lines.append("scope: the entire screen, every window.")
        lines.append(_session_line(self._desktop))
        lines.append(
            "capture the screen locally and OCR it to text; pixels are "
            "never saved."
        )
        return ActionPreview(detail_lines=tuple(lines))

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output=_INVALID_ARGUMENTS)
        scope = arguments["scope"]
        region: Region | None = None
        described = "full screen"
        if scope == "active_window":
            window = self._desktop.reader.active_window()
            # A window whose adapter cannot say where it is would have to
            # be captured as "the whole screen" — a wider scope than the
            # user approved, so it is a failure, not a fallback.
            if window is None or window.region is None:
                return ToolResult(success=False, output=_NO_FOCUSED_WINDOW)
            region = window.region
            described = _describe(window)
        try:
            png = self._desktop.capture.capture(region)
        except CaptureUnavailable as failure:
            return ToolResult(success=False, output=str(failure))
        hint = OCR_WINDOW if scope == "active_window" else OCR_SCREEN
        try:
            text = self._desktop.recognizer.recognize(png, hint)
        except RecognizeUnavailable as failure:
            return ToolResult(success=False, output=str(failure))
        if not text:
            return ToolResult(
                success=True,
                output=(
                    f"The {described} produced no readable text. The "
                    "screen may be blank, or the text too stylized for "
                    "local OCR."
                ),
            )
        masked = mask_secrets(text)
        shown = _truncate_text(masked, MAX_SCREEN_TEXT_CHARS)
        # The announcement compares against the bound, not against the
        # shown text: _truncate_text also rstrips, and a trailing space is
        # not a truncation the user should be told about.
        if len(masked) > MAX_SCREEN_TEXT_CHARS:
            shown = (
                f"{shown}\n\n[Truncated: showing the first "
                f"{MAX_SCREEN_TEXT_CHARS} of {len(masked)} OCR characters. "
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


# --------------------------------------------------------------------------
# window_focus
# --------------------------------------------------------------------------


class WindowFocusTool(Tool):
    """Focus a window by its exact id, then prove it took."""

    def __init__(self, desktop: Desktop) -> None:
        self._desktop = desktop

    @property
    def name(self) -> str:
        return "window_focus"

    @property
    def description(self) -> str:
        return (
            "Focuses an open window by its exact window id, which is "
            "listed in the approval text with the window's title and "
            "process id. Succeeds only when the desktop confirms the "
            "focus afterwards. Requires trusted runtime approval."
        )

    @property
    def argument_schema(self) -> dict[str, object]:
        return {
            "id": (
                "exact window id (from a prior screen_read report or the "
                "user's request)"
            )
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.SENSITIVE

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return (
            isinstance(arguments, dict)
            and set(arguments) == {"id"}
            and window_id_usable(arguments["id"])
        )

    def preview(self, request: ApprovalRequest) -> ActionPreview | None:
        window_id = request.arguments.get("id")
        if not window_id_usable(window_id):
            return None
        session = _session_line(self._desktop)
        windows = self._desktop.reader.windows()
        if windows is None:
            return ActionPreview(
                detail_lines=(
                    session,
                    (
                        "the desktop did not answer with a usable window "
                        "list; this focus will be refused at execution."
                    ),
                )
            )
        target = _find(windows, str(window_id))
        if target is None:
            return ActionPreview(
                detail_lines=(
                    session,
                    (
                        f"window id {window_id} is not an open window "
                        "right now; this focus will be refused at "
                        "execution."
                    ),
                )
            )
        return ActionPreview(
            detail_lines=(
                session,
                (
                    f"will focus: {target.class_name} "
                    f"{json.dumps(target.title)} (pid {target.pid}, "
                    f"window id {target.id})"
                ),
                "verified afterwards by re-querying the focused window.",
            )
        )

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output=_INVALID_ARGUMENTS)
        return _focus_result(self._desktop, str(arguments["id"]))


# --------------------------------------------------------------------------
# key_send
# --------------------------------------------------------------------------


class KeySendTool(Tool):
    """Type bounded text into one explicitly-identified, verified-focused window."""

    def __init__(self, desktop: Desktop) -> None:
        self._desktop = desktop

    @property
    def name(self) -> str:
        return "key_send"

    @property
    def description(self) -> str:
        return (
            "Types text into one window, addressed by its exact window id "
            "and approved by title and pid first. The window is focused "
            "and the focus confirmed before any keystroke; nothing is ever "
            "typed into a window Stella did not deliberately focus. "
            "Whether the application did anything with the text cannot be "
            "verified from the desktop and is honestly reported as "
            "inconclusive. Requires trusted runtime approval."
        )

    @property
    def argument_schema(self) -> dict[str, object]:
        return {
            "id": "exact window id",
            "text": "the literal text to type, bounded",
            "restore_focus": (
                "optional bool: return focus to the previous window after"
            ),
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.DANGEROUS

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        if (
            not isinstance(arguments, dict)
            or not {"id", "text"} <= set(arguments)
            or set(arguments) - {"id", "text", "restore_focus"}
        ):
            return False
        if not window_id_usable(arguments["id"]):
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
        window_id = str(request.arguments["id"])
        text = str(request.arguments["text"])
        windows = self._desktop.reader.windows()
        target = _find(windows, window_id) if windows is not None else None
        session = _session_line(self._desktop)
        if target is None:
            return ActionPreview(
                detail_lines=(
                    session,
                    (
                        f"window id {window_id} is not an open window "
                        "right now; this send will be refused at "
                        "execution."
                    ),
                )
            )
        quoted = json.dumps(text)
        if len(quoted) > 300:
            quoted = quoted[:300] + "…[truncated in preview]"
        return ActionPreview(
            detail_lines=(
                session,
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
            return ToolResult(success=False, output=_INVALID_ARGUMENTS)
        window_id = str(arguments["id"])
        text = str(arguments["text"])
        restore = bool(arguments.get("restore_focus", False))
        action = f"key send to {window_id}"
        windows = self._desktop.reader.windows()
        if windows is None:
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(action, "failed"),
                output=_NO_WINDOW_LIST,
            )
        target = _find(windows, window_id)
        if target is None:
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(action, "missing"),
                output=f"No open window has id {window_id}.",
            )
        previously = self._desktop.reader.active_window() if restore else None
        # The heart of the safety model, in this order and no other:
        # resolve against the live list, activate, let the adapter
        # re-verify the activation, and only then type.
        if not self._desktop.activator.activate(window_id):
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(action, "failed"),
                output=(
                    f"Refusing to type: the focus of {_describe(target)} "
                    "could not be confirmed."
                ),
            )
        try:
            self._desktop.keys.send_text(text)
        except SendUnavailable as failure:
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(action, "failed"),
                output=str(failure),
            )
        restored_note = ""
        if (
            restore
            and previously is not None
            and previously.id != window_id
            and self._desktop.activator.activate(previously.id)
        ):
            restored_note = f" Focus returned to {previously.id}."
        elif restore and previously is not None and previously.id != window_id:
            restored_note = (
                f" WARNING: focus could not be returned to {previously.id}."
            )
        return ToolResult(
            success=True,
            output=(
                f"Typed {len(text)} characters into {_describe(target)} "
                f"(id {window_id}, pid {target.pid}) after verifying the "
                "focus. Whether the application did anything with them "
                "cannot be verified from the desktop." + restored_note
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


_CONTROL_TARGETS = ("app", "window")
_CONTROL_ACTIONS = ("launch", "close", "focus", "move")
_MAX_WORKSPACE = 1000


def _workspace_value(value: object, *, required: bool) -> int | None | bool:
    """Shared workspace validation: None absent, False invalid, int valid."""

    if value is None:
        return None if not required else False
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    if not 1 <= value <= _MAX_WORKSPACE:
        return False
    return value


def _launcher_resolvable(command: str, which: Callable[[str], str | None]) -> bool:
    """Whether the program a launch proposes actually exists.

    The first word of a validated command must be a real binary: an
    absolute path we can execute, or a plain name found through PATH.
    This is a capability fact, not a permission — an unresolvable name
    could only ever produce a silent non-launch.
    """

    first = command.split(maxsplit=1)[0]
    if first.startswith("/"):
        return os.access(first, os.X_OK)
    return which(first) is not None


class DesktopControlTool(Tool):
    """Launch, close, focus or move apps and windows — always approved."""

    def __init__(
        self, desktop: Desktop, which: Callable[[str], str | None] = shutil.which
    ) -> None:
        self._desktop = desktop
        self._which = which

    @property
    def name(self) -> str:
        return "desktop_control"

    @property
    def description(self) -> str:
        return (
            "Controls desktop applications and windows: target=app with "
            "action=launch starts one program (name: a plain command like "
            "'chromium'; optional workspace: an int puts its window on "
            "that workspace). target=window with action=close|focus|move "
            "acts on one window by its exact id (move also needs "
            "workspace). Every use needs trusted approval naming the "
            "literal program or window; acting succeeds only when the "
            "desktop confirms the result afterwards."
        )

    @property
    def argument_schema(self) -> dict[str, object]:
        return {
            "target": "app|window",
            "action": "launch (app) | close|focus|move (window)",
            "name": "program command for target=app (plain, no shell "
            "characters, spaces allowed for arguments)",
            "id": "exact window id for target=window",
            "workspace": "optional for launch, required for move: positive "
            "workspace number",
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.DANGEROUS

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        if (
            not isinstance(arguments, dict)
            or set(arguments) - {"target", "action", "name", "id", "workspace"}
        ):
            return False
        target = arguments.get("target")
        action = arguments.get("action")
        if target not in _CONTROL_TARGETS or action not in _CONTROL_ACTIONS:
            return False
        workspace = _workspace_value(arguments.get("workspace"), required=False)
        if workspace is False:
            return False
        if target == "app":
            return (
                action == "launch"
                and "id" not in arguments
                and launch_command_usable(arguments.get("name"))
                and _launcher_resolvable(str(arguments.get("name", "")), self._which)
            )
        if action == "launch" or "name" in arguments:
            return False
        if not window_id_usable(arguments.get("id")):
            return False
        if action == "move":
            return _workspace_value(arguments.get("workspace"), required=True) is not False
        return workspace is None

    def preview(self, request: ApprovalRequest) -> ActionPreview | None:
        arguments = dict(request.arguments)
        if not self.validate_arguments(arguments):
            return None
        session = _session_line(self._desktop)
        lines = [session]
        workspace = arguments.get("workspace")
        if arguments["target"] == "app":
            name = str(arguments["name"])
            landing = (
                f" and move its window to workspace {workspace}"
                if workspace is not None
                else ""
            )
            lines += [
                f"will launch: {name}{landing}",
                (
                    "the program starts as if you launched it yourself; "
                    "closing or killing it later is the same approved act."
                ),
            ]
            return ActionPreview(detail_lines=tuple(lines))
        window_id = str(arguments["id"])
        action = str(arguments["action"])
        windows = self._desktop.reader.windows()
        target = _find(windows, window_id) if windows is not None else None
        if target is None:
            note = (
                "the desktop did not answer with a usable window list"
                if windows is None
                else f"window id {window_id} is not an open window right now"
            )
            return ActionPreview(
                detail_lines=(session, f"{note}; this will be refused at execution.")
            )
        described = (
            f"{target.class_name} {json.dumps(target.title)} "
            f"(pid {target.pid}, window id {target.id}, "
            f"workspace {target.workspace})"
        )
        if action == "close":
            lines.append(f"will close: {described}")
            lines.append("closing is not undoable from the desktop; unsaved "
                         "work in that window goes with it.")
        elif action == "move":
            lines.append(f"will move: {described}")
            lines.append(f"destination workspace: {workspace} (silent — "
                         "your focus will not be stolen).")
        else:
            lines.append(f"will focus: {described}")
        return ActionPreview(detail_lines=tuple(lines))

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output=_INVALID_ARGUMENTS)
        manager = self._desktop.manager
        if manager is None:
            return ToolResult(
                success=False,
                output=(
                    "This desktop cannot manage windows, so nothing was "
                    "launched, closed, focused or moved."
                ),
            )
        target = str(arguments["target"])
        action = str(arguments["action"])
        if target == "app":
            return self._run(
                f"launch {arguments['name']}",
                lambda: bool(
                    manager.launch(
                        str(arguments["name"]),
                        arguments.get("workspace"),
                    )
                ),
                accepted=(
                    "Hyprland accepted the launch, but no new window "
                    "appeared in time — the program may still be starting "
                    "or may have failed to open a window."
                ),
                done=lambda: "The desktop confirmed a new window appeared.",
            )
        window_id = str(arguments["id"])
        if action == "focus":
            return _focus_result(self._desktop, window_id)
        windows = self._desktop.reader.windows()
        if windows is None:
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(f"{action} window {window_id}", "failed"),
                output=_NO_WINDOW_LIST,
            )
        found = _find(windows, window_id)
        if found is None:
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(f"{action} window {window_id}", "missing"),
                output=f"No open window has id {window_id}.",
            )
        if action == "close":
            return self._run(
                f"close window {window_id}",
                lambda: bool(manager.close(window_id)),
                accepted=(
                    f"The close of {_describe(found)} could not be "
                    "confirmed; the window may still be open."
                ),
                done=lambda: f"Closed {_describe(found)}; the desktop confirmed it.",
            )
        workspace = int(arguments["workspace"])
        return self._run(
            f"move window {window_id} to workspace {workspace}",
            lambda: bool(manager.move(window_id, workspace)),
            accepted=(
                f"The move of {_describe(found)} to workspace {workspace} "
                "could not be confirmed; check before retrying."
            ),
            done=lambda: (
                f"Moved {_describe(found)} to workspace {workspace}; the "
                "desktop confirmed it."
            ),
        )

    def _run(
        self,
        action: str,
        act: Callable[[], bool],
        *,
        accepted: str,
        done: Callable[[], str],
    ) -> ToolResult:
        """One managed act: DesktopUnavailable is failure, False is unverified."""

        try:
            verified = act()
        except DesktopUnavailable as failure:
            return ToolResult(
                success=False,
                action_receipt=ActionReceipt(action, "failed"),
                output=str(failure),
            )
        if verified:
            return ToolResult(
                success=True,
                output=done(),
                action_receipt=ActionReceipt(action, "verified"),
            )
        return ToolResult(
            success=False,
            action_receipt=ActionReceipt(action, "unverified"),
            output=accepted,
        )


def tools_for(
    desktop: Desktop, which: Callable[[str], str | None] = shutil.which
) -> list[Tool]:
    """The desktop tools for one desktop, always together (rule 6).

    ``desktop_control`` joins only when the adapter proved it can manage
    windows: a desktop without a manager is not offered an action that
    could only fail (invariant 5).
    """

    tools: list[Tool] = [
        ScreenReadTool(desktop),
        WindowFocusTool(desktop),
        KeySendTool(desktop),
    ]
    if desktop.manager is not None:
        tools.append(DesktopControlTool(desktop, which))
    return tools


__all__ = [
    "DesktopControlTool",
    "KeySendTool",
    "ScreenReadTool",
    "WindowFocusTool",
    "tools_for",
]
