"""The narrow cross-platform layer behind Stella's process and path plumbing.

Stella is a Linux desktop assistant. This module exists so that the
*plumbing* — how a command string becomes an argv, where a file lives,
how a private file is made private — is correct by construction on
Linux, macOS and Windows instead of accidentally POSIX-only. It does
**not** make Stella's desktop capabilities portable: there are no
screen, keyboard or window adapters here, and only Linux is verified by
this repository's tests. The macOS and Windows branches are deliberately
small, dependency-free and honest about being unverified.

Design rules:

* Every function here *decides* something; none of them hides a failure
  behind a plausible-looking success. Where a platform has no mechanism,
  the answer is reported as "not applied", never as "applied".
* Nothing may fail at import time. ``ctypes.WinDLL`` does not exist off
  Windows and ``ctypes.CDLL(None)`` raises ``ValueError`` there, so both
  are looked up lazily inside guarded functions, never at module scope.
* Platform choice is always a parameter (``sys_platform``) that defaults
  to ``sys.platform``. That keeps the decision unit-testable with a fake
  platform string instead of being skipped on the machine that cannot
  run it.
"""

from __future__ import annotations

import ctypes
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "HARDEN_NO_MECHANISM",
    "LINUX",
    "MACOS",
    "OTHER",
    "WINDOWS",
    "Hardening",
    "default_editor",
    "harden_private_file",
    "has_unix_sockets",
    "platform_name",
    "polite_stop",
    "split_command",
]

LINUX = "linux"
MACOS = "macos"
WINDOWS = "windows"
#: Anything POSIX that is not Linux or macOS (BSD and friends): the POSIX
#: branches apply, the Linux-only kernel features do not.
OTHER = "other"

#: ``Hardening.mechanism`` when the platform has no private-file
#: mechanism Stella can apply. Deliberately not ``"applied"``.
HARDEN_NO_MECHANISM = "none"


def platform_name(sys_platform: str | None = None) -> str:
    """The canonical short platform token for ``sys.platform``.

    ``sys_platform`` lets a caller (or a test) decide for a platform it
    is not running on. An unrecognised value is ``OTHER``, which follows
    the POSIX branches — the safe direction, since POSIX code paths are
    the ones this project actually exercises.
    """

    name = sys.platform if sys_platform is None else sys_platform
    if name.startswith("linux"):
        return LINUX
    if name == "darwin":
        return MACOS
    if name.startswith("win"):
        return WINDOWS
    return OTHER


def _unquote(token: str) -> str:
    """Drop one layer of surrounding quotes from a ``posix=False`` token."""

    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def split_command(
    text: str,
    *,
    sys_platform: str | None = None,
    split_fn: Callable[[str], list[str]] = shlex.split,
) -> list[str]:
    """Turn a user-configured command line into an argv list.

    POSIX (unchanged, and still what every Linux user gets): ``shlex`` in
    posix mode, which processes backslash escapes.

    Windows: posix mode would eat the backslashes of ``C:\\Users\\...``,
    so ``shlex`` runs with ``posix=False`` and the surrounding quotes it
    leaves on each token are stripped. Escape processing is intentionally
    not emulated — on Windows a backslash in a quoted argument means a
    path, not an escape.

    ``split_fn`` is the seam tests use to prove the branch choice without
    pretending to be Windows.
    """

    if platform_name(sys_platform) == WINDOWS:
        return [_unquote(token) for token in split_fn(text, posix=False)]
    return split_fn(text)


def default_editor(
    env: Mapping[str, str] | None = None,
    *,
    sys_platform: str | None = None,
) -> str:
    """``$VISUAL``, then ``$EDITOR``, then the platform's plain default."""

    lookup = os.environ if env is None else env
    for variable in ("VISUAL", "EDITOR"):
        value = (lookup.get(variable) or "").strip()
        if value:
            return value
    return "notepad" if platform_name(sys_platform) == WINDOWS else "vi"


def polite_stop(
    process: subprocess.Popen, *, sys_platform: str | None = None
) -> None:
    """Ask one child to shut down on its own terms, as far as this platform lets.

    POSIX: ``SIGINT``, which is the clean-shutdown path both a recording
    command and ``llama.cpp`` read. Windows cannot deliver that signal to
    another process at all — ``send_signal`` raises ``ValueError`` there rather
    than failing quietly — so the closest thing is ``terminate()``, which
    kills the child without letting it finish its work. A caller that needs to
    know whether the child got its work done has to check the result, not the
    exit status: that is what ``recording_finalized_ok`` is for.
    """

    if platform_name(sys_platform) == WINDOWS:
        process.terminate()
        return
    process.send_signal(signal.SIGINT)


