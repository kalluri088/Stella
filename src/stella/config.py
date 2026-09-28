"""Saved first-run configuration and bounded provider probes.

This module owns everything a user needs *before* a Stella application
exists: a small JSON file under the XDG data directory that records the
chosen provider/model/endpoints, and read-only probes that answer
"is this reachable?" and "which models are installed?".

Security rules this module upholds:
- API keys are never written to the config file (there is no secure
  credential store; the key stays a per-session value supplied through
  the environment, exactly as before).
- Every probe returns a bounded, sanitized message; exception text is
  scrubbed of any secret the caller passed in before it is shown.
- Model names and provider responses are data. Nothing here constructs
  decisions, tools, or approvals.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from stella.app import StellaSettings, default_data_dir
from stella.llama_server import DEFAULT_LLAMA_SERVER_BINARY
from stella.ollama_client import DEFAULT_OLLAMA_BASE_URL

PROBE_TIMEOUT_SECONDS = 5.0
_MESSAGE_LIMIT = 160

_CONFIG_FIELDS = (
    "provider",
    "model",
    "ollama_base_url",
    "openai_base_url",
    "transcripts_enabled",
    "semantic_memory_enabled",
    "semantic_provider",
    "os_tools_enabled",
    "outline_tools_enabled",
    "web_tools_enabled",
)


def config_path() -> Path:
    return default_data_dir() / "config.json"


def save_configuration(settings: StellaSettings) -> None:
    """Persist only the non-secret provider/model/endpoint fields.

    API keys and paths to state are deliberately never stored.
    """

    payload = {
        field: getattr(settings, field) for field in _CONFIG_FIELDS
    }
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


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
    llama_binary: str = DEFAULT_LLAMA_SERVER_BINARY,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ConnectionTest:
    """Verify the chosen provider answers, without persisting anything.

    For Ollama this confirms the endpoint is alive and the model is in
    the installed list. For OpenAI-compatible endpoints it lists models,
    which authenticates the key without running any completion. The
    llama provider has no server to probe before it exists (Stella owns
    and starts it), so the test checks the two things a launch needs.
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
        timeout=timeout,
    )


def _test_openai_connection(
    *,
    model: str,
    base_url: str | None,
    api_key: str | None,
    timeout: float,
) -> ConnectionTest:
    secrets = tuple(value for value in (api_key,) if value)
    try:
        from openai import OpenAI
    except ImportError:  # pragma: no cover - installed via pyproject
        return ConnectionTest(
            ok=False,
            message="The OpenAI client library is not installed.",
        )
    key = api_key or ""
    if not key:
        return ConnectionTest(
            ok=False,
            message="No API key was entered. Type one, then test again.",
        )
    client = OpenAI(api_key=key, base_url=base_url, timeout=timeout * 4)
    try:
        client.models.list()
    except Exception as error:  # noqa: BLE001 - bounded, sanitized below
        summary = sanitize(_openai_error_kind(error) or str(error), secrets)
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

    return StellaSettings.from_saved(
        provider=raw["provider"],
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
        os_tools_enabled=raw.get("os_tools_enabled") is True,
        outline_tools_enabled=raw.get("outline_tools_enabled") is True,
        web_tools_enabled=raw.get("web_tools_enabled") is True,
    )
