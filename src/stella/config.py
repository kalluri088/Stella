"""Saved first-run configuration and bounded provider probes.

This module owns everything a user needs *before* a Stella application
exists: a small JSON file under the XDG data directory that records the
chosen provider/model/endpoints, and read-only probes that answer
"is this reachable?" and "which models are installed?".

Security rules this module upholds:
- API keys are never written to the config file and never returned by
  any probe. They live only in the private store owned by
  ``stella.provider_keys`` (``api_keys.json``, mode 0600); this module
  reads through that store's precedence rules and never persists.
- Every probe returns a bounded, sanitized message; exception text is
  scrubbed of any secret the caller passed in before it is shown.
- Model names and provider responses are data. Nothing here constructs
  decisions, tools, or approvals.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from stella import provider_keys
from stella.app import StellaSettings, default_data_dir
from stella.llama_server import DEFAULT_LLAMA_SERVER_BINARY
from stella.ollama_client import DEFAULT_OLLAMA_BASE_URL
from stella.portable import harden_private_file

PROBE_TIMEOUT_SECONDS = 5.0
_MESSAGE_LIMIT = 160

_CONFIG_FIELDS = (
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
    "shell_tools_enabled",
    "browser_tools_enabled",
    "wake_word_enabled",
)


def config_path() -> Path:
    return default_data_dir() / "config.json"


def save_configuration(settings: StellaSettings) -> None:
    """Persist only the non-secret provider/preset/model/endpoint fields.

    API keys and paths to state are deliberately never stored; keys have
    their own private file, owned by ``stella.provider_keys``.
    """

    payload = {
        field: getattr(settings, field) for field in _CONFIG_FIELDS
    }
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace: a crash or a full disk mid-write must never leave a
    # truncated config.json, because load_configuration treats an
    # unparseable file as "absent" and silently drops every saved setting.
    # mkstemp creates the temp 0600, so it is private before the rename.
    descriptor, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    # Owner-only, and reported rather than assumed: on POSIX this is the
    # real 0o600 mode bit (a failure raises, as it always did); on
    # Windows an icacls grant is attempted and its outcome is returned
    # instead of implied. Nothing here claims privacy it did not get.
    harden_private_file(path)


def load_configuration() -> dict[str, object] | None:
    """Return the saved config dict, or None when absent or unusable."""

    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    provider = raw.get("provider")
    model = raw.get("model")
    if (
        not isinstance(provider, str)
        or provider not in {"ollama", "openai", "llama"}
        or not isinstance(model, str)
        or not model.strip()
    ):
        return None
    preset = raw.get("preset")
    if preset is not None and not isinstance(preset, str):
        # A corrupt preset is inert, not fatal: it only means "no stored
        # key found for a slot", which the friendly key error handles.
        raw["preset"] = None
    return raw


def sanitize(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Bound a message and replace any secret value it may contain."""

    cleaned = " ".join(str(text).split())
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned[:_MESSAGE_LIMIT]


@dataclass(frozen=True)
class ModelScan:
    """Outcome of asking Ollama which models are installed."""

    reachable: bool
    models: tuple[str, ...]
    message: str


def ollama_tags_url(base_url: str) -> str:
    trimmed = base_url.rstrip("/")
    return f"{trimmed.removesuffix('/v1')}/api/tags"


