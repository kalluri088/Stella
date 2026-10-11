"""Tests for the first-run configuration and bounded provider probes."""

import json
import os
import stat
import urllib.error
from types import SimpleNamespace

import pytest

from stella import config, provider_keys
from stella.app import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_TURN_TIME_LIMIT_SECONDS,
    StellaSettings,
    default_vad_model,
)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Never read or write the developer's real configuration in tests."""

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("WIN_PD_OVERRIDE_LOCAL_APPDATA", str(tmp_path / "share"))
    monkeypatch.delenv("STELLA_MODEL", raising=False)
    monkeypatch.delenv("STELLA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    # The two turn budgets have environment overrides that outrank a saved
    # choice, so a developer's shell must not bend a test's numbers.
    monkeypatch.delenv("STELLA_MAX_TOOL_CALLS", raising=False)
    monkeypatch.delenv("STELLA_TURN_TIME_LIMIT_SECONDS", raising=False)
    # So do the two voice switches, which became saved settings exactly so
    # they could be launched around — but only when this launch says so.
    monkeypatch.delenv("STELLA_VOICE_BARGE_IN", raising=False)
    monkeypatch.delenv("STELLA_SPEECH_RESIDENT", raising=False)
    # And the two that joined them: the gate a capture passes on its way to
    # the recogniser, and the path of the model that gate reads. A developer
    # who keeps silero_vad.onnx elsewhere would otherwise see every test below
    # answer with their own path instead of the one the file was written with.
    monkeypatch.delenv("STELLA_VOICE_CAPTURE_GATE", raising=False)
    monkeypatch.delenv("STELLA_VAD_MODEL", raising=False)


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _fake_urlopen(body: bytes):
    def open(request, timeout=None):
        return _Response(body)

    return open


def _raising_urlopen(error: BaseException):
    def open(request, timeout=None):
        raise error

    return open


# ----------------------------------------------------------- save/load


def test_first_run_has_no_configuration():
    assert config.load_configuration() is None
    assert config.resolve_settings() is None


def test_saved_configuration_roundtrip():
    settings = StellaSettings.from_saved(provider="ollama", model="qwen3:4b")
    config.save_configuration(settings)
    raw = config.load_configuration()
    assert raw is not None
    assert raw["provider"] == "ollama"
    assert raw["model"] == "qwen3:4b"
    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.provider == "ollama"
    assert resolved.model == "qwen3:4b"


def test_configuration_stores_only_non_secret_fields():
    settings = StellaSettings.from_saved(
        provider="openai",
        model="gpt-4o-mini",
        openai_base_url="https://gw.example/v1",
    )
    config.save_configuration(settings)
    text = config.config_path().read_text(encoding="utf-8")
    raw = json.loads(text)
    assert set(raw) == {
        "provider",
        "preset",
        "model",
        "ollama_base_url",
        "openai_base_url",
        "transcripts_enabled",
        "semantic_memory_enabled",
        "semantic_provider",
        "os_tools_enabled",
        "outline_tools_enabled",
        "web_tools_enabled",
        # The shell capability, like every other saved family: one plain
        # bool. The command string is never persisted here.
        "shell_tools_enabled",
        # The browser capability likewise: one plain bool. No URL and no
        # screenshot path ever reach config.json.
        "browser_tools_enabled",
        # The wake-word checkbox, like every other saved capability: one
        # plain bool, no path and no key.
        "wake_word_enabled",
        # The wake-word choices: classifier file names as detected on the
        # owner's disk and a display phrase. No audio, no path content.
        "wake_models",
        "wake_phrase",
        # The microphone the always-open ear listens on: one PipeWire node
        # name, which is hardware identity rather than a secret or a path
        # into anything the owner keeps.
        "wake_source",
        # The two turn budgets: plain numbers, the first numeric settings this
        # file persists. They are what the owner set in Settings, not a model
        # name, a path or anything secret.
        "max_tool_calls",
        "turn_time_limit_seconds",
        # The two voice switches, now saved like everything else the owner
        # chose: one bool and one mode name. No command line, no model name,
        # no path and no key reaches config.json for either of them.
        "speech_resident",
        "voice_barge_in",
        # The capture gate likewise: one plain bool saying whether the room
        # gets trimmed off the edges of a recording.
        "voice_capture_gate",
        # The path of the small model that does the trimming. It names a
        # hand-downloaded file the owner chose, which is input to Stella
        # rather than state she keeps, and unlike the database paths this file
        # deliberately never stores it is not a location that holds anything of
        # hers. No key, no endpoint and no command line reaches it.
        "vad_model",
    }
    assert raw["openai_base_url"] == "https://gw.example/v1"
    for secret_word in ("api_key", "sk-", "key"):
        assert secret_word not in text


def test_saved_wake_choices_roundtrip():
    settings = StellaSettings.from_saved(
        provider="ollama",
        model="m",
        wake_models=("hey_stella.onnx",),
        wake_phrase="hey stella",
    )
    assert settings.wake_models == ("hey_stella.onnx",)
    config.save_configuration(settings)
    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.wake_models == ("hey_stella.onnx",)
    assert resolved.wake_phrase == "hey stella"


def test_saved_wake_source_survives_the_config_roundtrip():
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama",
            model="m",
            wake_source="alsa_input.usb-Samson_Q9U-00.analog-stereo",
        )
    )

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.wake_source == (
        "alsa_input.usb-Samson_Q9U-00.analog-stereo"
    )
    # Whitespace a hand-edit left behind is trimmed, not trusted verbatim
    # into a command line.
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    raw["wake_source"] = "  alsa_input.pci-0000_00_1f.3.analog-stereo  "
    config.config_path().write_text(json.dumps(raw), encoding="utf-8")
    assert config.resolve_settings().wake_source == (
        "alsa_input.pci-0000_00_1f.3.analog-stereo"
    )


