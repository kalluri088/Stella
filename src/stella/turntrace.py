"""Opt-in per-turn timing marks for latency measurement.

A voice answer's whole value is that the first sentence reaches a
speaker before the model has written the last. That claim is a set of
numbers, and nothing in the running application is supposed to know
about numbers: :func:`mark` exists so a benchmark can measure them.

When the environment names a file, ``mark`` appends ``stage,`` and
:meth:`time.monotonic` scaled to integer milliseconds; nothing else.
Never model text, never a transcript, never a secret. Unset is the
default, and unset costs one dict lookup and one ``None`` check.

The path is read once per process (or after :func:`reset`) and cached,
so a turn that runs without the diagnostic never touches
:mod:`os.environ`. A test that wants to change the switch between
turns calls :func:`reset` first; the live application never needs to,
because an owner sets the environment before launch.
"""

from __future__ import annotations

import os
import threading
import time

#: The one knob: a path, unset by default, never in ``config.json``.
ENV_VAR = "STELLA_TURN_TRACE"

_lock = threading.Lock()
_state: tuple[bool, str | None] = (False, None)


def _resolve() -> str | None:
    global _state
    if not _state[0]:
        with _lock:
            if not _state[0]:
                value = os.environ.get(ENV_VAR, "").strip()
                _state = (True, value or None)
    return _state[1]


def reset() -> None:
    """Forget the cached path so a test can re-set :data:`ENV_VAR`."""

    global _state
    with _lock:
        _state = (False, None)


def enabled() -> bool:
    """Whether a mark will actually be written right now."""

    return _resolve() is not None


def mark(stage: str) -> None:
    """Append ``stage,<monotonic-ms>`` when the diagnostic is on.

    Never raises: an observer that fails is not worth interrupting the
    turn it watches. The line is written with an append-mode open per
    call, so a crash between marks still leaves the earlier lines on
    disk for the reader to see.
    """

    path = _resolve()
    if path is None:
        return
    line = f"{stage},{int(time.monotonic() * 1000)}\n"
    try:
        with _lock, open(path, "a", encoding="utf-8") as sink:
            sink.write(line)
    except OSError:
        # A diagnostic that cannot reach disk stays silent; the next
        # call will try again, and so will every later one.
        pass
