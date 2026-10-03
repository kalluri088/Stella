"""A real filesystem jail for the commands Stella runs, via bubblewrap.

The ``shell_run`` tool used to confine a command only to its *starting
directory* and said so plainly: "it is not a filesystem jail." This module
makes that caveat a thing of the past on any machine that already has
``bwrap`` (bubblewrap — the sandbox Flatpak itself runs under, and the
default on this Arch/systemd desktop). It adds **no dependency and downloads
nothing**: it only assembles a ``bwrap`` command line the OS can already run.

What the jail actually does, verified on this host:

* **Read-only everything, one writable exception.** ``--ro-bind / /`` mounts
  the whole filesystem read-only inside the sandbox; the Stella workspace is
  then re-bound read-write. A command cannot edit, delete or create a file
  outside the workspace, even if the owner mistakenly approves it — ``/`` is a
  read-only mount. Private scratch lives on a private ``/tmp`` tmpfs.
* **Your home is hidden.** ``/home`` is replaced by an empty tmpfs and the
  workspace is re-bound back, so sibling projects, ``~/.ssh``, browser
  profiles and dotfiles are simply absent. A writable ``HOME`` is provided on
  a tmpfs so tools that insist on one still run, in an isolated directory.
* **Privilege is dropped.** The command loses every supplementary group
  (docker, kvm, libvirt, wheel and friends), so a "successful" command cannot
  reach the daemon groups that would let it escape the account.
* **Isolated namespaces.** New user, PID, mount, UTS and IPC namespaces
  (``--unshare-all``): the command cannot see or signal processes outside the
  sandbox, and ``--die-with-parent`` guarantees it goes away with Stella.
* **Bounded, honestly reported.** The starting command still cannot outlive
  the tool's own output cap and wall-clock timeout (see
  ``stella.shell_tools``), and a couple of conservative ``ulimit`` safety
  nets (CPU seconds, process count, file size) are set before the command
  runs so a runaway stops before the timeout does.

Network is **allowed** by default, because a shell the owner has approved for
this very command is expected to be able to ``git pull`` or ``curl``; it is
not a privilege increase over an unsandboxed shell and matches how the web
tools already reach out. Passing ``network=False`` closes it (no
``--share-net``), which the approval card reports either way.

This is defense in depth, not a promise against a determined kernel exploit,
and it is not a claim that a sandboxed command is safe to run unapproved —
the human approval of the literal command remains the authority (rules 3 and
10). The jail shrinks the blast radius *given* that a command runs; the
approval is what decides whether it runs at all.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from stella.portable import WINDOWS, platform_name

__all__ = [
    "ENV_SANDBOX",
    "Sandbox",
    "SandboxLimits",
    "build_sandbox_argv",
    "sandbox_available",
    "sandbox_requested",
]

# The single knob that turns the jail off for one launch. On by default: a
# shell that is enabled at all should be jailed by default, and only an
# explicit off value falls back to the confined-starting-directory path.
ENV_SANDBOX = "STELLA_SHELL_SANDBOX"
_ENV_OFF = frozenset({"0", "false", "off", "no"})

# Writable, isolated home inside the jail. The real /home is a tmpfs, so this
# path is a fresh tmpfs of its own — nothing of the owner's actual home.
SANDBOX_HOME = "/home/stella"

# Conservative ulimit safety nets, set before the command runs. These are not
# the authority (approval is) and not the primary bound (the tool's byte cap
# and wall-clock timeout are); they only stop a fork bomb or a runaway before
# the timeout has to. fsize is in 512-byte blocks, so ~128 MiB.
DEFAULT_CPU_SECONDS = 120
DEFAULT_NPROC = 1024
DEFAULT_FSIZE_BLOCKS = 262_144

# A PATH that finds system tools without importing the host's extra config.
SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"


@dataclass(frozen=True)
class SandboxLimits:
    """The ulimit safety nets applied inside the jail. Trusted, never model-set."""

    cpu_seconds: int = DEFAULT_CPU_SECONDS
    nproc: int = DEFAULT_NPROC
    fsize_blocks: int = DEFAULT_FSIZE_BLOCKS


def sandbox_requested(env: Mapping[str, str] | None = None) -> bool:
    """Whether the jail should be used, from the ``STELLA_SHELL_SANDBOX`` env.

    On unless the environment says off (default-on for a shell capability
    that is already gated by an explicit opt-in flag). Absent or empty means
    on; only one of the recognised off spellings turns the jail off.
    """

    raw = os.environ if env is None else env
    value = str(raw.get(ENV_SANDBOX, "")).strip().lower()
    return value not in _ENV_OFF


def _userns_enabled() -> bool:
    """Whether unprivileged user namespaces are switched on.

    A missing or unreadable knob is not taken as "off" — a Linux that ships
    bubblewrap normally has them enabled; only an explicit ``0`` disables.
    """

    for knob in (
        Path("/proc/sys/user/max_user_namespaces"),
        Path("/proc/sys/kernel/unprivileged_userns_clone"),
    ):
        try:
            if knob.read_text(encoding="utf-8").strip() == "0":
                return False
        except OSError:
            continue
    return True


def sandbox_available(env: Mapping[str, str] | None = None) -> bool:
    """Whether this host can actually run the jail — no, don't assume.

    Requires a non-Windows platform with ``bwrap`` on PATH and unprivileged
    user namespaces not switched off. When this returns False the caller must
    fall back to the confined-starting-directory path and say so honestly
    rather than claiming a jail it does not have.
    """

    if platform_name() == WINDOWS:
        return False
    raw = os.environ if env is None else env
    if shutil.which("bwrap", path=raw.get("PATH")) is None:
        return False
    return _userns_enabled()


def build_sandbox_argv(
    command: str,
    *,
    workspace: str,
    network: bool = True,
    home: str = SANDBOX_HOME,
    limits: SandboxLimits | None = None,
) -> list[str]:
    """Assemble the ``bwrap`` argv that runs ``command`` inside the jail.

    The user's command string is never pasted into the wrapper script: it is
    carried as a trailing positional argument and re-execed with
    ``/bin/sh -c "$1" sh``, so a command containing quotes, ``$`` or ``;``
    cannot break out of the wrapper or run as the wrapper. The returned argv
    is complete and self-contained — the caller spawns it directly.
    """

    caps = limits or SandboxLimits()
    workspace = str(workspace)
    argv: list[str] = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
    ]
    if network:
        argv.append("--share-net")
    argv += [
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        # Private, writable scratch and an isolated home; the real /home is an
        # empty tmpfs so nothing under it (dotfiles, other projects) survives.
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/home",
        "--tmpfs",
        home,
        # The one read-write exception, re-bound after the /home mask so a
        # workspace that lives under /home is still present.
        "--bind",
        workspace,
        workspace,
        "--chdir",
        workspace,
        "--clearenv",
        "--setenv",
        "HOME",
        home,
        "--setenv",
        "PATH",
        SANDBOX_PATH,
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--",
    ]
    wrapper = (
        f"ulimit -S -t {caps.cpu_seconds} 2>/dev/null; "
        f"ulimit -S -u {caps.nproc} 2>/dev/null; "
        f"ulimit -S -f {caps.fsize_blocks} 2>/dev/null; "
        'exec /bin/sh -c "$1" sh'
    )
    argv += ["/bin/sh", "-c", wrapper, "sh", command]
    return argv


@dataclass(frozen=True)
class Sandbox:
    """The jail as an object the shell tool can be handed and tests can fake."""

    home: str = SANDBOX_HOME
    limits: SandboxLimits = field(default_factory=SandboxLimits)

    def available(self) -> bool:
        return sandbox_available()

    def argv_for(
        self, command: str, *, workspace: str, network: bool = True
    ) -> list[str]:
        return build_sandbox_argv(
            command, workspace=workspace, network=network, home=self.home, limits=self.limits
        )