@pytest.mark.parametrize("value", [None, "", "   ", 7, True, ["a"], {"a": 1}])
def test_a_malformed_saved_wake_source_is_dropped_not_fatal(value: object):
    # "No saved choice" and "a saved choice this file cannot carry" are the
    # same answer: automatic. Refusing the file would discard every other
    # setting the owner saved, which the wake_models rule already rejects.
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama", model="keeper", wake_source="alsa_input.usb-x"
        )
    )
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    raw["wake_source"] = value
    config.config_path().write_text(json.dumps(raw), encoding="utf-8")

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.model == "keeper"
    assert resolved.wake_source is None


#: The subject of the test marked with this genuinely is the POSIX mode
#: bit: Windows has no 0600 to observe, and the file's privacy there is
#: the owner ACL `portable.harden_private_file` asks `icacls` for — proven
#: with fakes in `tests/test_portable.py`.
POSIX_MODES = pytest.mark.skipif(
    os.name != "posix",
    reason="0600 is a POSIX mode; on Windows the mechanism is an ACL",
)


def test_saved_turn_budgets_roundtrip():
    # The first numeric settings this file persists: what the owner typed in
    # Settings comes back out of config.json.
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama",
            model="m",
            max_tool_calls=3,
            turn_time_limit_seconds=45.5,
        )
    )

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.max_tool_calls == 3
    assert resolved.turn_time_limit_seconds == 45.5


@pytest.mark.parametrize(
    "field,value,expected",
    [
        # A count of tool calls is a whole number from 1 to 32: half a call is
        # not a call, and neither is a string, a boolean or a bound the
        # runtime would refuse to build.
        ("max_tool_calls", None, DEFAULT_MAX_TOOL_CALLS),
        ("max_tool_calls", -1, DEFAULT_MAX_TOOL_CALLS),
        ("max_tool_calls", 0, DEFAULT_MAX_TOOL_CALLS),
        ("max_tool_calls", 1.5, DEFAULT_MAX_TOOL_CALLS),
        ("max_tool_calls", "8", DEFAULT_MAX_TOOL_CALLS),
        ("max_tool_calls", True, DEFAULT_MAX_TOOL_CALLS),
        ("max_tool_calls", 999, DEFAULT_MAX_TOOL_CALLS),
        # Seconds may be fractional, but they must be positive and in scope.
        ("turn_time_limit_seconds", None, DEFAULT_TURN_TIME_LIMIT_SECONDS),
        ("turn_time_limit_seconds", -1, DEFAULT_TURN_TIME_LIMIT_SECONDS),
        ("turn_time_limit_seconds", 0, DEFAULT_TURN_TIME_LIMIT_SECONDS),
        ("turn_time_limit_seconds", "45", DEFAULT_TURN_TIME_LIMIT_SECONDS),
        ("turn_time_limit_seconds", True, DEFAULT_TURN_TIME_LIMIT_SECONDS),
        ("turn_time_limit_seconds", 601, DEFAULT_TURN_TIME_LIMIT_SECONDS),
    ],
)
def test_a_malformed_turn_budget_falls_back_to_the_default(
    field: str, value: object, expected: float
):
    # A bad number is dropped, never fatal: load_configuration returning None
    # for an unusable file would silently discard every other setting the
    # owner saved, so this follows the drop-malformed rule wake_models uses.
    config.save_configuration(
        StellaSettings.from_saved(provider="ollama", model="keeper")
    )
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    raw[field] = value
    config.config_path().write_text(json.dumps(raw), encoding="utf-8")

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.model == "keeper"
    assert getattr(resolved, field) == expected


def test_an_environment_turn_budget_outranks_the_saved_one(monkeypatch):
    # The file is written first, so the saved number is genuinely on disk
    # before the override exists: one launch uses 12 and 30, and reading has
    # no part in rewriting what the owner saved.
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama",
            model="m",
            max_tool_calls=3,
            turn_time_limit_seconds=45.5,
        )
    )
    monkeypatch.setenv("STELLA_MAX_TOOL_CALLS", "12")
    monkeypatch.setenv("STELLA_TURN_TIME_LIMIT_SECONDS", "30")

    resolved = config.resolve_settings()
    saved = config.load_configuration()

    assert resolved is not None
    assert resolved.max_tool_calls == 12
    assert resolved.turn_time_limit_seconds == 30.0
    # Reading never rewrites what was saved: the override is for this launch.
    assert saved is not None
    assert saved["max_tool_calls"] == 3
    assert saved["turn_time_limit_seconds"] == 45.5


def test_an_unset_environment_leaves_the_saved_turn_budget_alone():
    # The trap this pins: the environment path always supplies a value for
    # every field it owns, so an absent variable used to overwrite the saved
    # budget with the shipped default at the next launch.
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama",
            model="m",
            max_tool_calls=3,
            turn_time_limit_seconds=45.5,
        )
    )

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.max_tool_calls == 3
    assert resolved.turn_time_limit_seconds == 45.5


@POSIX_MODES
def test_configuration_file_is_private():
    config.save_configuration(
        StellaSettings.from_saved(provider="ollama", model="m")
    )
    mode = stat.S_IMODE(os.stat(config.config_path()).st_mode)
    assert mode == 0o600


