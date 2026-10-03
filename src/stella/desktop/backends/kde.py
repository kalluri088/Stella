"""Unimplemented adapter: KDE Plasma (KWin) on Wayland, and its X11 sibling.

Planned surface — KWin's D-Bus scripting interface plus a Wayland-side
typing tool:

* reads/acts: ``org.kde.KWin``/``/KWin`` ``callScript`` (or the
  ``org.kde.kwin.Screenshots``/``KWinScripting`` interfaces), where
  ``workspace.activeWindow()`` and ``workspace.windows`` give the list and
  ``window.requestActivate()`` moves focus.
* capture: ``org.kde.kwin.Screenshot``/``spectacle -b -n -p`` — again a
  file-returning API whose path handling must be measured before the
  no-pixels rule can be honoured.
* text: ``kdotool`` (a wtype replacement that works on KWin's Wayland),
  with ``ydotool`` or ``xdotool`` as the X11 fallback.

Session marker: ``XDG_CURRENT_DESKTOP`` containing ``KDE`` **and** the
KWin D-Bus name actually answering, **and** the typing binary on PATH.
The env var alone is not a session: a Plasma-named terminal in another
desktop must not steal the probe, which is why the adapter is asked, not
assumed.

Facts a real machine must confirm before any of this is trusted:

* does ``callScript`` need KWin's "Allow different connections" security
  policy turned on (a user-visible setting the approval text should name,
  and which changes what "usable" means), and what does a refused
  connection look like on stdout versus stderr?
* KWin scripting is asynchronous: a script's return value comes back via
  a signal, not as the method reply — so what is the correct
  request/response pairing, and can a stale reply from an earlier call be
  mistaken for this one? (This is the KDE version of the rc-0-with-garbage
  trap and must be measured, not reasoned about.)
* ``window.requestActivate()`` can be declined by the user's focus policy
  ("focus follows mouse", raise-on-click, latency stealing): the re-query
  of ``workspace.activeWindow()`` is the only acceptable proof, and the
  id used to compare must be stable — is ``window.resourceName``/
  ``window.internalId``/``window.windowId`` the right opaque handle, and is
  it unique across activities?
* whether the geometry ``window.geometry`` reports is in global or
  screen-local coordinates under fractional scaling (a capture region in
  the wrong space reads the wrong pixels, which is a privacy failure, not
  a cosmetic one),
* the exact ``kdotool`` argv and its unicode/XKB behaviour, and whether it
  needs the ``input`` group or an active session it can reach.

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
