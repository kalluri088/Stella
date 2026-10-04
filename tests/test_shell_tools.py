"""Tests for the one shell capability Stella added: ``shell_run``.

Two kinds of test live here. Most of them drive the tool through an injected
fake runner, so every decision — validation, the approval surface, result
shaping, untrusted-content wrapping, the timeout/truncation notes, the
registration gate, the config round-trip — is proven without starting a single
process. A small final class instead exercises the real bounded reader with a
handful of tiny, workspace-confined, sub-second commands, because the memory
cap and the wall-clock kill are exactly the kind of guarantee that is only
true if it runs.
"""

from __future__ import annotations

import json
import sys

import pytest

from stella import config as config_module
from stella import shell_tools
from stella.app import StellaSettings
from stella.sandbox import build_sandbox_argv, sandbox_available
from stella.shell_tools import (
    MAX_COMMAND_CHARS,
    MAX_OUTPUT_BYTES,
    TIMEOUT_SECONDS,
    ShellRun,
    ShellRunTool,
    build_shell_tools,
    shell_tool_summaries,
)
from stella.tools import (
    CONTENT_CLOSE,
    CONTENT_OPEN,
    MAX_PREVIEW_LINES,
    ApprovalRequest,
    ToolDispatcher,
    action_summary,
)


@pytest.fixture
def workspace(tmp_path):
    directory = tmp_path / "ws"
    directory.mkdir()
    return directory


class _FakeSandbox:
    """A stand-in jail whose availability and argv are scripted, never probed.

    Lets the tool's decision (jail vs confined, network on vs off) be proven
    without depending on whether this particular machine has bwrap.
    """

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.requests: list[tuple[str, str, bool]] = []

    def available(self) -> bool:
        return self._available

    def argv_for(self, command, *, workspace, network=True) -> list[str]:
        self.requests.append((command, workspace, network))
        return build_sandbox_argv(command, workspace=workspace, network=network)


class _FakeRunner:
    """Records what the tool asked to run and returns a scripted outcome."""

    def __init__(self, result: ShellRun | None = None) -> None:
        self.result = result or ShellRun(0, b"", False, False)
        self.calls: list[tuple[list[str], str, float]] = []

    def __call__(self, argv, cwd, timeout) -> ShellRun:
        self.calls.append((argv, cwd, timeout))
        return self.result


def _run(returncode=0, output=b"", truncated=False, timed_out=False):
    return ShellRun(returncode, output, truncated, timed_out)


class TestValidateArguments:
    @pytest.mark.parametrize(
        "arguments",
        [
            {"command": "echo hi"},
            {"command": "ls -la\npwd"},  # multi-line scripts are legitimate
            {"command": "x" * MAX_COMMAND_CHARS},
            {"command": "curl x", "network": True},
            {"command": "grep x", "network": False},  # opt out of network
        ],
    )
    def test_accepts(self, workspace, arguments):
        assert ShellRunTool(workspace).validate_arguments(arguments)

    @pytest.mark.parametrize(
        "arguments",
        [
            {},
            {"command": "ls", "extra": 1},
            {"path": "ls"},
            {"command": ""},
            {"command": "   "},
            {"command": "a\x00b"},
            {"command": "x" * (MAX_COMMAND_CHARS + 1)},
            {"command": 7},
            {"command": None},
            {"command": "ok", "network": "yes"},  # network must be a real bool
            {"command": "ok", "network": 1},
            "not a dict",
        ],
    )
    def test_rejects(self, workspace, arguments):
        assert not ShellRunTool(workspace).validate_arguments(arguments)