def test_a_failed_save_leaves_the_previous_configuration_intact(monkeypatch):
    # A non-atomic write truncated config.json before writing, so a crash
    # or full disk mid-write could silently wipe every saved setting
    # (load_configuration treats an unparseable file as "absent"). The
    # atomic replace must leave the previous good file whole.
    config.save_configuration(
        StellaSettings.from_saved(provider="ollama", model="keeper")
    )

    class _FailingHandle:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def write(self, _data):
            raise OSError("disk full")

    def fake_fdopen(descriptor, *_args, **_kwargs):
        os.close(descriptor)  # do not leak the mkstemp descriptor
        return _FailingHandle()

    monkeypatch.setattr(config.os, "fdopen", fake_fdopen)
    with pytest.raises(OSError):
        config.save_configuration(
            StellaSettings.from_saved(provider="ollama", model="replacement")
        )

    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.model == "keeper"
    # The temp file is cleaned up, not left beside the config.
    assert not list(config.config_path().parent.glob(".config.json.*.tmp"))


def test_preset_roundtrips_through_the_saved_configuration():
    config.save_configuration(
        StellaSettings.from_saved(
            provider="openai",
            model="claude-sonnet-4",
            preset="anthropic",
        )
    )
    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.preset == "anthropic"


def test_legacy_configuration_without_a_preset_resolves_to_none():
    config.save_configuration(
        StellaSettings.from_saved(provider="openai", model="m")
    )
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    del raw["preset"]
    config.config_path().write_text(json.dumps(raw), encoding="utf-8")
    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.preset is None


def test_a_corrupt_preset_is_inert_not_fatal():
    config.save_configuration(
        StellaSettings.from_saved(provider="openai", model="m")
    )
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    raw["preset"] = 42
    config.config_path().write_text(json.dumps(raw), encoding="utf-8")
    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.preset is None


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        json.dumps(["a", "list"]),
        json.dumps({"provider": "anthropic", "model": "claude"}),
        json.dumps({"provider": "ollama", "model": "   "}),
        json.dumps({"model": "qwen3:4b"}),
    ],
)
def test_unusable_configuration_files_are_rejected(body):
    config.config_path().parent.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text(body, encoding="utf-8")
    assert config.load_configuration() is None
    assert config.resolve_settings() is None


# -------------------------------------------------------- precedence


def test_environment_model_wins_over_saved_configuration(
    monkeypatch: pytest.MonkeyPatch,
):
    config.save_configuration(
        StellaSettings.from_saved(provider="ollama", model="saved-model")
    )
    monkeypatch.setenv("STELLA_MODEL", "env-model")
    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.model == "env-model"


def test_endpoint_environment_overrides_saved_endpoint(
    monkeypatch: pytest.MonkeyPatch,
):
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama", model="m", ollama_base_url="http://saved:11434/v1"
        )
    )
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://override:11434/v1")
    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.ollama_base_url == "http://override:11434/v1"


def test_saved_openai_base_url_is_respected():
    config.save_configuration(
        StellaSettings.from_saved(
            provider="openai", model="m", openai_base_url="https://gw.example/v1"
        )
    )
    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.openai_base_url == "https://gw.example/v1"


# ------------------------------------------------------------ probes


def test_tags_url_is_derived_like_the_native_chat_url():
    assert (
        config.ollama_tags_url("http://127.0.0.1:11434/v1")
        == "http://127.0.0.1:11434/api/tags"
    )
    assert (
        config.ollama_tags_url("http://host:11434/")
        == "http://host:11434/api/tags"
    )


def test_scan_discovers_multiple_installed_models(monkeypatch):
    body = json.dumps(
        {"models": [{"name": "qwen3:4b"}, {"name": "llama3.2:latest"}]}
    ).encode("utf-8")
    monkeypatch.setattr(config.urllib.request, "urlopen", _fake_urlopen(body))
    scan = config.scan_ollama_models("http://127.0.0.1:11434/v1")
    assert scan.reachable
    assert scan.models == ("qwen3:4b", "llama3.2:latest")


def test_scan_reports_unreachable_ollama(monkeypatch):
    monkeypatch.setattr(
        config.urllib.request,
        "urlopen",
        _raising_urlopen(urllib.error.URLError("connection refused")),
    )
    scan = config.scan_ollama_models("http://127.0.0.1:1/v1")
    assert not scan.reachable
    assert "ollama serve" in scan.message


def test_scan_reports_http_errors(monkeypatch):
    error = urllib.error.HTTPError(
        "http://x/api/tags", 500, "boom", hdrs=None, fp=None
    )
    monkeypatch.setattr(config.urllib.request, "urlopen", _raising_urlopen(error))
    scan = config.scan_ollama_models("http://x/v1")
    assert not scan.models
    assert "error 500" in scan.message


def test_scan_rejects_invalid_json(monkeypatch):
    monkeypatch.setattr(config.urllib.request, "urlopen", _fake_urlopen(b"<html>"))
    scan = config.scan_ollama_models("http://x/v1")
    assert scan.reachable
    assert not scan.models
    assert "could not read" in scan.message


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"models": "not-a-list"}),
        json.dumps({"models": [[1, 2]]}),
        json.dumps({"models": [{"name": "  "}]}),
        json.dumps({"models": [{"model": "missing-name"}]}),
    ],
)
def test_scan_rejects_malformed_payload_shapes(monkeypatch, payload):
    monkeypatch.setattr(
        config.urllib.request, "urlopen", _fake_urlopen(payload.encode("utf-8"))
    )
    scan = config.scan_ollama_models("http://x/v1")
    assert scan.reachable
    assert not scan.models
    assert "unexpected model list" in scan.message


