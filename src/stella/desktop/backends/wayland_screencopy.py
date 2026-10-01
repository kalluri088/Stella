"""Unimplemented adapter: any compositor speaking the wlr protocols.

This is the last-resort Wayland adapter, meant for wlroots-derived
compositors that are neither Hyprland nor sway (labwc, river, Wayfire,
cage…) — and, unlike every other adapter, it must be *honest about
what it cannot do*: there is no standard Wayland "list my windows" or
"focus this window" protocol. A compositor that implements
``zwlr_screencopy_manager_v1`` alone can offer capture and nothing else.

Planned surface — the wlr portal-style client tools:

* capture: ``grim`` (it speaks ``zwlr_screencopy_manager_v1`` /
  ``ext-image-copy-capture`` on a newer build).
* reads: ``wlrctl xdg-toplevel list`` where the compositor implements
  ``zwlr_foreign_toplevel_management``, otherwise the foreign-toplevel
  list is simply not available.
* focus: ``wlrctl xdg-toplevel <handle> activate``, then a re-query of
  the same foreign-toplevel list for the ``activated`` state.
* text: ``wtype`` (needs no special protocol) or ``ydotool`` (needs its
  uinput daemon and group membership).

Session marker: ``WAYLAND_DISPLAY`` set, and the adapter asking the
compositor which globals exist rather than trusting the env var — a
Wayland session with no supported capture protocol must fall through to
"no desktop", not register tools that fail.

Facts a real machine must confirm before any of this is trusted:

* how to enumerate globals from a client process (``wlrctl``/``wayland-info
  --name``/binding version), and which screencopy version this compositor
  actually advertises — grim silently fails on some;
* whether ``grim`` needs ``-o <output>`` here and what its geometry syntax
  is on this compositor (the Hyprland measurement ``X,Y WxH`` is *this*
  build's grim, and it must be re-measured rather than assumed);
* whether ``wlrctl`` exit codes distinguish "no such handle" from "the
  compositor does not implement the protocol" — that difference is
  ``None`` versus ``[]`` in the reader contract, and getting it wrong is
  the exact trap this seam exists to prevent;
* whether foreign-toplevel handles are stable enough to be an opaque id
  across two calls (they are recycled on some compositors, which would
  mean focus can be verified but not requested safely);
* and, decisively: if a compositor can offer capture but no verified
  focus, this adapter must return a ``Desktop`` whose activator refuses
  every act (``activate`` → ``False``, ``send_text`` →
  ``SendUnavailable``) rather than one that pretends.

Nothing here is implemented: ``probe`` returns ``None``.
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
