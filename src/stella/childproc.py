"""Kernel-level lifetime guarantees for Stella's subprocess fleet.

Report 34 found the one class its SIGKILL audit could not certify: a
long-lived helper (recorder, barge-in ear, local speech/transcription
commands, the judge interpreter, llama-server) outlives a SIGKILLed
Stella, because ``_cancel_process_tree`` only runs on ordered shutdown.
This module adds the guarantee that survives any death: children are
armed with ``PR_SET_PDEATHSIG``/``SIGKILL``, which this kernel delivers
when the spawning *process* exits (measured: thread exit does not fire
it, so children still outlive their spawning thread, as today).

It is explicitly a backstop, not the shutdown path — the ordered
ladder keeps doing the graceful work first. The boot sweep covers what
even PDEATHSIG cannot: children started by pre-1.3 builds (no mark)
and spawns where prctl silently failed. Marked-orphan processes are
killed, and the temporary directories they wrote are removed unless a
live child's command line still references them.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

#: Environment marker every guarded child carries: the pid of the
#: Stella process that spawned it. If that pid is gone, the child is
#: an orphan by definition.
CHILD_MARK_ENV = "STELLA_CHILD_PID"

#: Temporary-file prefixes this module's sweep is allowed to reclaim.
#: New names embed the creating pid (``stella-voice-<pid>-<random>``)
#: so a sibling instance's still-owned files are recognizable — and
#: untouched — without asking its processes.
TEMP_PREFIXES = ("stella-voice-", "stella-speech-", "stella-brain-")
_OWNER_RE = re.compile(r"^stella-(?:voice|speech|brain)-(\d+)-")

# How long a SIGKILLed helper gets to be confirmed dead before the sweep
# moves on without claiming it.
_DEATH_GRACE_SECONDS = 1.5

# Roughly when this process started; anything newer on disk belongs to
# a session that is at least as current as ours and must not be touched.
_START_TS = time.time()

try:  # pragma: no cover - every Linux has this; the fallback is honest
    _PRCTL = ctypes.CDLL(None, use_errno=True).prctl
except OSError:
    _PRCTL = None

_PR_SET_PDEATHSIG = 1


def _die_with_parent() -> None:
    """``preexec_fn``: SIGKILL this child when the parent process dies."""

    if _PRCTL is not None:
        # Return code is deliberately unchecked: a failed arm is covered
        # by the boot sweep, and raising between fork and exec would
        # turn a cosmetic loss into a spawn failure.
        _PRCTL(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


def guarded_popen(argv: Sequence[str], **kwargs) -> subprocess.Popen:
    """``subprocess.Popen`` with the parent-death guarantee and a mark.

    The mark env is merged into whatever environment the caller asked
    for, so callers keep owning their own env handling.
    """

    env = dict(kwargs.pop("env", None) or os.environ)
    env[CHILD_MARK_ENV] = str(os.getpid())
    kwargs["env"] = env
    kwargs["preexec_fn"] = _die_with_parent
    return subprocess.Popen(list(argv), **kwargs)


def _kill(pid: int) -> bool:
    """SIGKILL a possibly-foreign pid and make sure it is really gone.

    An orphan that is our own child turns into a zombie on SIGKILL, and
    ``os.kill(pid, 0)`` succeeds on a zombie: the check has to be a real
    wait, which only the parent can do.
    """

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return False
    deadline = time.time() + _DEATH_GRACE_SECONDS
    while time.time() < deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True  # not ours (or already reaped): it is gone
        except OSError:
            return False
        if waited == pid:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def _parent_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, just not ours to signal
        return True
    return True


def _marked_children() -> list[tuple[int, int]]:
    """(child pid, recorded Stella pid) for every marked child visible in /proc."""

    found: list[tuple[int, int]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:  # pragma: no cover - /proc is always there on Linux
        return found
    needle = f"{CHILD_MARK_ENV}=".encode()
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/environ", "rb") as handle:
                environ = handle.read()
        except OSError:
            continue  # another user's, or already gone
        for chunk in environ.split(b"\0"):
            if chunk.startswith(needle):
                try:
                    found.append((pid, int(chunk[len(needle):])))
                except ValueError:
                    pass
                break
    return found


def _command_line_args(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read().split(b"\0")
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw]


def _referencing_processes() -> list[tuple[int, list[str]]]:
    """(pid, argv) for live processes whose command line names a Stella temp path."""

    watchers: list[tuple[int, list[str]]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:  # pragma: no cover
        return watchers
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        tokens = _command_line_args(pid)
        if any(
            f"/{prefix}" in token
            for token in tokens
            for prefix in TEMP_PREFIXES
        ):
            watchers.append((pid, tokens))
    return watchers


def sweep_orphaned_children(output_fn: Callable[[str], None] = print) -> int:
    """Kill marked children whose Stella died; reclaim their temp files."""

    killed = 0
    protected: list[str] = []
    for pid, stella_pid in _marked_children():
        if _parent_alive(stella_pid):
            # A live Stella owns this child — ours or a sibling
            # instance's. Protect every temp path its argv names.
            for token in _command_line_args(pid):
                if any(f"/{prefix}" in token for prefix in TEMP_PREFIXES):
                    protected.append(token)
            continue
        if _kill(pid):
            killed += 1
    # Processes predating this marking have no env entry; anyone still
    # writing to a directory that turns out to be orphaned below is
    # killed with it.
    watchers = _referencing_processes()
    temp_root = Path(tempfile.gettempdir())
    reclaimed = 0
    candidates = [
        path
        for prefix in TEMP_PREFIXES
        for path in temp_root.glob(f"{prefix}*")
    ]
    for path in candidates:
        owner = _OWNER_RE.match(path.name)
        if owner and _parent_alive(int(owner.group(1))):
            continue  # a live process (us or a sibling instance) owns it
        try:
            if path.stat().st_mtime >= _START_TS:
                continue  # belongs to this or a newer session
            if any(str(path) in token for token in protected):
                continue  # a live child still references it
        except OSError:
            continue
        for pid, tokens in watchers:
            if any(str(path) in token for token in tokens) and _kill(pid):
                killed += 1
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
        reclaimed += 1
    if killed or reclaimed:
        output_fn(
            f"Cleaned up {killed} orphaned Stella helper process(es) and "
            f"{reclaimed} stale temporary file(s) from an earlier crash."
        )
    return killed
