"""Tests for the Stella-owned llama-server brain process and client.

The launch line itself is pinned byte-for-byte: research report 09
measured exactly these flags on this machine, so a silent change to it
is a change to the measured VRAM/speed contract and must fail loudly.
"""

from __future__ import annotations

import signal
import sqlite3
import subprocess

import pytest

from stella import app, portable
from stella.app import StellaApplication, StellaSession, StellaSettings
from stella.brain import Brain, Decision
from stella.llama_server import (
    LlamaBrainError,
    LlamaBrainServer,
    LlamaServerLLMClient,
)
from stella.llm import FakeLLMClient as FakeLLM
from stella.memory import InMemoryMemory
from stella.ollama_client import OllamaLLMClient
from stella.stella import Stella
from stella.tools import ToolDispatcher


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the developer's real configuration in tests."""

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("WIN_PD_OVERRIDE_LOCAL_APPDATA", str(tmp_path / "share"))


# ---------------------------------------------------------- launch line


def test_command_is_the_measured_round_c_launch_line() -> None:
    server = LlamaBrainServer(
        "/models/gpt-oss-20b-mxfp4.gguf",
        binary="/opt/llama/bin/llama-server",
        port=9001,
    )

    assert server.command == [
        "/opt/llama/bin/llama-server",
        "-m",
        "/models/gpt-oss-20b-mxfp4.gguf",
        "--host",
        "127.0.0.1",
        "--port",
        "9001",
        "-ngl",
        "99",
        "-cmoe",
        "-c",
        "16384",
        "-np",
        "2",
        "-ctk",
        "q8_0",
        "-ctv",
        "q8_0",
        "--spec-type",
        "ngram-mod",
        "--spec-ngram-mod-n-min",
        "2",
        "--spec-ngram-mod-n-max",
        "8",
        "--jinja",
    ]


def test_extra_args_are_appended_after_the_measured_line() -> None:
    command = LlamaBrainServer(
        "m.gguf", extra_args=("--alias", "brain")
    ).command

    assert command[-2:] == ["--alias", "brain"]
    assert command[-3] == "--jinja"
    assert command.count("-m") == 1


def test_base_url_points_at_the_compatibility_endpoint() -> None:
    server = LlamaBrainServer("m.gguf", host="0.0.0.0", port=8080)
    assert server.base_url == "http://0.0.0.0:8080/v1"


# ------------------------------------------------------------- lifecycle


class FakeProcess:
    """Scripted stand-in for the spawned llama-server child."""

    def __init__(
        self,
        *,
        exit_on_signal: bool = True,
        exit_on_terminate: bool = False,
    ) -> None:
        self.returncode: int | None = None
        self.exit_on_signal = exit_on_signal
        self.exit_on_terminate = exit_on_terminate
        self.signals: list[int] = []
        self.terminates = 0
        self.kills = 0

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, signal_number: int) -> None:
        self.signals.append(signal_number)
        if self.exit_on_signal:
            self.returncode = 0

    def terminate(self) -> None:
        self.terminates += 1
        if self.exit_on_terminate:
            self.returncode = 0

    def kill(self) -> None:
        self.kills += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int | None:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="llama-server", timeout=0)
        return self.returncode


class FakePopen:
    """Records the spawn call and writes test bytes into the log handle."""

    def __init__(self, process: FakeProcess, log_text: bytes = b"") -> None:
        self.process = process
        self.log_text = log_text
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        handle = kwargs.get("stdout")
        if handle is not None and self.log_text:
            handle.write(self.log_text)
            handle.flush()
        return self.process


@pytest.fixture
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stella.llama_server.HEALTH_POLL_SECONDS", 0.0)