def has_unix_sockets(*, sys_platform: str | None = None) -> bool:
    """Does this platform have the ``AF_UNIX`` socket Stella rings to talk to itself?

    Windows is the answer no: Python exposes no ``AF_UNIX`` there, and the
    per-user id Stella names the doorbell after (``os.getuid``) does not
    exist either. The question is asked by ``stella doctor``, by
    ``stella voice`` and by anything that probes for a running server on
    every platform, so the answer has to be a fact rather than an
    ``AttributeError`` on the machine that lacks the mechanism.
    """

    return platform_name(sys_platform) != WINDOWS


@dataclass(frozen=True)
class Hardening:
    """What Stella actually managed to do to keep a file private.

    ``applied`` is the only field callers may treat as success. It is
    False whenever nothing was done, so no caller can accidentally imply
    that a file is private on a platform where Stella did nothing.
    """

    applied: bool
    mechanism: str
    detail: str = ""

    def note(self) -> str:
        """A short honest addendum for a user-facing message, or empty."""

        if self.applied:
            return ""
        return f"could not restrict permissions ({self.detail or self.mechanism})"


def _current_user_sid(advapi: object | None = None) -> str | None:
    """``*S-1-5-...`` for the running user, or None. Windows only.

    ``icacls`` accepts a SID trustee prefixed with ``*``, which avoids
    both a domain-dependent account name and a pywin32 dependency. The
    whole path is best-effort: any failure is None, which the caller
    reports as "not hardened".
    """

    if advapi is None:
        try:
            advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError):
            return None
    try:
        token_query = 0x0008  # TOKEN_QUERY
        token_user = 1  # TokenUser

        class _SidAndAttributes(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]

        class _TokenUser(ctypes.Structure):
            _fields_ = [("User", _SidAndAttributes)]

        handle = ctypes.c_void_p()
        size = ctypes.c_uint32()
        advapi.OpenProcessToken.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi.GetTokenInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        advapi.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        if not advapi.OpenProcessToken(ctypes.c_void_p(-1), token_query, ctypes.byref(handle)):
            return None
        if (
            not advapi.GetTokenInformation(handle, token_user, None, 0, ctypes.byref(size))
            or not size.value
        ):
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(handle, token_user, buffer, size, ctypes.byref(size)):
            return None
        sid = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.User.Sid
        string_sid = ctypes.c_wchar_p()
        if not sid or not advapi.ConvertSidToStringSidW(sid, ctypes.byref(string_sid)):
            return None
        return f"*{string_sid.value}" if string_sid.value else None
    except (AttributeError, OSError, ValueError):
        return None


def _harden_windows(
    path: Path,
    *,
    sid_lookup: Callable[[], str | None] = _current_user_sid,
    run: Callable[..., object] = subprocess.run,
) -> Hardening:
    """Owner-only DACL through ``icacls``, reported honestly.

    ``0o600`` on NTFS is only the read-only bit, so a chmod-style call
    would *imply* privacy it does not provide. This grants exactly one
    trustee full control after removing inherited entries, in argv form
    (never through a shell), and returns whether the grant was reported
    as successful. It is unverified here: no macOS/Windows host is
    available to this repository.
    """

    sid = sid_lookup()
    if not sid:
        return Hardening(False, HARDEN_NO_MECHANISM, "no SID for the current user")
    argv = ["icacls", os.fspath(path), "/inheritance:r", "/grant:r", f"{sid}:F"]
    try:
        completed = run(argv, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return Hardening(False, HARDEN_NO_MECHANISM, f"icacls unavailable ({error.__class__.__name__})")
    if getattr(completed, "returncode", 1) == 0:
        return Hardening(True, "windows-acl")
    raw = b"".join(
        part if isinstance(part, bytes) else str(part).encode("utf-8", "replace")
        for part in (getattr(completed, "stdout", b""), getattr(completed, "stderr", b""))
    )
    detail = " ".join(raw.decode("utf-8", errors="replace").split())[:160]
    return Hardening(False, "windows-acl", detail or f"icacls exited {getattr(completed, 'returncode', 1)}")


def harden_private_file(path: str | Path, *, sys_platform: str | None = None) -> Hardening:
    """Make one file readable only by its owner, and say whether it worked.

    POSIX: ``chmod 0600``, exactly as Stella has always done. A failure
    there propagates as ``OSError`` rather than a soft report — this is
    the proven path and silently continuing would weaken it.

    Windows: an inherited-ACL reset plus an owner grant, best-effort and
    reported. Never claims privacy it cannot confirm.
    """

    target = Path(path)
    if platform_name(sys_platform) == WINDOWS:
        return _harden_windows(target)
    os.chmod(target, 0o600)
    return Hardening(True, "posix-mode-600")
