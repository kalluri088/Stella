"""Stella's desktop seam: a few tools, one capability surface, many adapters.

``os_tools.py`` was one machine. This package is the contract that lets
each desktop be a file: :mod:`stella.desktop.capabilities` says what a
desktop must be able to do, :mod:`stella.desktop.backends.hyprland` is
the measured reference adapter, the sibling stubs are unwritten adapters
with their evidence list, :mod:`stella.desktop.registry` decides which
one runs, and :mod:`stella.desktop.tools` holds the capabilities —
which cannot see which desktop they are on, and must not need to.

Registration keeps the old semantics exactly: the capability is opt-in
(the settings flag, env override ``STELLA_OS_TOOLS``) *and*
environment-gated by :func:`build_desktop_tools`. No usable desktop means
no tools at all, so the model is never offered something that can only
fail.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping

from stella.desktop.registry import select_desktop
from stella.desktop.tools import (
    DesktopControlTool,
    KeySendTool,
    ScreenReadTool,
    WindowFocusTool,
    tools_for,
)
from stella.tools import Tool

__all__ = [
    "DesktopControlTool",
    "KeySendTool",
    "ScreenReadTool",
    "WindowFocusTool",
    "build_desktop_tools",
    "select_desktop",
    "tools_for",
]


def build_desktop_tools(
    env: Mapping[str, str], *, which: Callable[[str], str | None] = shutil.which
) -> list[Tool]:
    """The desktop tools, or none when this is not a usable desktop.

    The gate is the registry's: a desktop exists only when one adapter
    recognized its own session and found every binary it needs, so a tool
    that could only ever fail never reaches the model.
    """

    desktop = select_desktop(env, which=which)
    if desktop is None:
        return []
    return tools_for(desktop, which)