def ask_for_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decide the platform a signal assertion is actually about.

    These tests say *the first rung is SIGINT* — a fact about POSIX, not
    about whichever machine the suite happens to run on, and the brain
    now asks :func:`stella.portable.polite_stop` which rung this platform
    can deliver. The Windows rung has its own test. Nothing is weakened:
    the assertion is the same, it just stops borrowing the host.
    """

    monkeypatch.setattr(
        portable, "platform_name", lambda sys_platform=None: portable.LINUX
    )


def test_start_spawns_the_command_and_waits_for_health(
    fast_polling, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcess()
    popen = FakePopen(process)
    monkeypatch.setattr("stella.llama_server.subprocess.Popen", popen)
    server = LlamaBrainServer("m.gguf")
    health = iter([False, False, True])
    monkeypatch.setattr(server, "_health_ok", lambda: next(health))

    server.start()

    assert server.alive
    argv, kwargs = popen.calls[0]
    assert argv == server.command
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    server.stop()


def test_start_reports_an_early_exit_with_the_log_tail(
    fast_polling, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcess()
    process.returncode = 1
    popen = FakePopen(process, b"ggml_init: bind: address already in use\n")
    monkeypatch.setattr("stella.llama_server.subprocess.Popen", popen)
    server = LlamaBrainServer("m.gguf")

    with pytest.raises(
        LlamaBrainError, match="exited before it was ready"
    ) as caught:
        server.start()

    assert "bind: address already in use" in str(caught.value)
    assert not server.alive
    assert server._log_path is None


def test_start_times_out_loudly_and_stops_the_child(
    fast_polling, monkeypatch: pytest.MonkeyPatch
) -> None:
    ask_for_posix(monkeypatch)
    process = FakeProcess()
    popen = FakePopen(process)
    monkeypatch.setattr("stella.llama_server.subprocess.Popen", popen)
    server = LlamaBrainServer("m.gguf")
    monkeypatch.setattr(server, "_health_ok", lambda: False)
    server.ready_timeout = 0.05

    with pytest.raises(LlamaBrainError, match="did not become ready"):
        server.start()

    # The failed start still owns its child: the stop ladder ran.
    assert process.signals == [signal.SIGINT]
    assert process.returncode == 0


def test_spawn_failure_is_actionable_and_cleans_the_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raising(argv, **kwargs):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr("stella.llama_server.subprocess.Popen", raising)
    server = LlamaBrainServer("m.gguf", binary="not-installed")

    with pytest.raises(LlamaBrainError, match="STELLA_LLAMA_SERVER_BINARY"):
        server.start()

    assert server._log_path is None


def test_start_refuses_a_port_that_already_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popen = FakePopen(FakeProcess())
    monkeypatch.setattr("stella.llama_server.subprocess.Popen", popen)
    server = LlamaBrainServer("m.gguf")
    monkeypatch.setattr(server, "_health_ok", lambda: True)

    with pytest.raises(LlamaBrainError, match="already serving"):
        server.start()

    assert popen.calls == []


def test_stop_prefers_sigint_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ask_for_posix(monkeypatch)
    process = FakeProcess()
    server = LlamaBrainServer("m.gguf")
    server._process = process

    server.stop()
    server.stop()

    assert process.signals == [signal.SIGINT]
    assert process.terminates == 0
    assert process.kills == 0
    assert not server.alive


def test_stop_escalates_when_the_child_ignores_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ask_for_posix(monkeypatch)
    process = FakeProcess(exit_on_signal=False, exit_on_terminate=False)
    server = LlamaBrainServer("m.gguf")
    server._process = process

    server.stop()

    assert process.signals == [signal.SIGINT]
    assert process.terminates == 1
    assert process.kills == 1
    assert not server.alive


def test_stop_asks_portable_how_to_be_polite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows cannot deliver SIGINT to another process — ``send_signal``
    # raises ``ValueError`` there, which is how the brain used to die on
    # that platform before it ever reached terminate. Forcing the
    # *decision* runs the branch a Windows user gets, here.
    monkeypatch.setattr(
        portable,
        "platform_name",
        lambda sys_platform=None: portable.WINDOWS,
    )
    process = FakeProcess(exit_on_signal=False, exit_on_terminate=True)
    server = LlamaBrainServer("m.gguf")
    server._process = process

    server.stop()

    assert process.signals == []
    assert process.terminates == 1
    assert process.kills == 0
    assert not server.alive


def test_context_manager_starts_and_stops() -> None:
    server = LlamaBrainServer("m.gguf")
    events: list[str] = []
    server.start = lambda: events.append("start")  # type: ignore[method-assign]
    server.stop = lambda: events.append("stop")  # type: ignore[method-assign]

    with server as entered:
        assert entered is server

    assert events == ["start", "stop"]


# ---------------------------------------------------------------- client


def test_client_is_the_ollama_compatibility_path() -> None:
    client = LlamaServerLLMClient("model.gguf", "http://127.0.0.1:9001/v1")

    assert isinstance(client, OllamaLLMClient)
    assert client.native is False
    assert client.model == "model.gguf"


# ------------------------------------------------ settings from environment


def _clear_llama_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "STELLA_MODEL",
        "STELLA_LLM_PROVIDER",
        "STELLA_LLAMA_SERVER_BINARY",
        "STELLA_LLAMA_SERVER_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_from_environment_accepts_the_llama_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_llama_environment(monkeypatch)
    monkeypatch.setenv("STELLA_MODEL", "/models/brain.gguf")
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "llama")
    monkeypatch.setenv("STELLA_LLAMA_SERVER_BINARY", "/opt/bin/llama-server")
    monkeypatch.setenv("STELLA_LLAMA_SERVER_PORT", "9123")

    settings = StellaSettings.from_environment()

    assert settings.provider == "llama"
    assert settings.llama_binary == "/opt/bin/llama-server"
    assert settings.llama_port == 9123


@pytest.mark.parametrize("port", ["0", "-1", "65536", "http", ""])
def test_an_invalid_llama_port_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    _clear_llama_environment(monkeypatch)
    monkeypatch.setenv("STELLA_MODEL", "m")
    monkeypatch.setenv("STELLA_LLAMA_SERVER_PORT", port)

    with pytest.raises(SystemExit, match="STELLA_LLAMA_SERVER_PORT"):
        StellaSettings.from_environment()


def test_from_environment_still_rejects_unknown_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_llama_environment(monkeypatch)
    monkeypatch.setenv("STELLA_MODEL", "m")
    monkeypatch.setenv("STELLA_LLM_PROVIDER", "anthropic")

    with pytest.raises(SystemExit, match="llama"):
        StellaSettings.from_environment()


# ----------------------------------------------- application integration


class FakeBrainServer:
    """Stands in for LlamaBrainServer inside build/close tests."""

    def __init__(
        self,
        model_path: str,
        *,
        binary: str,
        host: str = "127.0.0.1",
        port: int = 8080,
        **_kwargs: object,
    ) -> None:
        self.model_path = model_path
        self.binary = binary
        self.port = port
        self.starts = 0
        self.stops = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


class SpyClient:
    """Stands in for LlamaServerLLMClient; records its construction."""

    last: SpyClient | None = None

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        answer_max_output_tokens: int | None = None,
        decision_max_output_tokens: int | None = None,
        usage: object | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.answer_max_output_tokens = answer_max_output_tokens
        self.decision_max_output_tokens = decision_max_output_tokens
        self.usage = usage
        SpyClient.last = self


def _llama_settings(tmp_path, **overrides) -> StellaSettings:
    fields = {
        "provider": "llama",
        "model": str(tmp_path / "brain.gguf"),
        "memory_db": str(tmp_path / "memory.db"),
        "history_db": str(tmp_path / "history.db"),
        "transcripts_db": str(tmp_path / "transcripts.db"),
        "semantic_db": str(tmp_path / "semantic.db"),
        "workspace": str(tmp_path / "workspace"),
        "llama_binary": "/bin/llama-server",
        "llama_port": 9111,
    }
    fields.update(overrides)
    return StellaSettings(**fields)


@pytest.fixture
def patched_brain(monkeypatch: pytest.MonkeyPatch):
    """Replace the real server/client classes in the build path."""

    created: list[FakeBrainServer] = []

    def factory(model_path, **kwargs):
        server = FakeBrainServer(model_path, **kwargs)
        created.append(server)
        return server

    monkeypatch.setattr(app, "LlamaBrainServer", factory)
    monkeypatch.setattr(app, "LlamaServerLLMClient", SpyClient)
    SpyClient.last = None
    return created


def test_build_starts_and_close_stops_the_owned_brain(
    tmp_path, patched_brain
) -> None:
    application = app.build_application(_llama_settings(tmp_path))

    server = patched_brain[0]
    assert (server.model_path, server.binary, server.port) == (
        str(tmp_path / "brain.gguf"),
        "/bin/llama-server",
        9111,
    )
    assert server.starts == 1
    assert application.brain_server is server
    assert SpyClient.last is not None
    assert SpyClient.last.base_url == server.base_url
    # The session's usage recorder reaches every client, not just Ollama's —
    # otherwise `/usage` would quietly report nothing on a llama.cpp brain.
    assert SpyClient.last.usage is not None

    application.close()
    assert server.stops == 1


def test_a_failing_build_never_starts_the_brain(
    tmp_path, patched_brain
) -> None:
    # Pointing the memory database at an existing directory makes the
    # build fail midway; the server start is the last step, so nothing
    # is ever spawned to be stranded.
    with pytest.raises(sqlite3.OperationalError):
        app.build_application(
            _llama_settings(tmp_path, memory_db=str(tmp_path))
        )

    assert patched_brain == [] or all(
        server.starts == 0 for server in patched_brain
    )


def test_non_llama_builds_have_no_brain_server(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-secret")
    application = app.build_application(
        _llama_settings(tmp_path, provider="openai", model="gpt-test")
    )
    try:
        assert application.brain_server is None
    finally:
        application.close()


# ------------------------------------------------ apply-settings collision


class SilentBrain(Brain):
    def decide(self, context, should_cancel=None) -> Decision:
        raise AssertionError("no turns in this test")


def _make_llama_application(
    settings: StellaSettings, server: FakeBrainServer
) -> StellaApplication:
    stella = Stella(
        SilentBrain(),
        FakeLLM(),
        ToolDispatcher([]),
        InMemoryMemory(),
    )
    return StellaApplication(
        StellaSession(stella), settings, brain_server=server
    )


def _wait_for(bridge: app.StellaBridge, kind: str) -> None:
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(event.kind == kind for event in bridge.poll()):
            return
        time.sleep(0.02)
    raise AssertionError(f"no {kind!r} event")


def test_applying_llama_settings_frees_the_shared_port_first(
    tmp_path, patched_brain, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_server = FakeBrainServer(
        "old.gguf", binary="/bin/llama-server", port=9111
    )
    old_settings = _llama_settings(tmp_path)
    old = _make_llama_application(old_settings, old_server)
    new = _make_llama_application(
        old_settings, FakeBrainServer("new.gguf", binary="b", port=9111)
    )

    def replacement(settings: StellaSettings) -> StellaApplication:
        # At build time the retiring brain must already have released
        # the port, or the new server could never bind it.
        assert old_server.stops == 1
        return new

    monkeypatch.setattr(app, "build_application", replacement)
    bridge = app.StellaBridge(lambda: old)
    try:
        bridge.post_apply_settings(old_settings)
        _wait_for(bridge, "settings")
        assert bridge._application is new
    finally:
        bridge.stop()


def test_applying_a_different_port_keeps_the_running_brain(
    tmp_path, patched_brain, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_server = FakeBrainServer(
        "old.gguf", binary="/bin/llama-server", port=9111
    )
    old = _make_llama_application(
        _llama_settings(tmp_path), old_server
    )
    new = _make_llama_application(
        _llama_settings(tmp_path, llama_port=9222),
        FakeBrainServer("new.gguf", binary="b", port=9222),
    )

    def replacement(settings: StellaSettings) -> StellaApplication:
        # A different port needs no pre-stop: the build-first safety net
        # (a bad config cannot destroy the working session) stands.
        assert old_server.stops == 0
        return new

    monkeypatch.setattr(app, "build_application", replacement)
    bridge = app.StellaBridge(lambda: old)
    try:
        bridge.post_apply_settings(_llama_settings(tmp_path, llama_port=9222))
        _wait_for(bridge, "settings")
    finally:
        bridge.stop()