class TestExecutionDecisions:
    def test_clean_run_is_success_and_wrapped_as_untrusted(self, workspace):
        runner = _FakeRunner(_run(0, b"hello\n"))
        result = ShellRunTool(workspace, runner=runner).execute(
            {"command": "echo hello"}
        )
        assert result.success is True
        assert result.action_receipt.status == "verified"
        assert result.output.startswith(CONTENT_OPEN)
        assert result.output.endswith(CONTENT_CLOSE)
        assert "hello" in result.output

    def test_argv_cwd_and_timeout_are_the_workspace_and_the_bound(self, workspace):
        runner = _FakeRunner()
        ShellRunTool(workspace, runner=runner).execute({"command": "true"})
        argv, cwd, timeout = runner.calls[0]
        assert cwd == str(workspace)
        assert timeout == TIMEOUT_SECONDS
        if sys.platform != "win32":
            assert argv[:2] == ["/bin/sh", "-c"]
            assert argv[2] == "true"

    def test_missing_workspace_refuses_before_spawning(self, tmp_path):
        runner = _FakeRunner()
        result = ShellRunTool(tmp_path / "nope", runner=runner).execute(
            {"command": "echo hi"}
        )
        assert result.success is False
        assert result.output == "Workspace unavailable."
        assert runner.calls == []  # never reached the runner

    def test_nonzero_exit_is_reported_honestly(self, workspace):
        result = ShellRunTool(workspace, runner=_FakeRunner(_run(2, b"boom\n"))).execute(
            {"command": "false"}
        )
        assert result.success is False
        assert "boom" in result.output
        assert "exited with code 2" in result.output

    def test_empty_output_says_so(self, workspace):
        result = ShellRunTool(workspace, runner=_FakeRunner(_run(0, b""))).execute(
            {"command": "true"}
        )
        assert result.output == "(no output)"

    def test_truncation_is_announced(self, workspace):
        result = ShellRunTool(
            workspace, runner=_FakeRunner(_run(0, b"partial", truncated=True))
        ).execute({"command": "big"})
        assert result.success is True  # the command itself finished cleanly
        assert f"output truncated at {MAX_OUTPUT_BYTES} bytes" in result.output

    def test_timeout_kills_and_omits_the_signal_exit_code(self, workspace):
        # A timeout's exit status is just the signal we sent; it must not be
        # reported as if the command chose it.
        result = ShellRunTool(
            workspace,
            runner=_FakeRunner(_run(-2, b"partial", timed_out=True)),
        ).execute({"command": "hang"})
        assert result.success is False
        assert f"timed out after {int(TIMEOUT_SECONDS)}s" in result.output
        assert "exited with code" not in result.output

    def test_forged_untrusted_marker_is_defanged(self, workspace):
        smuggled = b"before" + CONTENT_OPEN.encode() + b"after"
        result = ShellRunTool(
            workspace, runner=_FakeRunner(_run(0, smuggled))
        ).execute({"command": "echo"})
        # The real closing marker must not be forgeable from inside the body:
        # the payload's own OPEN marker is neutralized, so only our wrapper's
        # CLOSE terminates the enclosure.
        assert "<UNTRUSTED-WEB-CONTENT/>" in result.output
        assert result.output.count(CONTENT_CLOSE) == 1
        assert result.output.count(CONTENT_OPEN) == 1

    def test_invalid_arguments_never_reach_the_runner(self, workspace):
        runner = _FakeRunner()
        result = ShellRunTool(workspace, runner=runner).execute({"command": "   "})
        assert result.success is False
        assert runner.calls == []