def test_scan_empty_list_explains_install_without_downloading(monkeypatch):
    body = json.dumps({"models": []}).encode("utf-8")
    monkeypatch.setattr(config.urllib.request, "urlopen", _fake_urlopen(body))
    scan = config.scan_ollama_models("http://x/v1")
    assert scan.reachable
    assert scan.models == ()
    assert "ollama pull" in scan.message


# ------------------------------------------------- what a pasted key costs


def _recording_urlopen(body: bytes = b'{"data": []}'):
    """A transport that keeps the requests it was handed."""

    seen: list = []

    def open(request, timeout=None):
        seen.append(request)
        return _Response(body)

    open.seen = seen
    return open


def test_a_pasted_key_is_sent_trimmed_by_the_model_scan(monkeypatch):
    # A token copied out of a terminal usually carries the newline it ended
    # on. save_api_key strips before storing, so the probe was judging a
    # different string from the one the store keeps — and a router that
    # refused the whitespace reported a perfectly good key as rejected.
    opener = _recording_urlopen(json.dumps({"data": [{"id": "m"}]}).encode())
    monkeypatch.setattr(config.urllib.request, "urlopen", opener)
    config.scan_openai_models("http://localhost:3001/v1", "freellmapi-token\n")
    assert opener.seen[0].get_header("Authorization") == (
        "Bearer freellmapi-token"
    )


def test_the_key_probe_judges_the_bytes_the_store_will_keep(monkeypatch):
    opener = _recording_urlopen()
    monkeypatch.setattr(config.urllib.request, "urlopen", opener)
    check = config.check_api_key(
        "http://localhost:3001/v1", " freellmapi-token \n"
    )
    assert check.state == "verified"
    assert opener.seen[0].get_header("Authorization") == (
        "Bearer freellmapi-token"
    )


def test_a_key_that_is_only_whitespace_is_absent_and_not_sent(monkeypatch):
    # Trimming happens before the guard, so an empty paste cannot become a
    # request carrying a bare "Bearer ".
    opener = _recording_urlopen()
    monkeypatch.setattr(config.urllib.request, "urlopen", opener)
    check = config.check_api_key("http://localhost:3001/v1", "  \n")
    assert check.state == "inconclusive"
    assert opener.seen == []


# --------------------------------------------------- connection tests


def test_connection_test_ollama_success(monkeypatch):
    body = json.dumps({"models": [{"name": "qwen3:4b"}]}).encode("utf-8")
    monkeypatch.setattr(config.urllib.request, "urlopen", _fake_urlopen(body))
    result = config.test_connection(
        provider="ollama", model="qwen3:4b", ollama_base_url="http://x/v1"
    )
    assert result.ok
    assert "installed" in result.message


def test_connection_test_detects_removed_model(monkeypatch):
    body = json.dumps({"models": [{"name": "other:1"}]}).encode("utf-8")
    monkeypatch.setattr(config.urllib.request, "urlopen", _fake_urlopen(body))
    result = config.test_connection(
        provider="ollama", model="qwen3:4b", ollama_base_url="http://x/v1"
    )
    assert not result.ok
    assert "ollama pull qwen3:4b" in result.message


def test_connection_test_requires_a_model():
    result = config.test_connection(provider="ollama", model="  ")
    assert not result.ok
    assert "No model" in result.message


def test_connection_test_llama_needs_a_real_gguf_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda _: "/bin/llama-server")
    result = config.test_connection(
        provider="llama", model=str(tmp_path / "missing.gguf")
    )
    assert not result.ok
    assert "No GGUF model file" in result.message


def test_connection_test_llama_needs_the_server_command(tmp_path, monkeypatch):
    model = tmp_path / "brain.gguf"
    model.write_bytes(b"not really a gguf")
    monkeypatch.setattr(config.shutil, "which", lambda _: None)
    result = config.test_connection(provider="llama", model=str(model))
    assert not result.ok
    assert "STELLA_LLAMA_SERVER_BINARY" in result.message


def test_connection_test_llama_reports_launch_readiness(
    tmp_path, monkeypatch
):
    model = tmp_path / "brain.gguf"
    model.write_bytes(b"not really a gguf")
    monkeypatch.setattr(
        config.shutil, "which", lambda _: "/bin/llama-server"
    )
    result = config.test_connection(provider="llama", model=str(model))
    assert result.ok
    assert "Ready to launch" in result.message


def test_saved_configuration_accepts_the_llama_provider(tmp_path):
    settings = StellaSettings.from_saved(
        provider="llama", model=str(tmp_path / "brain.gguf")
    )
    config.save_configuration(settings)
    raw = config.load_configuration()
    assert raw is not None
    assert raw["provider"] == "llama"
    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.provider == "llama"


def test_connection_test_openai_without_key(monkeypatch):
    result = config.test_connection(
        provider="openai", model="gpt-4o-mini", api_key=None
    )
    assert not result.ok
    assert "No API key" in result.message


class _FakeModels:
    def __init__(self, error: Exception | None) -> None:
        self._error = error

    def list(self):
        if self._error is not None:
            raise self._error
        return []


def _fake_openai(error: Exception | None):
    class FakeOpenAI:
        def __init__(self, *, api_key, base_url=None, timeout=None):
            self.api_key = api_key
            self.base_url = base_url
            self.models = _FakeModels(error)

    return FakeOpenAI


def test_connection_test_openai_success_never_uses_key_in_message(monkeypatch):
    monkeypatch.setattr("openai.OpenAI", _fake_openai(None))
    result = config.test_connection(
        provider="openai",
        model="gpt-4o-mini",
        api_key="sk-super-secret-value",
        openai_base_url="https://api.example.com/v1",
    )
    assert result.ok
    assert "sk-super-secret-value" not in result.message


