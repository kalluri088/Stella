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
from stella.app import (
    BARGE_MODES,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_TURN_TIME_LIMIT_SECONDS,
    MAX_TOOL_CALLS_LIMIT,
    MAX_TURN_TIME_LIMIT_SECONDS,
    StellaSettings,
    default_data_dir,
)
from stella.llama_server import DEFAULT_LLAMA_SERVER_BINARY
from stella.ollama_client import DEFAULT_OLLAMA_BASE_URL
from stella.portable import harden_private_file

PROBE_TIMEOUT_SECONDS = 5.0
_MESSAGE_LIMIT = 160
# The address the OpenAI SDK dials when it is handed no base URL. Named
# because two callers have to say it — the endpoint resolver below and the
# startup key sweep — and a silent second copy spelled out at each call site
# is how a probe and a runtime end up asking different servers.
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

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
    "wake_models",
    "wake_phrase",
    # The PipeWire node the always-open ear listens on. A node name is
    # hardware identity, not a secret, and it is saved because "which
    # microphone" is not something the owner should have to reach by
    # environment variable (docs/VOICE.md).
    "wake_source",
    # The first numeric settings this file persists: the two budgets one turn
    # may spend. They join the save list so a choice made in Settings survives
    # the next launch, and resolve_settings coerces them rather than trusting
    # whatever JSON number a hand-edit left behind.
    "max_tool_calls",
    "turn_time_limit_seconds",
    # The two voice switches that used to live only in a shell profile: the
    # resident synthesis worker, and when the barge-in ear arms. They are saved
    # for the same reason as the microphone row — a setting the owner can only
    # reach by environment variable is a setting she does not have, and a
    # reboot that quietly changes what the ear does is exactly what
    # commitment 6 of docs/STELLA_VISION.md forbids.
    "speech_resident",
    "voice_barge_in",
    # And the third: whether one finished capture is trimmed down to its
    # speech before the recogniser reads it. Saved on, because the clutter
    # it removes is a live complaint rather than a hypothetical, and saved
    # at all for the same reason as the two above it.
    "voice_capture_gate",
    # Where the small speech detector that does that trimming lives. A path
    # to a ~2 MB model file the owner downloaded by hand — input to Stella,
    # not state it keeps, so it is not the kind of path this file otherwise
    # refuses. It is saved because the gate says nothing when the file is
    # missing: a machine that keeps silero_vad.onnx elsewhere had one way to
    # say so, and it was a shell variable a desktop launch never reads.
    "vad_model",
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


def _request_key(api_key: str) -> str:
    """The key as it goes into an ``Authorization`` header.

    ``provider_keys.save_api_key`` strips before storing, so a token pasted
    with a trailing newline is saved clean but was *tested* dirty: the router
    rejects the whitespace and Stella reported "the API key was rejected"
    about a perfectly good key. Trimming here keeps the probe honest about
    the same bytes the store will keep. A value that is only whitespace
    becomes empty, which every caller already reads as "no key".
    """

    return api_key.strip()


