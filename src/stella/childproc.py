"""Kernel-level lifetime guarantees for Stella's subprocess fleet.

Report 34 found the one class its SIGKILL audit could not certify: a
long-lived helper (recorder, barge-in ear, local speech/transcription
commands, the judge interpreter, llama-server) outlives a SIGKILLed
Stella, because ``_cancel_process_tree`` only runs on ordered shutdown.
This module adds the guarantee that survives any death.

The guarantee is now chosen per platform, never assumed:

* **Linux** — ``PR_SET_PDEATHSIG``/``SIGKILL`` through ``preexec_fn``,
  armed between fork and exec by the kernel's own code path. This is the
  proven mechanism (``tests/test_childproc.py`` kills a real parent with
  SIGKILL and watches the child die) and it is left exactly as it was.
* **Windows** — no ``preexec_fn`` at all: ``subprocess`` raises
  ``ValueError`` if one is passed there. Instead the child is assigned to
  a Job Object created through ``ctypes`` (no pywin32) with
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. When Stella dies the kernel
  closes its handles, the job's last handle closes, and every process in
  the job is killed.
* **macOS** — no ``prctl`` and no ``PDEATHSIG`` equivalent. Nothing is
  armed at spawn time; a child simply reparents to launchd when Stella
  dies. The portable watchdog below is the mechanism for children that
  run Stella's own code.
* **Every platform** — :func:`start_parent_watchdog`, an in-process
  daemon thread that watches for the parent's death and takes its own
  process group down. This is what removes ``preexec_fn`` from being the
  *sole* guarantee: it also avoids the fork-safety hazard of running
  Python code with threads live between fork and exec.

The watchdog only runs inside a process that calls it. External
binaries (``pw-record``, ``llama-server``, a user's speech command)
cannot be given one, so on macOS they have no spawn-time guarantee; the
ordered shutdown ladder still handles the graceful case. That hole is
real and is stated here rather than papered over — it is the reason
macOS is not claimed as a supported platform.

It is explicitly a backstop, not the shutdown path — the ordered ladder
keeps doing the graceful work first. The boot sweep covers what even
PDEATHSIG cannot: children started by pre-1.3 builds (no mark) and
spawns where prctl silently failed. Marked-orphan processes are killed,
and the temporary directories they wrote are removed unless a live
child's command line still references them. The sweep reads ``/proc``
and so is Linux-only; off Linux it is a documented no-op.

Nothing in this module may fail at import time on any platform.
``ctypes.CDLL(None)`` raises ``ValueError`` on Windows (not ``OSError``)
and ``CDLL("kernel32")``/``WinDLL`` do not exist off Windows, so every
library handle is looked up lazily inside a guarded function.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath

from stella.portable import WINDOWS, platform_name

__all__ = [
    "CHILD_MARK_ENV",
    "TEMP_PREFIXES",
    "exit_status_finalized",
    "guarded_popen",
    "parent_death_guarantee",
    "recording_finalized_ok",
    "start_parent_watchdog",
    "sweep_orphaned_children",
    "terminate_own_process_group",
]

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

# How often the portable watchdog asks whether Stella is still there.
# Half a second is well below the interval at which a stale helper
# matters and well above anything that costs a real process CPU.
_WATCHDOG_INTERVAL_SECONDS = 0.5

# A bare RIFF/WAVE header: a recorder that wrote one captured nothing.
_MIN_WAV_BYTES = 44

# Roughly when this process started; anything newer on disk belongs to
# a session that is at least as current as ours and must not be touched.
_START_TS = time.time()

_PR_SET_PDEATHSIG = 1
_PRCTL_LOCK = threading.Lock()
_PRCTL_RESOLVED = False
_PRCTL: object | None = None

# Windows Job Object constants and the handle registry that keeps them
# alive for the process lifetime. Values are fixed ABI, not guesses.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_STATUS_CONTROL_C_EXIT = 0xC000013A
#: Windows reports a console Ctrl+C (what Stella sends to a recorder) as
#: this exit code, because it has no negative "killed by signal N".
WINDOWS_INTERRUPT_EXIT = _STATUS_CONTROL_C_EXIT

_OPEN_JOB_HANDLES: list[object] = []
_JOB_LOCK = threading.Lock()


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
        ("IoPriority", ctypes.c_int32),
        ("JobMemoryLimit", ctypes.c_size_t),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _prctl_function() -> object | None:
    """The libc ``prctl`` symbol, resolved once, or None if unavailable.

    ``CDLL(None)`` raises ``ValueError`` on Windows and ``OSError`` where
    dynamic loading is restricted; either way the answer is "no prctl",
    and the caller falls back to the watchdog. Never raise from here:
    importing this module is not allowed to fail on any platform.
    """

    global _PRCTL, _PRCTL_RESOLVED
    if _PRCTL_RESOLVED:
        return _PRCTL
    with _PRCTL_LOCK:
        if not _PRCTL_RESOLVED:
            try:
                _PRCTL = ctypes.CDLL(None, use_errno=True).prctl
            except (AttributeError, OSError, ValueError):
                _PRCTL = None
            _PRCTL_RESOLVED = True
    return _PRCTL


def parent_death_guarantee(sys_platform: str | None = None) -> str:
    """The stable token for the spawn-time guarantee this platform gets.

    Exposed so the decision itself is testable (and reportable) without
    pretending to be another operating system. ``+watchdog`` is always
    present because the watchdog is available everywhere; whether a
    given child actually runs one depends on whether it executes
    Stella's code — see the module docstring.
    """

    name = platform_name(sys_platform)
    if name == "linux":
        return "prctl+watchdog"
    if name == WINDOWS:
        return "job+watchdog"
    return "watchdog"


def _die_with_parent() -> None:
    """``preexec_fn``: SIGKILL this child when the parent process dies."""

    prctl = _prctl_function()
    if prctl is not None:
        # Return code is deliberately unchecked: a failed arm is covered
        # by the boot sweep, and raising between fork and exec would
        # turn a cosmetic loss into a spawn failure.
        prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)  # type: ignore[operator]


def _kernel32() -> object | None:
    try:
        return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return None


def _arm_windows_job(process: subprocess.Popen) -> None:
    """Put a freshly spawned child in a kill-on-close job.

    The process handle is ``Popen._handle``: on Windows CPython stores
    the ``hProcess`` returned by ``CreateProcessW`` there (see
    ``subprocess.Popen.internal_call``), and it is the right handle for
    ``AssignProcessToJobObject`` because CreateProcess grants it
    ``PROCESS_ALL_ACCESS``. There is no public attribute to use.

    Best-effort by design. A child that already exited between spawn and
    assignment is not a leak — it finished — so a failed call is dropped
    rather than turned into a spawn error, matching the Linux branch.
    """

    kernel32 = _kernel32()
    if kernel32 is None:
        return
    handle = getattr(process, "_handle", None)
    if not handle:
        return
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            kernel32.CloseHandle(job)
            return
        if not kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(handle)):
            kernel32.CloseHandle(job)
            return
        # Hold the reference: closing it is what kills the child, and the
        # kernel closes it for us if Stella is SIGKILLed.
        process._stella_job = job  # type: ignore[attr-defined]
        with _JOB_LOCK:
            _OPEN_JOB_HANDLES.append(job)
    except (AttributeError, OSError, ValueError):
        return


def _close_job_handles() -> None:
    """Close every job this process still holds (ordered shutdown).

    Registered with ``atexit`` so a clean Stella exit takes its helpers
    with it rather than leaving them for the next session. On a SIGKILL
    this never runs and the kernel does the same thing implicitly.
    """

    kernel32 = _kernel32()
    if kernel32 is None:
        return
    with _JOB_LOCK:
        handles, _OPEN_JOB_HANDLES[:] = list(_OPEN_JOB_HANDLES), []
    for job in handles:
        try:
            kernel32.CloseHandle(job)
        except (AttributeError, OSError, ValueError):  # pragma: no cover
            pass


atexit.register(_close_job_handles)


def _parent_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, just not ours to signal
        return True
    return True


def terminate_own_process_group() -> None:
    """Take this process and everything started alongside it down.

    POSIX: ``SIGKILL`` to our own process group, which is the widest
    thing a process is entitled to signal about itself. ``os._exit``
    follows so a thread blocked in the kill path cannot keep the process
    up.

    Windows: no process-group concept; terminate this process directly.
    Grandchildren are the Job Object's job, not this function's.
    """

    if platform_name() == WINDOWS:
        kernel32 = _kernel32()
        if kernel32 is not None:
            try:
                kernel32.TerminateProcess(kernel32.GetCurrentProcess(), 1)
            except (AttributeError, OSError, ValueError):  # pragma: no cover
                pass
        os._exit(1)
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except OSError:
        pass
    os._exit(1)


def start_parent_watchdog(
    parent_pid: int | None = None,
    *,
    interval_seconds: float = _WATCHDOG_INTERVAL_SECONDS,
    alive_fn: Callable[[int], bool] = _parent_alive,
    getppid_fn: Callable[[], int] = os.getppid,
    terminate_fn: Callable[[], None] = terminate_own_process_group,
    on_death: Callable[[], None] | None = None,
) -> threading.Thread:
    """Watch for Stella's death from inside a Stella-owned child.

    The portable half of the guarantee, and the only one that works on
    every platform without a kernel feature. It checks two things,
    because they fail differently:

    * ``getppid()`` changed — the classic reparenting signal. On macOS
      and Windows a child is re-parented when its parent dies, so the
      original ppid is not the interesting value; the *change* is.
    * the tracked ``parent_pid`` is gone — covers a parent that died and
      was replaced by an unrelated process reusing the pid, and the
      Linux case where the ppid stays constant until init notices.

    ``parent_pid`` defaults to the current ppid, which is what Stella is
    for a direct child. ``alive_fn``/``getppid_fn``/``terminate_fn`` are
    the seams the tests use to prove the decision logic without
    orchestrating real process death, and ``on_death`` lets a caller run
    its own cleanup before the kill.
    """

    tracked = os.getppid() if parent_pid is None else parent_pid
    started_ppid = getppid_fn()

    def loop() -> None:
        while True:
            current_ppid = getppid_fn()
            if current_ppid != started_ppid or not alive_fn(tracked):
                if on_death is not None:
                    on_death()
                terminate_fn()
                return
            time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, name="stella-parent-watchdog", daemon=True)
    thread.start()
    return thread


def guarded_popen(argv: Sequence[str], **kwargs) -> subprocess.Popen:
    """``subprocess.Popen`` with the parent-death guarantee and a mark.

    The mark env is merged into whatever environment the caller asked
    for, so callers keep owning their own env handling.
    """

    env = dict(kwargs.pop("env", None) or os.environ)
    env[CHILD_MARK_ENV] = str(os.getpid())
    kwargs["env"] = env
    guarantee = parent_death_guarantee()
    if guarantee.startswith("prctl"):
        # Linux: unchanged. ``preexec_fn`` is refused outright on
        # Windows, and is pointless on macOS where there is no prctl.
        kwargs["preexec_fn"] = _die_with_parent
    else:
        kwargs.pop("preexec_fn", None)
    process = subprocess.Popen(list(argv), **kwargs)
    if guarantee.startswith("job"):
        _arm_windows_job(process)
    return process


def exit_status_finalized(
    returncode: int | None,
    *,
    sys_platform: str | None = None,
    interrupt_signal: int | None = None,
) -> bool:
    """Whether a stopped helper's exit status can mean "it finished".

    POSIX conventions (Linux unchanged, exactly as voice.py has always
    read them): 0 for a clean exit, ``-SIGINT`` when the kernel reports
    death by the interrupt Stella itself sent, and 2 because several
    shell-level recorders translate Ctrl-C into it.

    Windows has no negative signal codes. ``pw-record``-equivalents
    report a console interrupt as ``0xC000013A``, and anything else —
    including a bare ``2`` that on POSIX meant SIGINT — is not evidence
    of a clean finalization. Combined with
    :func:`recording_finalized_ok`'s file check, an unrecognised status
    fails closed.

    ``interrupt_signal`` lets a caller state which signal it sent; it
    defaults to ``SIGINT``, which is what Stella sends today.
    """

    if returncode is None:
        return False
    if platform_name(sys_platform) == WINDOWS:
        return returncode == 0 or returncode == WINDOWS_INTERRUPT_EXIT
    sigint = signal.SIGINT if interrupt_signal is None else interrupt_signal
    return returncode in (0, -int(sigint), 2)


def recording_finalized_ok(
    returncode: int | None,
    path: str | os.PathLike[str],
    *,
    sys_platform: str | None = None,
    min_bytes: int = _MIN_WAV_BYTES,
) -> bool:
    """Did the recorder actually leave a usable capture behind?

    One place for the conclusion, so no platform's exit-status convention
    can be read with the other platform's rules. Status alone is never
    enough: the file has to exist and be larger than a bare RIFF/WAVE
    header. A vanished or empty file fails closed even when the status
    looked fine.
    """

    if not exit_status_finalized(returncode, sys_platform=sys_platform):
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    return size > min_bytes


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


def _mentions_stella_temp(token: str) -> bool:
    """Whether a command-line word names one of Stella's temp paths.

    Separator-independent on purpose: matching ``f"/{prefix}"`` assumed a
    POSIX path layout. Each ``/``- and ``\\``-delimited component is
    compared by prefix instead, so ``/tmp/stella-voice-9-x/a.wav``,
    ``C:\\Users\\me\\AppData\\Local\\Temp\\stella-voice-9-x\\a.wav`` and an
    ``--out=/tmp/stella-voice-9-x/a.wav`` glued argument all match, while
    a word that merely contains the prefix mid-string does not.
    """

    for pure in (PurePosixPath(token), PureWindowsPath(token)):
        if any(part.startswith(TEMP_PREFIXES) for part in pure.parts):
            return True
    return False


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
        if any(_mentions_stella_temp(token) for token in tokens):
            watchers.append((pid, tokens))
    return watchers


def _sweep_linux(output_fn: Callable[[str], None]) -> int:
    killed = 0
    protected: list[str] = []
    for pid, stella_pid in _marked_children():
        if _parent_alive(stella_pid):
            # A live Stella owns this child — ours or a sibling
            # instance's. Protect every temp path its argv names.
            for token in _command_line_args(pid):
                if _mentions_stella_temp(token):
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


def sweep_orphaned_children(output_fn: Callable[[str], None] = print) -> int:
    """Kill marked children whose Stella died; reclaim their temp files.

    Linux only, because the whole mechanism is reading other processes'
    ``/proc/<pid>/environ`` and ``cmdline``. Off Linux this is an
    explicit no-op returning 0 rather than a best-effort scan whose
    ``OSError`` handling would silently turn "could not look" into
    "nothing was orphaned". macOS therefore has neither a spawn-time
    kernel guarantee nor a boot sweep for foreign helpers; that is the
    documented gap in the module docstring, not an oversight here.
    """

    if platform_name() != "linux":
        return 0
    return _sweep_linux(output_fn)