def test_connection_test_openai_reports_rejected_key(monkeypatch):
    class AuthError(Exception):
        status_code = 401

    monkeypatch.setattr("openai.OpenAI", _fake_openai(AuthError()))
    result = config.test_connection(
        provider="openai", model="m", api_key="sk-super-secret-value"
    )
    assert not result.ok
    assert "rejected" in result.message
    assert "sk-super-secret-value" not in result.message


def test_the_connection_test_authenticates_with_the_trimmed_key(monkeypatch):
    # The other half of the paste bug: the client the dialog builds must
    # carry the same bytes save_api_key would keep, or "Test connection"
    # fails on a key that then works on the next launch.
    built: dict = {}

    class Capturing(_fake_openai(None)):
        def __init__(self, *, api_key, base_url=None, timeout=None):
            super().__init__(api_key=api_key, base_url=base_url, timeout=timeout)
            built["api_key"] = api_key

    monkeypatch.setattr("openai.OpenAI", Capturing)
    result = config.test_connection(
        provider="openai", model="m", api_key="sk-pasted-value\n"
    )
    assert result.ok
    assert built["api_key"] == "sk-pasted-value"


def test_a_fixed_preset_is_tested_at_its_own_endpoint(monkeypatch):
    # The dialog sends no base URL for a preset that carries one, because
    # the endpoint belongs to the preset and not to the saved settings. The
    # probe used to hand that None straight to the SDK, whose default is
    # api.openai.com — so a local router key was "rejected" by a server the
    # user never chose, while the same key worked from the terminal.
    built: dict = {}

    class Capturing(_fake_openai(None)):
        def __init__(self, *, api_key, base_url=None, timeout=None):
            super().__init__(
                api_key=api_key, base_url=base_url, timeout=timeout
            )
            built["base_url"] = base_url

    monkeypatch.setattr("openai.OpenAI", Capturing)
    result = config.test_connection(
        provider="openai",
        model="auto",
        api_key="freellmapi-a-local-token",
        preset="freellmapi",
    )
    assert result.ok
    assert built["base_url"] == "http://localhost:3001/v1"


def test_connection_test_sanitizes_unknown_errors(monkeypatch):
    error = RuntimeError(
        "upstream said: authorization: Bearer sk-super-secret-value failed"
    )
    monkeypatch.setattr("openai.OpenAI", _fake_openai(error))
    result = config.test_connection(
        provider="openai", model="m", api_key="sk-super-secret-value"
    )
    assert not result.ok
    assert "sk-super-secret-value" not in result.message
    assert "[redacted]" in result.message
    assert len(result.message) <= 200


def test_connection_test_reports_unreachable_endpoints(monkeypatch):
    class ConnectionError_(Exception):
        pass

    ConnectionError_.__name__ = "APIConnectionError"
    monkeypatch.setattr("openai.OpenAI", _fake_openai(ConnectionError_()))
    result = config.test_connection(provider="openai", model="m", api_key="k")
    assert not result.ok
    assert "could not be reached" in result.message


# -------------------------------------------- which address a turn reaches


def test_the_endpoint_resolver_names_the_server_a_turn_would_reach():
    # One function, three cases, in the order the runtime applies them. The
    # point is that a None base URL is not "no endpoint": it is the SDK's own
    # default, and a probe that asks a different address is a green line about
    # a server no turn will ever be sent to.
    assert config.openai_endpoint("openrouter") == "https://openrouter.ai/api/v1"
    assert config.openai_endpoint("freellmapi") == "http://localhost:3001/v1"
    assert (
        config.openai_endpoint("openrouter", "http://127.0.0.1:9/v1")
        == "http://127.0.0.1:9/v1"
    )
    # A preset with no address of its own — the legacy OpenAI slot and the
    # hand-typed custom slot — really is the SDK default.
    assert config.openai_endpoint(None) == config.DEFAULT_OPENAI_BASE_URL
    assert config.openai_endpoint("custom") == config.DEFAULT_OPENAI_BASE_URL


def test_the_launch_sweep_never_borrows_the_default_for_another_key(
    monkeypatch,
):
    # The deliberate asymmetry between the resolver above and the startup
    # sweep: the sweep walks stored keys with no active settings to resolve
    # against, so only the slot the default actually belongs to is asked.
    # Treating every keyless-address preset as api.openai.com would nag on a
    # perfectly valid FreeLLMAPI token every single launch.
    asked: list[str] = []

    def fake(endpoint, _key, _timeout=None, **_kw):
        asked.append(endpoint)
        return config.KeyCheck("verified", "the endpoint accepted the key")

    monkeypatch.setattr(config, "check_api_key", fake)
    monkeypatch.setattr(provider_keys, "effective_api_key", lambda _pid: "sk-x")
    monkeypatch.setattr(
        provider_keys, "stored_presets", lambda: ("custom", "openai")
    )
    assert config.startup_key_health_report() == []
    assert asked == [config.DEFAULT_OPENAI_BASE_URL]


# ----------------------------------------------- key resolution & gates


def _no_openai_client(*_args, **_kwargs):
    raise AssertionError("no client may be built for a mismatched key")


def test_a_mismatched_key_is_refused_before_any_request(monkeypatch):
    # The weird case the user asked about: OpenAI selected, a Claude key
    # pasted. The gate corrects offline — no client, no network call.
    monkeypatch.setattr("openai.OpenAI", _no_openai_client)
    result = config.test_connection(
        provider="openai",
        model="m",
        api_key="sk-ant-api03-super-secret",
        preset="openai",
    )
    assert not result.ok
    assert "Claude (Anthropic)" in result.message
    assert "sk-ant" not in result.message


