"""Tests for the first-run configuration and bounded provider probes."""

import json
import os
import stat
import urllib.error
from types import SimpleNamespace

import pytest

from stella import config, provider_keys
from stella.app import StellaSettings


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Never read or write the developer's real configuration in tests."""

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.delenv("STELLA_MODEL", raising=False)
    monkeypatch.delenv("STELLA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


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
    }
    assert raw["openai_base_url"] == "https://gw.example/v1"
    for secret_word in ("api_key", "sk-", "key"):
        assert secret_word not in text


def test_configuration_file_is_private():
    config.save_configuration(
        StellaSettings.from_saved(provider="ollama", model="m")
    )
    mode = stat.S_IMODE(os.stat(config.config_path()).st_mode)
    assert mode == 0o600


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