def scan_ollama_models(
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ModelScan:
    """Query Ollama's real model list. Never downloads anything."""

    request = urllib.request.Request(ollama_tags_url(base_url), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return ModelScan(
            reachable=True,
            models=(),
            message=(
                f"Ollama at {base_url} replied with error {error.code}."
            ),
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        return ModelScan(
            reachable=False,
            models=(),
            message=(
                f"Ollama is not reachable at {base_url}. Start it with "
                "`ollama serve`, then try again."
            ),
        )
    except json.JSONDecodeError:
        return ModelScan(
            reachable=True,
            models=(),
            message=(
                f"Ollama at {base_url} sent a response Stella could not "
                "read. Check the endpoint points at an Ollama server."
            ),
        )
    models = _model_names(payload)
    if models is None:
        return ModelScan(
            reachable=True,
            models=(),
            message=(
                f"Ollama at {base_url} sent an unexpected model list. "
                "Check the endpoint points at an Ollama server."
            ),
        )
    if not models:
        return ModelScan(
            reachable=True,
            models=(),
            message=(
                "Ollama is running but no models are installed. Install "
                "one first, for example: ollama pull qwen3:4b"
            ),
        )
    return ModelScan(
        reachable=True,
        models=models,
        message=f"Found {len(models)} installed model(s).",
    )


def scan_openai_models(
    base_url: str,
    api_key: str,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ModelScan:
    """Query an OpenAI-compatible /models endpoint. Never downloads anything.

    The base_url is expected to already carry the version path (for example
    ``http://localhost:3001/v1``); this appends ``/models`` to it.
    """

    trimmed = base_url.rstrip("/")
    url = f"{trimmed}/models"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return ModelScan(
            reachable=True,
            models=(),
            message=(
                f"{base_url} replied with error {error.code}. Check the "
                "API key and base URL."
            ),
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        return ModelScan(
            reachable=False,
            models=(),
            message=(
                f"The endpoint at {base_url} is not reachable. Start it, "
                "then try again."
            ),
        )
    except json.JSONDecodeError:
        return ModelScan(
            reachable=True,
            models=(),
            message=(
                f"{base_url} sent a response Stella could not read. Check "
                "the endpoint points at an OpenAI-compatible server."
            ),
        )
    models = _openai_model_names(payload)
    if models is None:
        return ModelScan(
            reachable=True,
            models=(),
            message=(
                f"{base_url} sent an unexpected model list. Check the "
                "endpoint points at an OpenAI-compatible server."
            ),
        )
    if not models:
        return ModelScan(
            reachable=True,
            models=(),
            message=f"{base_url} reported no models.",
        )
    return ModelScan(
        reachable=True,
        models=models,
        message=f"Found {len(models)} model(s) at this endpoint.",
    )


def _openai_model_names(payload: object) -> tuple[str, ...] | None:
    """Validate the OpenAI /models payload shape; None means malformed."""

    if not isinstance(payload, dict):
        return None
    entries = payload.get("data")
    if not isinstance(entries, list):
        return None
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        name = entry.get("id")
        if not isinstance(name, str) or not name.strip():
            return None
        names.append(name)
    return tuple(names)


@dataclass(frozen=True)
class KeyCheck:
    """Outcome of a lightweight "does this stored key authenticate?" probe.

    ``state`` is exactly one of:
      ``verified``      the endpoint reached and accepted the key;
      ``rejected``      the endpoint reached but refused the key (401/403);
      ``unreachable``   the endpoint could not be reached — this says
                        nothing about whether the key is valid;
      ``inconclusive``  reached, but this endpoint offers no way to judge a
                        key without running a completion.
    ``detail`` is bounded, safe text for the user and never carries the key.
    """

    state: str
    detail: str


def check_api_key(
    base_url: str,
    api_key: str,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    *,
    chat_dialect: bool = False,
) -> KeyCheck:
    """Authenticate a stored key against an OpenAI-compatible /models list.

    A plain GET /models with the Bearer key proves the endpoint accepts the
    key without running any completion, so it is cheap enough to fire when
    the user picks a provider. The three-way result keeps "the key is
    wrong" distinct from "the network is down" so the UI never shows an
    alarming error for a transient blip. The key is used only in the
    request header and is never returned or logged.

    For a chat-completions endpoint (``chat_dialect``) the model list is
    not a reliable place to judge a key: many routers guard or simply do
    not serve /models while accepting the same key on chat. So a 401/403
    there is reported as inconclusive — "use Test connection with a
    model", which does authenticate through chat — rather than a scary
    "key rejected" that is really just the router protecting its list.
    """
    if not base_url or not api_key:
        return KeyCheck("inconclusive", "no endpoint or key to check")
    trimmed = base_url.rstrip("/")
    url = f"{trimmed}/models"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            if chat_dialect:
                return KeyCheck(
                    "inconclusive",
                    "this endpoint guards its model list; check the key "
                    "with Test connection",
                )
            return KeyCheck("rejected", f"HTTP {error.code}")
        if error.code == 404:
            return KeyCheck("inconclusive", "no model list at this endpoint")
        return KeyCheck("unreachable", f"HTTP {error.code}")
    except (urllib.error.URLError, TimeoutError, OSError):
        return KeyCheck("unreachable", "the endpoint is not reachable")
    return KeyCheck("verified", "the endpoint accepted the key")


def _model_names(payload: object) -> tuple[str, ...] | None:
    """Validate the tags payload shape; None means malformed."""

    if not isinstance(payload, dict):
        return None
    entries = payload.get("models")
    if not isinstance(entries, list):
        return None
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        names.append(name)
    return tuple(names)


@dataclass(frozen=True)
class ConnectionTest:
    """Bounded result of a user-initiated 'Test connection'."""

    ok: bool
    message: str


def test_connection(
    *,
    provider: str,
    model: str,
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL,
    openai_base_url: str | None = None,
    api_key: str | None = None,
    preset: str | None = None,
    llama_binary: str = DEFAULT_LLAMA_SERVER_BINARY,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ConnectionTest:
    """Verify the chosen provider answers, without persisting anything.

    For Ollama this confirms the endpoint is alive and the model is in
    the installed list. For OpenAI-compatible endpoints it lists models,
    which authenticates the key without running any completion; an empty
    key field resolves through the store's precedence (environment, then
    the stored key for this preset). The llama provider has no server to
    probe before it exists (Stella owns and starts it), so the test
    checks the two things a launch needs.
    """

    model = model.strip()
    if not model:
        return ConnectionTest(ok=False, message="No model is selected yet.")
    if provider == "llama":
        if not os.path.isfile(model):
            return ConnectionTest(
                ok=False,
                message=(
                    f"No GGUF model file at {model}. For the llama "
                    "provider the model is the full path to a downloaded "
                    ".gguf file."
                ),
            )
        if shutil.which(llama_binary) is None:
            return ConnectionTest(
                ok=False,
                message=(
                    f"The {llama_binary} command was not found. Install "
                    "llama.cpp or set STELLA_LLAMA_SERVER_BINARY to its "
                    "full path."
                ),
            )
        return ConnectionTest(
            ok=True,
            message=(
                "Ready to launch: model file and llama-server were found. "
                "Stella starts the brain itself when these settings apply."
            ),
        )
    if provider == "ollama":
        scan = scan_ollama_models(ollama_base_url, timeout=timeout)
        if not scan.reachable:
            return ConnectionTest(ok=False, message=scan.message)
        if model in scan.models:
            return ConnectionTest(
                ok=True,
                message=f"Connected: Ollama reports {model} is installed.",
            )
        if not scan.models:
            return ConnectionTest(ok=False, message=scan.message)
        return ConnectionTest(
            ok=False,
            message=(
                f"Ollama is reachable but {model} is not installed. "
                f"Install it first: ollama pull {model}"
            ),
        )
    return _test_openai_connection(
        model=model,
        base_url=openai_base_url,
        api_key=api_key,
        preset=preset,
        timeout=timeout,
    )


def _test_openai_connection(
    *,
    model: str,
    base_url: str | None,
    api_key: str | None,
    timeout: float,
    preset: str | None = None,
) -> ConnectionTest:
    try:
        from openai import OpenAI
    except ImportError:  # pragma: no cover - installed via pyproject
        return ConnectionTest(
            ok=False,
            message="The OpenAI client library is not installed.",
        )
    key = api_key or provider_keys.effective_api_key(preset) or ""
    secrets = (key,) if key else ()
    if not key:
        return ConnectionTest(
            ok=False,
            message=(
                "No API key was entered or stored for this provider. "
                "Type one, or export OPENAI_API_KEY."
            ),
        )
    mismatch = provider_keys.mismatch_hint(preset, key)
    if mismatch:
        # Offline gate: a Claude key pasted into the OpenAI slot should
        # get a helpful correction, not a stranger's 401. No request is
        # made and no client is constructed.
        return ConnectionTest(ok=False, message=mismatch)
    client = OpenAI(api_key=key, base_url=base_url, timeout=timeout * 4)
    try:
        client.models.list()
    except Exception as error:  # noqa: BLE001 - bounded, sanitized below
        status = getattr(error, "status_code", None)
        missing_models = status == 404 or "NotFound" in type(error).__name__
        refused_models = status in (401, 403)
        chat_dialect = provider_keys.tool_dialect_for(preset) == "chat"
        # A chat-dialect router (FreeLLMAPI and friends) may guard or not
        # even serve /models while its chat endpoint happily takes the
        # same key. So both "no model list here" (404) and "model list
        # refused" (401/403) fall through to a single-token chat probe —
        # which is what actually authenticates the key for that endpoint.
        # A genuinely bad key still fails the probe, and that 401 is what
        # we report, so the probe cannot turn a real rejection into a
        # pass. Only chat-dialect presets do this; a responses-dialect
        # OpenAI serving no /models is a real problem to report, not to
        # probe around.
        if chat_dialect and (missing_models or refused_models):
            # Some OpenAI-compatible providers do not serve /models; a
            # single-token chat probe authenticates them without a real
            # completion. Only for chat-dialect presets — OpenAI itself
            # serving no /models would be a real problem.
            try:
                client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                )
            except Exception as probe_error:  # noqa: BLE001
                summary = sanitize(
                    _openai_error_kind(probe_error)
                    or str(probe_error),
                    secrets,
                )
                return ConnectionTest(
                    ok=False,
                    message=f"Connection failed: {summary}",
                )
            return ConnectionTest(
                ok=True,
                message=(
                    f"Connected: the API accepted the key for model {model}."
                ),
            )
        kind = _openai_error_kind(error)
        summary = sanitize(kind or str(error), secrets)
        return ConnectionTest(
            ok=False,
            message=f"Connection failed: {summary}",
        )
    return ConnectionTest(
        ok=True,
        message=f"Connected: the API accepted the key for model {model}.",
    )


def _openai_error_kind(error: Exception) -> str | None:
    """Turn known provider errors into useful, key-free sentences."""

    name = type(error).__name__
    status = getattr(error, "status_code", None)
    if status == 401 or "Authentication" in name:
        return "the API key was rejected"
    if status == 403 or "Permission" in name:
        return "the API key is not allowed to use this endpoint"
    if status == 404 or "NotFound" in name:
        return "the base URL does not serve the OpenAI API"
    if "Connection" in name or "Timeout" in name:
        return "the endpoint could not be reached"
    return None


def resolve_settings() -> StellaSettings | None:
    """The configuration Stella should start with, or None for first run.

    Precedence keeps advanced usage intact: an explicit ``STELLA_MODEL``
    environment uses the historical environment path exactly; otherwise a
    saved first-run configuration is loaded (endpoint environment
    variables still override it); otherwise the caller should show setup.
    """

    if os.environ.get("STELLA_MODEL"):
        return StellaSettings.from_environment()
    raw = load_configuration()
    if raw is None:
        return None

    def endpoint(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    preset = raw.get("preset")
    return StellaSettings.from_saved(
        provider=raw["provider"],
        preset=preset if isinstance(preset, str) else None,
        model=raw["model"],
        ollama_base_url=(
            os.environ.get("OLLAMA_BASE_URL")
            or endpoint(raw.get("ollama_base_url"))
            or DEFAULT_OLLAMA_BASE_URL
        ),
        openai_base_url=(
            os.environ.get("OPENAI_BASE_URL")
            or endpoint(raw.get("openai_base_url"))
        ),
        transcripts_enabled=raw.get("transcripts_enabled") is True,
        semantic_memory_enabled=raw.get("semantic_memory_enabled") is True,
        semantic_provider=raw.get("semantic_provider", "local-hash"),
        os_tools_enabled=raw.get("os_tools_enabled", True) is True,
        outline_tools_enabled=raw.get("outline_tools_enabled") is True,
        web_tools_enabled=raw.get("web_tools_enabled") is True,
        shell_tools_enabled=raw.get("shell_tools_enabled") is True,
        browser_tools_enabled=raw.get("browser_tools_enabled") is True,
        # Wake word is now on by default for the desktop UI: a saved config
        # that predates the key resolves to ON. An explicit
        # ``"wake_word_enabled": false`` (the unticked opt-out) still wins.
        wake_word_enabled=raw.get("wake_word_enabled", True) is not False,
    )