def test_a_stored_key_is_used_when_none_is_entered(monkeypatch):
    provider_keys.save_api_key("anthropic", "sk-ant-stored-value")
    monkeypatch.setattr("openai.OpenAI", _fake_openai(None))
    result = config.test_connection(
        provider="openai", model="m", api_key=None, preset="anthropic"
    )
    assert result.ok
    assert "sk-ant-stored-value" not in result.message


class _RecordingChat:
    def __init__(self, error: Exception | None) -> None:
        self.calls: list[dict] = []
        self._error = error
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(choices=[])


class NotFoundInFake(Exception):
    """Named like the SDK's NotFound so type-name detection fires."""


def test_a_provider_without_a_models_endpoint_gets_a_chat_probe(monkeypatch):
    # Anthropic-style endpoints may 404 on /models; the fallback is one
    # bounded single-token chat completion, which still authenticates.
    created: list = []

    class FakeOpenAI:
        def __init__(self, *, api_key, base_url=None, timeout=None):
            self.models = _FakeModels(NotFoundInFake())
            self.chat = _RecordingChat(None)
            created.append(self)

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    result = config.test_connection(
        provider="openai",
        model="claude-sonnet-4-20250514",
        api_key="sk-ant-key",
        preset="anthropic",
    )
    assert result.ok
    assert len(created) == 1
    probe = created[0].chat
    assert len(probe.calls) == 1
    assert probe.calls[0]["max_tokens"] == 1
    assert probe.calls[0]["model"] == "claude-sonnet-4-20250514"


def test_the_openai_preset_never_falls_back_to_a_chat_probe(monkeypatch):
    # OpenAI itself always serves /models; a 404 there is a real problem
    # to report, not something to probe around.
    created: list = []

    class FakeOpenAI:
        def __init__(self, *, api_key, base_url=None, timeout=None):
            self.models = _FakeModels(NotFoundInFake())
            self.chat = _RecordingChat(AssertionError("must not probe"))
            created.append(self)

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    result = config.test_connection(
        provider="openai", model="gpt-4o-mini", api_key="sk-key", preset="openai"
    )
    assert not result.ok
    assert created[0].chat.calls == []


# ----------------------------------------------------------- sanitize


def test_sanitize_redacts_secrets_and_bounds_length():
    long_text = "x" * 500
    assert config.sanitize(long_text) == "x" * 160
    assert config.sanitize("key is sk-abc123", ("sk-abc123",)) == "key is [redacted]"
    assert config.sanitize("  collapse   whitespace  ") == "collapse whitespace"


def test_saved_configuration_roundtrips_the_os_tools_opt_in():
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama", model="m", os_tools_enabled=True
        )
    )
    raw = config.load_configuration()
    assert raw is not None
    assert raw["os_tools_enabled"] is True
    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.os_tools_enabled is True


def test_environment_toggle_overrides_the_saved_os_tools_choice(monkeypatch):
    monkeypatch.setenv("STELLA_OS_TOOLS", "0")
    settings = StellaSettings.from_saved(
        provider="ollama", model="m", os_tools_enabled=True
    )
    assert settings.os_tools_enabled is False


def test_desktop_awareness_is_on_by_default():
    # "Desktop awareness should be by default checked." A fresh setup and
    # a bare settings object both start on; an explicit False still wins.
    assert StellaSettings(model="m").os_tools_enabled is True
    assert (
        StellaSettings.from_saved(provider="ollama", model="m").os_tools_enabled
        is True
    )
    off = StellaSettings.from_saved(
        provider="ollama", model="m", os_tools_enabled=False
    )
    assert off.os_tools_enabled is False


def test_desktop_awareness_is_on_by_default_in_the_environment_path(
    monkeypatch,
):
    # Parity with the saved/desktop path: a pure-environment run also
    # starts desktop awareness ON (still double-gated at build time), and
    # an explicit STELLA_OS_TOOLS=0 turns it off.
    monkeypatch.setenv("STELLA_MODEL", "m")
    monkeypatch.delenv("STELLA_OS_TOOLS", raising=False)
    assert StellaSettings.from_environment().os_tools_enabled is True
    monkeypatch.setenv("STELLA_OS_TOOLS", "0")
    assert StellaSettings.from_environment().os_tools_enabled is False


def test_wake_word_is_on_by_default_in_the_saved_config(monkeypatch):
    # The desktop UI now starts listening for its name unless the owner
    # unticks the box: from_saved's default arms wake (mode "on"). The bare
    # dataclass keeps its historical conservative off, so the headless and
    # environment paths are unaffected by this flip. An explicit False is
    # still honoured.
    monkeypatch.delenv("STELLA_WAKE_WORD", raising=False)
    assert StellaSettings(model="m").wake_word_enabled is False
    on = StellaSettings.from_saved(provider="ollama", model="m")
    assert on.wake_word_enabled is True
    assert on.wake_word == "on"
    off = StellaSettings.from_saved(
        provider="ollama", model="m", wake_word_enabled=False
    )
    assert off.wake_word_enabled is False
    assert off.wake_word == "off"


def test_a_legacy_config_without_the_wake_key_resolves_on(monkeypatch):
    # A configuration written before wake had a checkbox carries no
    # "wake_word_enabled" field at all. resolve_settings now reads that
    # absence as ON so the desktop defaults to listening; the unticked
    # opt-out persists an explicit false, which still resolves OFF.
    monkeypatch.delenv("STELLA_WAKE_WORD", raising=False)
    config.config_path().parent.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text(
        json.dumps({"provider": "ollama", "model": "m"}), encoding="utf-8"
    )
    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.wake_word_enabled is True
    assert resolved.wake_word == "on"
    config.config_path().write_text(
        json.dumps(
            {"provider": "ollama", "model": "m", "wake_word_enabled": False}
        ),
        encoding="utf-8",
    )
    resolved = config.resolve_settings()
    assert resolved is not None
    assert resolved.wake_word_enabled is False
    assert resolved.wake_word == "off"


