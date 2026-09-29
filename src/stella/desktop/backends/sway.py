"""Unimplemented adapter: sway (and any i3-protocol-compatible WM).

Planned surface — ``swaymsg`` against the IPC socket, i.e.
``swaymsg -r -t get_tree`` / ``-t get_workspaces`` for reads and
``swaymsg [con_id="<id>"] focus`` for acts. Capture is ``grim`` (sway is
wlroots, so the Hyprland capture notes apply with a different geometry
parser) and text injection is ``wtype`` or ``ydotool``.

Session marker: ``$SWAYSOCK`` (fall back to ``i3 --get-socketpath``),
plus ``XDG_CURRENT_DESKTOP`` containing ``sway`` so an i3-throwback
socket left behind by another session cannot win the probe.

Facts a real machine must confirm before any of this is trusted — the
same class of measurement that made the Hyprland adapter the shape it is:

* does ``swaymsg`` answer garbage with rc 0 the way ``hyprctl`` does, and
  on which stream does a rejected command report its error?
* is the tree response valid ``-t get_tree`` JSON only with ``-r``, and
  what exactly marks the focused window (``"focused": true`` on the
  window node, ``nodes`` vs ``floating_nodes``, the scratchpad)?
* what is the stable id for a window — ``con_id``, ``pid``, ``window``
  (the X11 id under XWayland) — and is it unique across outputs?
* does ``[con_id="N"] focus`` need quoting rules that make an untrusted
  id dangerous, and does ``focus`` silently no-op on a window on another
  workspace (then the re-query is what saves us, as with Hyprland)?
* the exact ``grim`` geometry syntax this wlroots version accepts, and
  whether the output selection needs ``-o`` (multi-monitor).
* is ``wtype`` enough for unicode text under sway, or does the machine
  need ``ydotool`` with its group-membership/daemon requirement (which
  would change what "usable desktop" means at probe time)?

Nothing here is implemented: ``probe`` returns ``None``, so no tool is
registered for sway and the model never sees a capability that could only
fail.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping

from stella.desktop.capabilities import Desktop
from stella.desktop.runner import Runner, subprocess_runner


def probe(
    env: Mapping[str, str],
    runner: Runner = subprocess_runner,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> Desktop | None:
    return None  # not yet implemented
