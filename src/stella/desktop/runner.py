"""The subprocess plumbing every adapter shares — and every test replaces.

Adapters never call ``subprocess`` directly; they take a ``Runner``. That
is the one injection point the desktop tests are built around: a fake can
answer exactly like the machine did (garbage with rc 0, errors on stdout)
without anything needing a desktop, a compositor or a display.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Completed:
    """The three facts of a subprocess run that any decision may use."""

    returncode: int
    stdout: bytes
    stderr: bytes


Runner = Callable[..., Completed]
"""``callable(argv: Sequence[str], *, timeout: float, stdin: bytes | None,
env: Mapping[str, str] | None) -> Completed``."""


def subprocess_runner(
    argv: Sequence[str],
    *,
    timeout: float,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> Completed:
    process = subprocess.run(
        list(argv),
        input=stdin,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=dict(env) if env is not None else None,
    )
    return Completed(process.returncode, process.stdout, process.stderr)


def error_detail(
    result: Completed, *, label: str = "The tool said", limit: int = 200
) -> str:
    """The command's own words, clipped and safe to show the model.

    Stdout first: measured on Hyprland (report 03 rule 2), dispatch errors
    arrive as rc 7 **on stdout** with an empty stderr. An adapter whose
    tool reports errors elsewhere still gets *an* honest sentence here —
    it just has to decide, on a real machine, which stream to trust.
    """

    detail = (result.stdout or result.stderr).decode("utf-8", "replace").strip()
    return f"{label}: {detail[:limit]}" if detail else ""


__all__ = ["Completed", "Runner", "error_detail", "subprocess_runner"]