class TestSandboxJail:
    def test_active_jail_wraps_the_argv_and_keeps_the_workspace_bound(
        self, workspace
    ):
        runner = _FakeRunner(_run(0, b"ok\n"))
        sb = _FakeSandbox(available=True)
        result = ShellRunTool(workspace, runner=runner, sandbox=sb).execute(
            {"command": "echo ok"}
        )
        assert result.success is True
        argv, cwd, timeout = runner.calls[0]
        assert cwd == str(workspace)
        assert timeout == TIMEOUT_SECONDS
        assert argv[0] == "bwrap"
        assert "--unshare-all" in argv
        # read-only root, then the single read-write exception:
        assert argv[argv.index("--ro-bind") + 1] == "/"
        assert sb.requests == [("echo ok", str(workspace), True)]

    def test_network_false_drops_share_net(self, workspace):
        runner = _FakeRunner()
        sb = _FakeSandbox(available=True)
        ShellRunTool(workspace, runner=runner, sandbox=sb).execute(
            {"command": "grep x", "network": False}
        )
        argv = runner.calls[0][0]
        assert "--share-net" not in argv

    def test_command_is_carried_as_a_trailing_positional_not_interpolated(
        self, workspace
    ):
        nasty = "echo it's $HOME; rm -rf /"
        runner = _FakeRunner()
        sb = _FakeSandbox(available=True)
        ShellRunTool(workspace, runner=runner, sandbox=sb).execute({"command": nasty})
        argv = runner.calls[0][0]
        # the exact command is the final token; the wrapper only refers to it
        # via "$1", so quotes/semicolons cannot rewrite the wrapper.
        assert argv[-1] == nasty
        wrapper = argv[argv.index("-c") + 1]
        assert nasty not in wrapper
        assert "$1" in wrapper

    def test_unavailable_jail_falls_back_to_confined_and_says_so(self, workspace):
        runner = _FakeRunner(_run(0, b"ok\n"))
        sb = _FakeSandbox(available=False)
        result = ShellRunTool(workspace, runner=runner, sandbox=sb).execute(
            {"command": "echo ok"}
        )
        argv = runner.calls[0][0]
        assert argv[:2] == ["/bin/sh", "-c"]  # confined path, not bwrap
        assert "WITHOUT the filesystem jail" in result.output
        assert sb.requests == []  # never asked to build a jail argv

    def test_jail_off_never_consults_the_sandbox(self, workspace):
        runner = _FakeRunner()
        sb = _FakeSandbox(available=True)
        ShellRunTool(workspace, runner=runner, sandbox=sb, jail=False).execute(
            {"command": "echo ok"}
        )
        assert runner.calls[0][0][:2] == ["/bin/sh", "-c"]
        assert sb.requests == []


class TestPreview:
    def test_shows_the_literal_command_and_the_warning(self, workspace):
        preview = ShellRunTool(workspace).preview(
            ApprovalRequest("shell_run", {"command": "echo hi"})
        )
        assert preview is not None
        assert "$ echo hi" in preview.detail_lines
        assert preview.truncated is False
        # Default tool has no jail wired → honest "off" warning, net allowed.
        assert "not a filesystem jail" in preview.warning
        assert "network is allowed" in preview.warning

    def test_active_jail_warning_is_honest_about_the_protection(self, workspace):
        preview = ShellRunTool(
            workspace, sandbox=_FakeSandbox(available=True)
        ).preview(ApprovalRequest("shell_run", {"command": "echo hi", "network": False}))
        assert preview is not None
        assert "bubblewrap jail" in preview.warning
        assert "network is blocked" in preview.warning

    def test_unavailable_jail_says_it_is_not_a_jail(self, workspace):
        preview = ShellRunTool(
            workspace, sandbox=_FakeSandbox(available=False)
        ).preview(ApprovalRequest("shell_run", {"command": "echo hi"}))
        assert preview is not None
        assert "is not available" in preview.warning
        assert "NOT active" in preview.warning

    def test_long_command_is_bounded_to_preview_lines(self, workspace):
        many = "\n".join(f"echo line{i}" for i in range(MAX_PREVIEW_LINES + 20))
        preview = ShellRunTool(workspace).preview(
            ApprovalRequest("shell_run", {"command": many})
        )
        assert preview.truncated is True
        assert len(preview.detail_lines) <= MAX_PREVIEW_LINES

    @pytest.mark.parametrize("command", ["", "   ", 5, None])
    def test_nothing_to_show_yields_no_preview(self, workspace, command):
        assert (
            ShellRunTool(workspace).preview(
                ApprovalRequest("shell_run", {"command": command})
            )
            is None
        )


