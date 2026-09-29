"""Which desktop is this? Ask in a fixed order, believe the answer.

``select_desktop`` is the only place that decides which adapter runs, and
the order is a documented decision, not a list to shuffle:

1. ``hyprland`` — ``HYPRLAND_INSTANCE_SIGNATURE`` is the strongest marker
   on this machine: it is per-session, set only by a running Hyprland, and
   the only adapter proven working here (reports 03/11/13).
2. ``sway`` — ``$SWAYSOCK``/i3 IPC is likewise per-session, but a leftover
   socket from an i3/sway nested session is a real thing, so sway's probe
   must answer over the socket rather than trust the variable.
3. ``kde`` — ``XDG_CURRENT_DESKTOP`` is a *desktop-file* label, not proof
   of a session, so it only ranks this high because the KDE probe also has
   to reach KWin over D-Bus and find a typing binary to be usable.
4. ``gnome`` — same reasoning: the GNOME Shell bus name answering *is* the
   marker; the env var is not.
5. ``x11_ewmh`` — ``XDG_SESSION_TYPE=x11`` plus ``$DISPLAY`` plus a real
   EWMH reply. Last among the full adapters because an XWayland client
   inside a Wayland session has a perfectly good ``$DISPLAY``, and must not
   win a probe against the compositor the user is actually typing into.
6. ``wayland_screencopy`` — the generic wlroots fallback, last because it
   is the least certain about what it can offer, and it should only be
   reached when no specific adapter claimed the session.

Two rules hold everywhere:

* **A marker is a hint, an answer is proof.** A backend that *can* ask
  its compositor must ask; no adapter may register on one environment
  variable alone when it has a way to be wrong. A session marker for a
  compositor the user is not in must not win.
* **A desktop must be complete or absent.** Each probe checks its own
  binaries through the injected ``which``; anything missing means ``None``
  and the next candidate is tried. Invariant 5: an unusable environment
  registers no tools, because a tool that can only fail is worse than no
  tool.

An adapter that raises while probing is treated the same as one that
returns ``None``: a broken candidate never takes the assistant down, and
never wins the order. That is a deliberate swallowing of a startup-path
exception — probes are supposed to answer ``None`` themselves, and the
stub-and-adapter tests are where a raising probe gets caught.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping

from stella.desktop.backends import (
    gnome,
    hyprland,
    kde,
    sway,
    wayland_screencopy,
    x11_ewmh,
)
from stella.desktop.capabilities import Desktop
from stella.desktop.runner import Runner, subprocess_runner

Probe = Callable[..., Desktop | None]
"""``probe(env, runner, *, which) -> Desktop | None``, per backends/__init__.py."""

_CANDIDATES: tuple[tuple[str, Probe], ...] = (
    # The order *is* the priority; the module docstring says why.
    ("hyprland", hyprland.probe),
    ("sway", sway.probe),
    ("kde", kde.probe),
    ("gnome", gnome.probe),
    ("x11_ewmh", x11_ewmh.probe),
    ("wayland_screencopy", wayland_screencopy.probe),
)


def select_desktop(
    env: Mapping[str, str],
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Runner = subprocess_runner,
) -> Desktop | None:
    """The first desktop that reports itself usable, or ``None``.

    Ties are broken strictly left-to-right by ``_CANDIDATES`` (see the
    module docstring for why that order is the order).
    """

    for _name, probe in _CANDIDATES:
        try:
            desktop = probe(env, runner, which=which)
        except Exception:  # noqa: BLE001, S112 - cannot answer is not a desktop
            continue
        if desktop is not None:
            return desktop
    return None


__all__ = ["select_desktop"]