# ------------------------------------------------- startup key-health sweep


def test_startup_key_health_report_shows_only_connection_errors(monkeypatch):
    # A healthy key is silent; only a rejected or unreachable endpoint is
    # turned into a line — and the raw key never appears, only the redacted
    # last-four hint the panel would show.
    sentinel = "sk-SENTINELSECRETVALUE-99"
    monkeypatch.setattr(provider_keys, "stored_presets", lambda: ("openai",))
    monkeypatch.setattr(provider_keys, "effective_api_key", lambda pid: sentinel)

    def _fake(_endpoint, _key, _timeout=None, **_kw):
        return config.KeyCheck(state, "HTTP 401")

    for state, expect in (
        ("rejected", True),
        ("unreachable", True),
        ("verified", False),
        ("inconclusive", False),
    ):
        monkeypatch.setattr(config, "check_api_key", _fake)
        lines = config.startup_key_health_report()
        if expect:
            assert len(lines) == 1
            assert "OpenAI" in lines[0]
            assert sentinel not in lines[0]
        else:
            assert lines == []


def test_startup_key_health_report_skips_unprobeable_presets(monkeypatch):
    # custom (no endpoint until a URL is known) and the local servers take
    # nothing to probe; a preset with no stored key is skipped too. None of
    # them may even reach the network.
    calls: list[str] = []

    def _fake(endpoint, _key, _timeout=None, **_kw):
        calls.append(endpoint)
        return config.KeyCheck("rejected", "HTTP 401")

    monkeypatch.setattr(config, "check_api_key", _fake)
    monkeypatch.setattr(provider_keys, "effective_api_key", lambda pid: "sk-x")
    monkeypatch.setattr(
        provider_keys, "stored_presets", lambda: ("custom", "ollama")
    )
    assert config.startup_key_health_report() == []
    assert calls == []

    monkeypatch.setattr(provider_keys, "stored_presets", lambda: ("openai",))
    monkeypatch.setattr(provider_keys, "effective_api_key", lambda pid: None)
    assert config.startup_key_health_report() == []
    assert calls == []


def test_startup_key_health_report_does_not_false_reject_anthropic(monkeypatch):
    # A Bearer /models call against Anthropic 401s a perfectly good key
    # (it authenticates with x-api-key). The sweep probes it as a chat
    # dialect, so check_api_key returns inconclusive and the launch stays
    # silent — the OpenAI preset, by contrast, is probed as responses.
    seen: dict[str, bool] = {}

    def _fake(_endpoint, _key, _timeout=None, *, chat_dialect=False):
        seen["chat_dialect"] = chat_dialect
        return config.KeyCheck("inconclusive", "this endpoint guards its list")

    monkeypatch.setattr(config, "check_api_key", _fake)
    monkeypatch.setattr(provider_keys, "effective_api_key", lambda pid: "sk-ant-x")

    monkeypatch.setattr(provider_keys, "stored_presets", lambda: ("anthropic",))
    assert config.startup_key_health_report() == []
    assert seen["chat_dialect"] is True

    monkeypatch.setattr(provider_keys, "stored_presets", lambda: ("openai",))
    assert config.startup_key_health_report() == []
    assert seen["chat_dialect"] is False


def test_saved_voice_switches_roundtrip():
    # The two switches the owner used to be able to reach only by exporting
    # an environment variable: what Settings saves comes back out of the
    # file, and the file holds the plain shapes and nothing else.
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama",
            model="m",
            speech_resident=True,
            voice_barge_in="on",
        )
    )
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))

    assert raw["speech_resident"] is True
    assert raw["voice_barge_in"] == "on"

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.speech_resident is True
    assert resolved.voice_barge_in == "on"


def test_a_configuration_older_than_the_voice_switches_still_resolves():
    # A file written before these two existed carries neither key, and the
    # answer is the shipped behaviour — worker as asked for, ear armed only
    # where a microphone was chosen — not a rejected file.
    config.save_configuration(
        StellaSettings.from_saved(provider="ollama", model="keeper")
    )
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    del raw["speech_resident"]
    del raw["voice_barge_in"]
    config.config_path().write_text(json.dumps(raw), encoding="utf-8")

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.model == "keeper"
    assert resolved.speech_resident is False
    assert resolved.voice_barge_in == "auto"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("maybe", "auto"),
        ("", "auto"),
        ("   ", "auto"),
        (None, "auto"),
        (True, "auto"),
        (7, "auto"),
        (["on"], "auto"),
        ({"mode": "on"}, "auto"),
        # A mode is a name, spelled any way a hand-edit leaves it: case and
        # stray spaces are normalised rather than dropped, exactly as a saved
        # microphone node is trimmed.
        ("ON", "on"),
        (" off ", "off"),
        ("Auto", "auto"),
    ],
)
def test_a_saved_barge_mode_is_read_as_a_name_not_an_invention(
    value: object, expected: str
):
    config.save_configuration(
        StellaSettings.from_saved(provider="ollama", model="keeper")
    )
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    raw["voice_barge_in"] = value
    config.config_path().write_text(json.dumps(raw), encoding="utf-8")

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.model == "keeper"
    assert resolved.voice_barge_in == expected


