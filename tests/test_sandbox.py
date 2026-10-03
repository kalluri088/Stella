"""Tests for the bubblewrap jail builder — the pure argv, never a live spawn.

The argv construction is where correctness lives: read-only root, one
read-write exception, the home mask ordered before the workspace re-bind, the
network toggle, the ulimit safety nets, and above all that the user's command
is carried as a trailing positional rather than interpolated into the wrapper.
A single real end-to-end run proves the flags work on a machine that has
bubblewrap; everywhere else these are pure-function assertions.
"""

from __future__ import annotations

import sys

import pytest

from stella import sandbox as sb
from stella.sandbox import (
    ENV_SANDBOX,
    Sandbox,
    SandboxLimits,
    build_sandbox_argv,
    sandbox_requested,
)


class TestSandboxRequested:
    @pytest.mark.parametrize("value", ["", "1", "true", "on", "yes", "anything"])
    def test_on_unless_explicitly_off(self, value):
        assert sandbox_requested({ENV_SANDBOX: value}) is True

    def test_absent_defaults_on(self):
        assert sandbox_requested({}) is True

    @pytest.mark.parametrize("value", ["0", "false", "off", "no", "OFF", " No "])
    def test_recognised_off_spellings_disable(self, value):
        assert sandbox_requested({ENV_SANDBOX: value}) is False


class TestAvailability:
    def test_windows_never_available(self, monkeypatch):
        monkeypatch.setattr(sb, "platform_name", lambda: "windows")
        assert sb.sandbox_available({}) is False

    def test_missing_bwrap_never_available(self, monkeypatch):
        monkeypatch.setattr(sb, "platform_name", lambda: "linux")
        monkeypatch.setattr(sb.shutil, "which", lambda *_a, **_k: None)
        assert sb.sandbox_available({}) is False

    def test_disabled_userns_blocks_it(self, monkeypatch):
        monkeypatch.setattr(sb, "platform_name", lambda: "linux")
        monkeypatch.setattr(sb.shutil, "which", lambda *_a, **_k: "/usr/bin/bwrap")
        monkeypatch.setattr(sb, "_userns_enabled", lambda: False)
        assert sb.sandbox_available({}) is False

    def test_present_bwrap_and_userns_enable_it(self, monkeypatch):
        monkeypatch.setattr(sb, "platform_name", lambda: "linux")
        monkeypatch.setattr(sb.shutil, "which", lambda *_a, **_k: "/usr/bin/bwrap")
        monkeypatch.setattr(sb, "_userns_enabled", lambda: True)
        assert sb.sandbox_available({}) is True


def _adjacent(argv: list[str], option: str) -> tuple[str, ...]:
    """The option value(s) that follow ``--option`` until the next flag."""

    out: list[str] = []
    i = argv.index(option) + 1
    while i < len(argv) and not argv[i].startswith("-"):
        out.append(argv[i])
        i += 1
    return tuple(out)


class TestBuildArgv:
    def test_starts_with_bwrap_and_uses_core_guarantees(self):
        argv = build_sandbox_argv("echo hi", workspace="/ws")
        assert argv[0] == "bwrap"
        assert "--die-with-parent" in argv
        assert "--unshare-all" in argv
        assert "--new-session" in argv

    def test_host_is_read_only_and_workspace_is_the_writable_exception(self):
        argv = build_sandbox_argv("ls", workspace="/ws")
        assert _adjacent(argv, "--ro-bind") == ("/", "/")
        assert _adjacent(argv, "--bind") == ("/ws", "/ws")

    def test_home_is_masked_before_the_workspace_is_re_bound(self):
        # Ordering matters: a workspace under /home only survives because the
        # bind comes after the /home tmpfs mask.
        argv = build_sandbox_argv("ls", workspace="/home/u/proj")
        assert "/tmp" in argv  # private scratch tmpfs
        assert "/home" in argv  # the mask (an exact element, not the prefix)
        assert argv.index("/home") < argv.index("/home/u/proj")

    def test_network_default_shares_net_and_false_closes_it(self):
        assert "--share-net" in build_sandbox_argv("curl x", workspace="/ws")
        assert "--share-net" not in build_sandbox_argv(
            "grep x", workspace="/ws", network=False
        )

    def test_environment_is_cleared_then_minimal(self):
        argv = build_sandbox_argv("x", workspace="/ws")
        assert "--clearenv" in argv
        assert _adjacent(argv, "--setenv")  # HOME ... present
        assert "HOME" in argv and "PATH" in argv
        assert sb.SANDBOX_HOME in argv

    def test_command_is_a_trailing_positional_not_in_the_wrapper(self):
        nasty = "echo it's $HOME; rm -rf /"
        argv = build_sandbox_argv(nasty, workspace="/ws")
        assert argv[-1] == nasty
        wrapper = argv[argv.index("-c") + 1]
        assert nasty not in wrapper
        assert "$1" in wrapper

    def test_ulimit_safety_nets_are_set(self):
        argv = build_sandbox_argv("x", workspace="/ws", limits=SandboxLimits(cpu_seconds=11, nproc=22, fsize_blocks=33))
        wrapper = argv[argv.index("-c") + 1]
        assert "ulimit -S -t 11" in wrapper
        assert "ulimit -S -u 22" in wrapper
        assert "ulimit -S -f 33" in wrapper

    def test_only_posix_shells_are_wrapped_never_a_bare_command(self):
        # The program tail is exactly: sh -c <wrapper> sh <command>.
        argv = build_sandbox_argv("true", workspace="/ws")
        tail = argv[argv.index("--") + 1 :]
        assert tail[0] == "/bin/sh" and tail[1] == "-c"
        assert tail[3] == "sh" and tail[4] == "true"


class TestSandboxObject:
    def test_argv_for_delegates_with_home_and_limits(self):
        sandbox = Sandbox(home="/home/custom", limits=SandboxLimits(cpu_seconds=5))
        argv = sandbox.argv_for("echo", workspace="/ws", network=False)
        assert "/home/custom" in argv
        assert "--share-net" not in argv
        wrapper = argv[argv.index("-c") + 1]
        assert "ulimit -S -t 5" in wrapper

    def test_default_sandbox_reports_host_availability(self):
        # Whatever the host is, the object must agree with the module probe.
        assert Sandbox().available() is sb.sandbox_available()


@pytest.mark.skipif(sys.platform != "linux", reason="bubblewrap needs POSIX")
@pytest.mark.skipif(
    not sb.sandbox_available(), reason="bubblewrap not present on this host"
)
def test_real_jail_runs_a_command():
    import subprocess

    argv = build_sandbox_argv("echo jailed-ok", workspace="/tmp")
    done = subprocess.run(
        argv, capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0
    assert "jailed-ok" in done.stdout
