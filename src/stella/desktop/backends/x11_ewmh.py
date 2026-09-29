"""Unimplemented adapter: any X11 session, through EWMH + xdotool.

Planned surface — the Extended Window Manager Hints (EWMH) properties on the root
window (``_NET_CLIENT_LIST``, ``_NET_ACTIVE_WINDOW``, ``_NET_WM_NAME``,
``_NET_WM_PID``) read with ``xprop``/``xdotool``, focused with
``xdotool windowactivate --sync``, typed with ``xdotool type``, captured
per window with ``xwd`` or ImageMagick ``import``.

Session marker: ``XDG_SESSION_TYPE=x11`` **and** ``$DISPLAY``, and then
actually asking the display whether it is EWMH-compliant
(``_NET_SUPPORTING_WM_CHECK`` set and pointing at a real window). The
env var alone must not win: a stale ``XDG_SESSION_TYPE`` in a shell
profile is exactly the trap registry.py documents.

Facts a real machine must confirm before any of this is trusted:

* are window ids decimal or ``0x``-hex in this tool's output, and what is
  the canonical form to hand back as the opaque id (the tools will not
  guess)?
* does ``xdotool getwindowfocus`` follow keyboard focus or input focus,
  and does EWMH ``windowactivate`` actually move it on this WM — some WMs
  honour the request but refuse the focus change, so the re-query
  (``_NET_ACTIVE_WINDOW`` read back) is the only proof, as always?
* which windows does ``_NET_CLIENT_LIST`` omit (override-redirect, dock,
  splash) and is that list ordered per-ws or globally?
* does ``xdotool search`` exit 0 with empty output when nothing matches —
  the inverse of the ``hyprctl`` rc-0-with-garbage trap, where empty must
  mean "definitely none" and unparseable must mean "unusable"?
* the exact geometry syntax of the chosen capture tool
  (``import -geometry WxH+X+Y`` differs from grim's ``X,Y WxH``), whether
  it writes PNG or PPM/BMP to stdout, and how to prove it never writes a
  file, and
* does ``xclip``/``xdotool type`` handle the full unicode range and
  multi-key compose sequences for the text this tool is allowed to type,
  and how many characters per call before it becomes a keystroke storm.

Nothing here is implemented: ``probe`` returns ``None``, so an X11 user
gets no desktop tools rather than broken ones.
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