@pytest.mark.parametrize("value", [None, "", "true", "True", 1, 1.0, ["on"]])
def test_only_a_real_boolean_wakes_the_resident_worker(value: object):
    # The saved half of this switch is written by a checkbox, so it is a
    # JSON bool. `"true"` and `1` are the *environment* spellings and are
    # deliberately not accepted here: a hand-edit this file cannot read as a
    # bool leaves the worker as asked for, which is the shape that cannot
    # misfire. A real `true` is proven by the round-trip above.
    config.save_configuration(
        StellaSettings.from_saved(provider="ollama", model="keeper")
    )
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    raw["speech_resident"] = value
    config.config_path().write_text(json.dumps(raw), encoding="utf-8")

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.model == "keeper"
    assert resolved.speech_resident is False


def test_the_environment_outranks_a_saved_voice_switch_in_both_directions(
    monkeypatch: pytest.MonkeyPatch,
):
    # The saved answer is what the machine does by default; one launch may
    # say otherwise without rewriting it — including saying the opposite.
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama",
            model="m",
            speech_resident=True,
            voice_barge_in="on",
        )
    )
    monkeypatch.setenv("STELLA_SPEECH_RESIDENT", "0")
    monkeypatch.setenv("STELLA_VOICE_BARGE_IN", "off")

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.speech_resident is False
    assert resolved.voice_barge_in == "off"
    # Saving is untouched by the override: the file still says what the
    # owner chose in Settings.
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert raw["speech_resident"] is True
    assert raw["voice_barge_in"] == "on"

    monkeypatch.setenv("STELLA_SPEECH_RESIDENT", "1")
    monkeypatch.setenv("STELLA_VOICE_BARGE_IN", "auto")
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama", model="m", voice_barge_in="off"
        )
    )
    resolved = config.resolve_settings()
    assert resolved.speech_resident is True
    assert resolved.voice_barge_in == "auto"


def test_saving_the_gate_off_is_the_way_it_comes_back_on():
    # The one shipped-on voice switch, so the round trip is the whole story:
    # a checkbox off in Settings is a `false` in the file and an ungated
    # capture, and nothing is quietly restored to the default.
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama", model="m", voice_capture_gate=False
        )
    )
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))

    assert raw["voice_capture_gate"] is False

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.voice_capture_gate is False


@pytest.mark.parametrize("value", [None, "", "false", "False", 0, 0.0, ["off"]])
def test_only_a_saved_no_switches_the_gate_off(value: object):
    # Read the opposite way round from the resident worker, because this
    # default is on: a hand-edit that is not a JSON `false` is not the owner
    # saying no, so the capture stays gated. `0` and `"0"` are the
    # *environment* spellings and are deliberately not accepted here.
    config.save_configuration(
        StellaSettings.from_saved(provider="ollama", model="keeper")
    )
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    raw["voice_capture_gate"] = value
    config.config_path().write_text(json.dumps(raw), encoding="utf-8")

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.model == "keeper"
    assert resolved.voice_capture_gate is True


def test_the_environment_outranks_a_saved_gate_in_both_directions(
    monkeypatch: pytest.MonkeyPatch,
):
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama", model="m", voice_capture_gate=False
        )
    )
    monkeypatch.setenv("STELLA_VOICE_CAPTURE_GATE", "1")

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.voice_capture_gate is True
    # And the saved no is still the saved answer once the run is over.
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert raw["voice_capture_gate"] is False

    monkeypatch.setenv("STELLA_VOICE_CAPTURE_GATE", "0")
    config.save_configuration(
        StellaSettings.from_saved(provider="ollama", model="m")
    )

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.voice_capture_gate is False


def test_a_saved_detector_path_survives_the_config_roundtrip():
    # The gate reads its model from exactly this string, so a machine that
    # keeps silero_vad.onnx somewhere else has to be able to say it once. A
    # desktop launch runs no shell profile, which is the whole reason the path
    # is a saved setting rather than the variable it used to be.
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama",
            model="m",
            vad_model="/elsewhere/models/silero_vad.onnx",
        )
    )

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.vad_model == "/elsewhere/models/silero_vad.onnx"
    # Whitespace a hand-edit left behind is trimmed, not trusted verbatim into
    # the argument of a model loader.
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    raw["vad_model"] = "  /elsewhere/other_vad.onnx  "
    config.config_path().write_text(json.dumps(raw), encoding="utf-8")
    assert config.resolve_settings().vad_model == "/elsewhere/other_vad.onnx"


@pytest.mark.parametrize("value", [None, "", "   ", 7, ["a"], True])
def test_an_unreadable_detector_path_is_dropped_not_refused(value: object):
    # Every other setting the owner saved is worth more than one bad string,
    # so a value this file cannot read falls back to the usual place instead of
    # rejecting the configuration — the same rule wake_source follows.
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama",
            model="m",
            vad_model="/elsewhere/silero_vad.onnx",
        )
    )
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    raw["vad_model"] = value
    config.config_path().write_text(json.dumps(raw), encoding="utf-8")

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.model == "m"
    assert resolved.vad_model == default_vad_model()


def test_an_explicit_detector_path_outranks_the_saved_one_for_one_launch(
    monkeypatch: pytest.MonkeyPatch,
):
    config.save_configuration(
        StellaSettings.from_saved(
            provider="ollama", model="m", vad_model="/saved/silero_vad.onnx"
        )
    )
    monkeypatch.setenv("STELLA_VAD_MODEL", "  /this/launch.onnx  ")

    resolved = config.resolve_settings()

    assert resolved is not None
    assert resolved.vad_model == "/this/launch.onnx"
    # And the path the owner wrote down is still what the next launch uses.
    raw = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert raw["vad_model"] == "/saved/silero_vad.onnx"