class TestSummaries:
    def test_summary_leads_with_the_command(self, workspace):
        summary = shell_tool_summaries("shell_run", {"command": "echo hi"})
        assert summary is not None
        assert "shell command" in summary
        assert "echo hi" in summary

    def test_other_capabilities_are_not_mine(self):
        assert shell_tool_summaries("web_search", {"query": "x"}) is None
        assert shell_tool_summaries("shell_run", {"command": "  "}) is None

    def test_long_command_is_clipped_in_the_summary(self):
        summary = shell_tool_summaries("shell_run", {"command": "x" * 400})
        assert summary is not None
        assert "…" in summary

    def test_action_summary_does_not_dump_arguments(self):
        # The generic fallback would print the whole argument dict; the
        # dedicated shell summary must win first.
        text = action_summary(
            ApprovalRequest("shell_run", {"command": "rm -rf ./scratch"})
        )
        assert "run this shell command" in text
        assert "arguments" not in text


class TestRegistrationGate:
    def test_floor_is_dangerous_and_every_use_asks(self, workspace):
        tool = ShellRunTool(workspace)
        dispatcher = ToolDispatcher([tool])
        assert tool.risk_level.value == "dangerous"
        assert dispatcher.requires_approval("shell_run", {"command": "echo hi"})

    def test_build_shell_tools_exposes_exactly_one_capability(self, workspace):
        tools = build_shell_tools({}, workspace=workspace, runner=_FakeRunner())
        assert [tool.name for tool in tools] == ["shell_run"]


# ------------------------------------------------------------ config round-trip


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    for name in (
        "STELLA_MODEL",
        "STELLA_LLM_PROVIDER",
        "OPENAI_API_KEY",
        "OLLAMA_BASE_URL",
        "OPENAI_BASE_URL",
        "STELLA_SHELL_TOOLS",
        "STELLA_OS_TOOLS",
        "STELLA_WEB",
        "STELLA_OUTLINE",
        "STELLA_TRANSCRIPTS",
        "STELLA_SEMANTIC_MEMORY",
        "STELLA_WAKE_WORD",
    ):
        monkeypatch.delenv(name, raising=False)


class TestShellFlagPersists:
    def test_bare_dataclass_default_is_off(self):
        # The hand-built / environment fallback stays OFF so a STELLA_MODEL or
        # headless run gains shell_run only on request; the desktop/saved
        # default is separately ON (see test_saved_and_desktop_default_is_on).
        assert StellaSettings().shell_tools_enabled is False

    def test_saved_and_desktop_default_is_on(self, isolated_data_dir, monkeypatch):
        monkeypatch.delenv("STELLA_SHELL_TOOLS", raising=False)
        # from_saved's default arms the tool for a fresh setup...
        assert (
            StellaSettings.from_saved(provider="ollama", model="m").shell_tools_enabled
            is True
        )
        # ...and a config written before the key existed resolves ON, while an
        # explicit false (the unticked opt-out) still resolves OFF.
        config_module.config_path().parent.mkdir(parents=True, exist_ok=True)
        config_module.config_path().write_text(
            json.dumps({"provider": "ollama", "model": "m"}), encoding="utf-8"
        )
        assert config_module.resolve_settings().shell_tools_enabled is True
        config_module.save_configuration(
            StellaSettings(provider="ollama", model="m", shell_tools_enabled=False)
        )
        assert config_module.resolve_settings().shell_tools_enabled is False

    def test_saved_true_survives_and_no_command_is_persisted(self, isolated_data_dir):
        settings = StellaSettings(provider="ollama", model="m", shell_tools_enabled=True)
        config_module.save_configuration(settings)
        raw = json.loads(config_module.config_path().read_text())
        assert raw["shell_tools_enabled"] is True
        # config.json holds only the bool; a command string is never a field.
        assert "rm -rf" not in json.dumps(raw)
        assert config_module.load_configuration()["shell_tools_enabled"] is True
        assert config_module.resolve_settings().shell_tools_enabled is True

    def test_env_toggle_wins_over_the_saved_value(self, isolated_data_dir, monkeypatch):
        monkeypatch.setenv("STELLA_SHELL_TOOLS", "on")
        assert StellaSettings.from_saved(provider="ollama", model="m").shell_tools_enabled is True
        monkeypatch.setenv("STELLA_SHELL_TOOLS", "off")
        assert (
            StellaSettings.from_saved(
                provider="ollama", model="m", shell_tools_enabled=True
            ).shell_tools_enabled
            is False
        )