def scan_openai_models(
    base_url: str,
    api_key: str,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ModelScan:
    """Query an OpenAI-compatible /models endpoint. Never downloads anything.

    The base_url is expected to already carry the version path (for example
    ``http://localhost:3001/v1``); this appends ``/models`` to it.
    """

    api_key = _request_key(api_key)
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


def openai_endpoint(
    preset: str | None,
    base_url_override: str | None = None,
) -> str:
    """The address the OpenAI client will dial for these settings.

    ``build_application`` hands the SDK
    ``settings.openai_base_url or provider_keys.base_url_for(preset)``
    (``app.py``), and a None there is not "no endpoint" — it is the SDK's
    own default, whatever the preset was called. A probe has to ask the
    server a turn would actually be sent to, or a green line proves
    nothing about the next request.
    """

    return (
        base_url_override
        or provider_keys.base_url_for(preset)
        or DEFAULT_OPENAI_BASE_URL
    )


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
    if not base_url or not (api_key := _request_key(api_key)):
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


def startup_key_health_report(
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> list[str]:
    """Check every stored key once at launch and return only the problems.

    The OpenAI-compatible endpoints (FreeLLMAPI and friends) are probed the
    same way the Settings panel probes them: a cheap ``GET /models`` with the
    Bearer key, no completion. A healthy key is silent — nothing comes back —
    because the ask was "check all the keys, but only show a connection
    error." Only a ``rejected`` or ``unreachable`` endpoint produces a short,
    safe line. The key is used solely to build the request and is never
    returned or logged; the only fragment that may appear is
    ``redacted_hint``'s last four characters, exactly as the panel shows it.

    Three categories are deliberately left unprobed, because a wrong answer
    there would nag on a valid key every launch:
      * a preset with no endpoint to reach (the custom slot before a base URL
        is known, and the local Ollama/llama.cpp servers, which take no key);
      * a preset that has no stored key at all;
      * an endpoint that cannot be judged from its model list — ``check_api_key``
        reports those as ``inconclusive``. That covers a Bearer call against
        Anthropic, whose real API authenticates with ``x-api-key``, so a naive
        probe would 401 a perfectly good key.
    """

    lines: list[str] = []
    for preset_id in provider_keys.stored_presets():
        preset = provider_keys.PRESETS.get(preset_id)
        if preset is None:
            continue
        # Deliberately not openai_endpoint(): this sweep walks stored keys
        # independently of any active settings, so for the slots that name no
        # endpoint of their own there is nothing to ask. Falling back to the
        # SDK default here would dial api.openai.com with a FreeLLMAPI key and
        # nag on a perfectly valid token.
        endpoint = preset.base_url or (
            DEFAULT_OPENAI_BASE_URL if preset.id == "openai" else None
        )
        if endpoint is None:
            continue
        key = provider_keys.effective_api_key(preset.id)
        if not key:
            continue
        check = check_api_key(
            endpoint,
            key,
            timeout,
            chat_dialect=provider_keys.tool_dialect_for(preset.id) == "chat",
        )
        redacted = provider_keys.redacted_hint(key)
        if check.state == "rejected":
            lines.append(
                f"Key check: {preset.label} {redacted} was rejected "
                f"({check.detail}). Enter a new key or pick another provider."
            )
        elif check.state == "unreachable":
            lines.append(
                f"Key check: {preset.label} {redacted} couldn't be reached "
                f"to verify ({check.detail}). The key may still be fine."
            )
    return lines


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
    raw_key = api_key or provider_keys.effective_api_key(preset) or ""
    key = _request_key(raw_key)
    # A saved configuration carries no base URL for a fixed preset, on
    # purpose — the endpoint belongs to the preset, not to the user's
    # settings. app.py resolves it the same way when it builds the real
    # client; without this identical step the SDK falls back to its own
    # default of api.openai.com, and a FreeLLMAPI or OpenRouter key gets
    # "rejected" by a server nobody chose.
    base_url = base_url or provider_keys.base_url_for(preset)
    # Both spellings are redaction material: an error string that echoed the
    # pasted form must not slip the untrimmed secret through.
    secrets = tuple(dict.fromkeys(part for part in (raw_key, key) if part))
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


def _saved_number(
    raw: object,
    *,
    low: float,
    high: float,
    integral: bool = False,
) -> float | None:
    """One saved number inside the allowed range, or None when it is not.

    ``low`` is exclusive and ``high`` inclusive, matching the ranges the
    Settings panel accepts. A JSON boolean is never a number here and a string
    is never converted. An unusable value means "use the default" rather than
    "refuse the file", because rejecting the configuration would silently
    discard every other setting the owner saved — the same drop-malformed rule
    ``wake_models`` above already follows.
    """

    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if integral and not float(raw).is_integer():
        return None
    if not low < raw <= high:
        return None
    return float(raw) if not integral else float(int(raw))


def _saved_barge_mode(raw: object) -> str:
    """The saved barge-in mode, or ``auto`` when the file says anything else.

    Deliberately tolerant rather than fatal: an unrecognised value in a
    hand-edited ``config.json`` falls back to the shipped default, because
    raising here would discard every other setting the owner saved — the same
    drop-malformed rule ``wake_models`` above already follows. The mode still
    cannot reach a microphone by accident: ``StellaSettings.__post_init__``
    refuses any name outside ``auto``, ``on``, ``off`` whoever constructs it,
    and ``auto`` is the spelling that arms nothing without a chosen capture.
    """

    if isinstance(raw, str) and raw.strip().casefold() in BARGE_MODES:
        return raw.strip().casefold()
    return "auto"


def _saved_vad_model(raw: object) -> str | None:
    """The saved speech-detector path, or None to use the usual place.

    Trimmed like ``wake_source`` above and dropped the same way when it is not
    a plain non-empty string, because a value this file cannot read is worth
    less than every other setting in it. None is not an error: it means Stella
    looks where it has always looked (``app.default_vad_model``), which is also
    what a config written before this key existed says. The path is opened as
    a file and never run, so nothing here needs to police where it points —
    ``SileroVad`` reports a missing model in one friendly sentence.
    """

    return raw.strip() if isinstance(raw, str) and raw.strip() else None


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
    # Classifier choices survive the JSON round-trip as a list of names;
    # anything malformed is dropped rather than trusted, and an empty
    # result means "detect every classifier on disk".
    raw_wake_models = raw.get("wake_models", ())
    wake_models = (
        tuple(
            name.strip()
            for name in raw_wake_models
            if isinstance(name, str) and name.strip()
        )
        if isinstance(raw_wake_models, (list, tuple))
        else ()
    )
    raw_wake_phrase = raw.get("wake_phrase")
    # The capture node the ear listens on. A malformed value is dropped —
    # which means "let Stella choose" — rather than refused or trusted
    # verbatim into a subprocess argv.
    raw_wake_source = raw.get("wake_source")
    wake_source = (
        raw_wake_source.strip()
        if isinstance(raw_wake_source, str) and raw_wake_source.strip()
        else None
    )
    # The two turn budgets. Both arrive as JSON numbers, and both are checked
    # against the same ranges the Settings panel refuses outside of: a
    # hand-edited config.json gets the default, not an unbounded loop.
    saved_calls = _saved_number(
        raw.get("max_tool_calls"),
        low=0,
        high=MAX_TOOL_CALLS_LIMIT,
        integral=True,
    )
    saved_seconds = _saved_number(
        raw.get("turn_time_limit_seconds"),
        low=0,
        high=MAX_TURN_TIME_LIMIT_SECONDS,
    )
    # The saved speech-detector path. Nothing is decided here beyond "a string
    # or nothing": a key this file cannot read is dropped, which sends Stella
    # to the usual place rather than refusing the rest of what was saved.
    vad_model = _saved_vad_model(raw.get("vad_model"))
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
        # On unless the owner saved a no: a config that predates this key, or
        # says nothing, runs with semantic recall (#245). Only an explicit
        # false turns it off, and the panel writes that key the moment the
        # checkbox is clicked.
        semantic_memory_enabled=raw.get("semantic_memory_enabled", True)
        is True,
        semantic_provider=raw.get("semantic_provider", "local-hash"),
        os_tools_enabled=raw.get("os_tools_enabled", True) is True,
        outline_tools_enabled=raw.get("outline_tools_enabled") is True,
        web_tools_enabled=raw.get("web_tools_enabled") is True,
        # Shell and browser now default ON for the desktop UI: a saved config
        # that predates these keys resolves to on. An explicit
        # ``"shell_tools_enabled": false`` / ``"browser_tools_enabled": false``
        # (the unticked opt-out) still wins. This feeds the desktop window and
        # ``stella voice``, which forces them back off in headless_settings().
        shell_tools_enabled=raw.get("shell_tools_enabled", True) is True,
        browser_tools_enabled=raw.get("browser_tools_enabled", True) is True,
        # Wake word is now on by default for the desktop UI: a saved config
        # that predates the key resolves to ON. An explicit
        # ``"wake_word_enabled": false`` (the unticked opt-out) still wins.
        wake_word_enabled=raw.get("wake_word_enabled", True) is not False,
        wake_models=wake_models,
        wake_phrase=(
            raw_wake_phrase if isinstance(raw_wake_phrase, str) else ""
        ),
        wake_source=wake_source,
        max_tool_calls=int(saved_calls or DEFAULT_MAX_TOOL_CALLS),
        turn_time_limit_seconds=(
            saved_seconds or DEFAULT_TURN_TIME_LIMIT_SECONDS
        ),
        # A config that predates these keys gets the shipped defaults: no
        # resident worker for a speech command that was never written down,
        # and an ear that arms only where a capture was named by hand.
        speech_resident=raw.get("speech_resident") is True,
        voice_barge_in=_saved_barge_mode(raw.get("voice_barge_in")),
        # Read the other way round than the two above, because this default
        # is on: an absent key, a hand-edited string, a null all mean the
        # owner never said no. Only a saved ``false`` switches the gate off.
        voice_capture_gate=raw.get("voice_capture_gate") is not False,
        vad_model=vad_model,
    )
