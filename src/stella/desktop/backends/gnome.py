"""Unimplemented adapter: GNOME Shell on Wayland (and X11) over D-Bus.

Planned surface — the GNOME Shell private D-Bus API, reached with
``gdbus call`` (or ``busctl --user call``, whichever the machine answers
through):

* capture: ``org.gnome.Shell.Screenshot`` — ``Screenshot`` /
  ``ScreenshotWindow`` / ``FlashArea``. Note the shape of this API: it
  writes a *file* and returns its path. The no-pixels invariant means the
  adapter must read the file and remove it, and must state the measured
  race/permission behaviour of that path here before it is trusted.
* reads: ``org.gnome.Shell.Eval`` on ``global.get_window_actors()`` /
  ``display.focus_window()``, or the ``org.gnome.Shell.Windows``/
  ``Org GNOME Shell``-style interface where it exists.
* focus: ``Meta.Window.activate()`` through the same scripting route.
* text: the hard one. Wayland GNOME exposes no keyboard-injection API, so
  ``xdotool type`` works only on an X11 session or against XWayland
  windows; a Wayland-native window may simply be untypeable from here.

Session marker: the well-known name is claimable only inside a GNOME
session, so ``busctl --user call org.gnome.Shell /org/gnome/Shell
org.gnome.Shell Eval 's' 'mainloop.is_running()'`` succeeding **is** the
probe. Do not trust ``XDG_CURRENT_DESKTOP=GNOME`` alone — that is set in
GNOME-on-X11, in a nested session, and in a terminal that inherited it.

Facts a real machine must confirm before any of this is trusted:

* is ``Eval`` enabled at all on this GNOME version (distros and later
  releases gate or remove it; ``Looking Glass`` being the only route would
  change the design), and if not, which supported API gives the window
  list and the focus?
* does ``Screenshot``'s returned path land in the user's Pictures (a
  privacy leak the approval text must state) or in a temp dir, and can it
  be pointed at a pipe instead?
* what exactly does ``Eval`` return around JSON — a ``(true, '<json>')``
  tuple whose string form has to be un-wrapped before parsing — and how
  does it answer when the JavaScript throws (an unusable answer, never an
  empty window list)?
* whether ``Meta.Window.activate()`` takes effect synchronously or needs
  Mutter's own frame clock before ``focus_window()`` reads it back, and
* the GNOME-screenshot-to-PNG byte format/extension guarantee, because
  the recognizer's contract is PNG bytes.

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