# ---------------------------------------------------------- the real reader


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX shell + signals")
class TestRealBoundedReader:
    def test_captures_stdout_and_exit_status(self, workspace):
        tool = ShellRunTool(workspace)
        result = tool.execute({"command": "echo reader-works"})
        assert result.success is True
        assert "reader-works" in result.output

    def test_merges_stderr_into_captured_output(self, workspace):
        tool = ShellRunTool(workspace)
        result = tool.execute({"command": "echo to-stderr 1>&2"})
        assert result.success is True
        assert "to-stderr" in result.output

    def test_reports_a_nonzero_exit(self, workspace):
        tool = ShellRunTool(workspace)
        result = tool.execute({"command": "exit 4"})
        assert result.success is False
        assert "exited with code 4" in result.output

    def test_output_is_capped_in_memory_and_announced(self, workspace):
        # Emit roughly twice the cap; the reader keeps at most the cap and
        # says the rest was cut. This is the memory guarantee, run for real.
        tool = ShellRunTool(workspace)
        payload = MAX_OUTPUT_BYTES * 2
        result = tool.execute(
            {
                "command": (
                    f"{sys.executable} -c "
                    f"'import sys; sys.stdout.write(\"A\" * {payload})'"
                )
            }
        )
        body = result.output.split(CONTENT_OPEN, 1)[1].split(CONTENT_CLOSE, 1)[0]
        assert body.count("A") <= MAX_OUTPUT_BYTES
        assert f"output truncated at {MAX_OUTPUT_BYTES} bytes" in result.output

    def test_timeout_kills_a_stuck_command_quickly(self, workspace, monkeypatch):
        # Force a short deadline and prove the wall-clock kill fires long
        # before the command's own sleep would end.
        monkeypatch.setattr(shell_tools, "TIMEOUT_SECONDS", 0.4)
        tool = ShellRunTool(workspace)
        result = tool.execute({"command": "sleep 5"})
        assert result.success is False
        assert "timed out after 0s" in result.output

    def test_timeout_takes_the_whole_group_down(self, workspace, monkeypatch):
        # A pipeline grandchild (sleep | cat) is killed with the group, not
        # orphaned: the tool returns promptly rather than waiting on it.
        monkeypatch.setattr(shell_tools, "TIMEOUT_SECONDS", 0.5)
        tool = ShellRunTool(workspace)
        result = tool.execute({"command": "sleep 8 | cat"})
        assert result.success is False
        assert "timed out" in result.output


@pytest.mark.skipif(sys.platform != "linux", reason="bubblewrap needs POSIX")
@pytest.mark.skipif(
    not sandbox_available(), reason="bubblewrap not present on this host"
)
class TestRealJail:
    """End-to-end proof the wired capability actually jails, on a bwrap box.

    Skipped (not failed) where bubblewrap is absent, so CI without it is
    honest: the jail is not claimed where it cannot run.
    """

    def test_jailed_run_succeeds_and_host_is_read_only(self, workspace):
        tool = build_shell_tools({}, workspace=workspace)[0]
        probe = (
            "[ -f /etc/hostname ] && echo HOST_VISIBLE; "
            "(touch /escape 2>/dev/null && echo WROTE || echo BLOCKED); "
            "echo HOME=$HOME"
        )
        result = tool.execute({"command": probe})
        assert result.success is True
        assert "HOST_VISIBLE" in result.output
        assert "BLOCKED" in result.output  # host root is read-only in the jail
        assert "HOME=/home/stella" in result.output

    def test_jailed_run_cannot_see_the_real_home_contents(self, workspace):
        # /home is masked; anything the owner keeps there is absent in the box.
        tool = build_shell_tools({}, workspace=workspace)[0]
        result = tool.execute({"command": "ls -a /home"})
        body = result.output.split(CONTENT_OPEN, 1)[1].split(CONTENT_CLOSE, 1)[0]
        # Only the sandbox home mount and the re-bound workspace chain survive;
        # no listing of the owner's real dotfiles/projects.
        assert ".ssh" not in body
