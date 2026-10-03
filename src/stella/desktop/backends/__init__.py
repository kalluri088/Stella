"""Desktop adapters: one module per desktop, one agent per module.

The contract every adapter here implements lives in
:mod:`stella.desktop.capabilities`; this is the part that is about the
*adapter*, so a new one can be written without reading any other file.

An adapter module exposes exactly one public entry point:

``probe(env, runner, *, which=shutil.which) -> Desktop | None``

* ``env`` is the process environment mapping (never assume; read it).
* ``runner`` is the injectable subprocess seam from
  :mod:`stella.desktop.runner`. Nothing in an adapter may import
  ``subprocess`` itself — that is what makes the whole surface testable
  without a display server, and the Hyprland tests are the pattern.
* ``which`` is the injectable PATH lookup.
* Returns a complete :class:`Desktop`, or ``None``. ``None`` is the
  answer for "not this desktop", "session marker missing", "a binary I
  need is missing", and "not implemented yet". Registration treats all
  four the same way: no tool reaches the model (invariant 5).

What an adapter owes the invariants:

1. Reads return ``None`` for an unusable answer and the real (possibly
   empty) list otherwise. Exit codes prove nothing; check the shape.
2. ``activate`` re-queries the compositor and returns True only when this
   window is now the active one. Never report the command's own success.
3. ``send_text`` may only type into the focused surface, and the caller
   has verified that focus; anything else is out of contract.
4. ``capture`` returns PNG bytes in memory and raises
   ``CaptureUnavailable`` on any failure, including a tool that wants to
   write a file: if the only working capture path on a desktop saves a
   screenshot to disk, the adapter must read it and delete it, and say so
   in this module's docstring with the measured evidence.
5. A window id is opaque and validated *inside* the adapter before it
   reaches a command line or a scripting API. The tools will hand
   anything matching their neutral shape straight back to this module.
6. ``session_note`` names the exact session being touched, because the
   approval dialog has to say which one.

Stub files below are deliberately empty of behaviour: they exist so each
future adapter has a file of its own and no agent has to edit a shared
one. Do not implement one unless the task says to.
"""
