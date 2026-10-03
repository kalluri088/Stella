"""Provider presets and the private on-disk store for API keys.

Security rules this module owns:

* API keys never enter ``StellaSettings`` (whose repr flows into logs and
  notices) and never enter ``config.json``. They live only in
  ``api_keys.json`` next to the state databases, written 0600 and
  replaced atomically.
* No function here ever returns key material in an error, a message or
  a log line; the hints are built from preset labels and key *prefixes*
  only, and the UI shows at most a redacted suffix.
* Precedence is explicit: ``OPENAI_API_KEY`` in the environment wins for
  the OpenAI and custom slots (the documented env path); a provider
  preset with its own identity (Claude, Grok, …) resolves *only* against
  its stored key, so an OpenAI env key can never be sent to Anthropic.
* ``stella backup`` deliberately excludes this file: backups are meant to
  travel, keys are not.

The presets are configuration, not code: every one of them rides the
existing OpenAI-compatible client with a different base URL and key, so
"the model is replaceable" (rule 2) stays one client deep.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

STORE_VERSION = 1
MAX_KEY_CHARS = 4096

# A key is an opaque token: no surrounding or embedded whitespace, no
# control characters. Anything else is a paste accident at best.
_KEY_SHAPE = re.compile(r"^[!-~]+$")

# A named secret's key is a lowercase identifier, not free text: it selects
# which credential a subsystem reads, so it must be stable and unambiguous.
_SECRET_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# The web capability's optional search key, stored like a provider key but
# never one (it is not a model endpoint). The environment variable
# ``TINYFISH_API_KEY`` still wins over this; see web_tools.
TINYFISH_SECRET = "tinyfish"


@dataclass(frozen=True)
class ProviderPreset:
    """One entry of the provider picker.

    ``detection_prefixes`` are the shapes that provider issues; the
    longest match wins, which is what separates ``sk-ant-`` from a bare
    ``sk-``. ``key_issuer`` names where a genuine key comes from so a
    mismatch hint can point at the right page. ``tool_dialect`` records
    which tool-calling API the endpoint serves: only OpenAI answers the
    Responses API; every other OpenAI-compatible endpoint (including an
    unknown custom gateway, which is the safer default) speaks
    ``chat/completions``.
    """

    id: str
    label: str
    base_url: str | None
    detection_prefixes: tuple[str, ...]
    models: tuple[str, ...]
    key_issuer: str
    tool_dialect: str = "chat"
    key_required: bool = True


PRESETS: dict[str, ProviderPreset] = {
    preset.id: preset
    for preset in (
        ProviderPreset(
            id="openai",
            label="OpenAI",
            base_url=None,
            detection_prefixes=("sk-proj-", "sk-"),
            models=("gpt-4o-mini", "gpt-4o"),
            key_issuer="platform.openai.com",
            tool_dialect="responses",
        ),
        ProviderPreset(
            id="anthropic",
            label="Claude (Anthropic)",
            base_url="https://api.anthropic.com/v1",
            detection_prefixes=("sk-ant-",),
            models=(
                "claude-sonnet-4-20250514",
                "claude-3-5-haiku-20241022",
            ),
            key_issuer="console.anthropic.com",
        ),
        ProviderPreset(
            id="xai",
            label="Grok (xAI)",
            base_url="https://api.x.ai/v1",
            detection_prefixes=("xai-",),
            models=("grok-4", "grok-3"),
            key_issuer="console.x.ai",
        ),
        ProviderPreset(
            id="groq",
            label="Groq",
            base_url="https://api.groq.com/openai/v1",
            detection_prefixes=("gsk_",),
            models=("openai/gpt-oss-120b", "openai/gpt-oss-20b"),
            key_issuer="console.groq.com",
        ),
        ProviderPreset(
            id="openrouter",
            label="OpenRouter",
            base_url="https://openrouter.ai/api/v1",
            detection_prefixes=("sk-or-v1-",),
            models=(
                "anthropic/claude-sonnet-4",
                "openai/gpt-4o-mini",
            ),
            key_issuer="openrouter.ai",
        ),
        ProviderPreset(
            id="google",
            label="Google Gemini",
            base_url=(
                "https://generativelanguage.googleapis.com"
                "/v1beta/openai/"
            ),
            detection_prefixes=("AIza",),
            models=("gemini-2.5-flash", "gemini-2.5-pro"),
            key_issuer="aistudio.google.com",
        ),
        ProviderPreset(
            id="freellmapi",
            label="FreeLLMAPI (local router)",
            base_url="http://localhost:3001/v1",
            detection_prefixes=("freellmapi-",),
            models=(),
            key_issuer="your FreeLLMAPI dashboard",
            tool_dialect="chat",
        ),
        ProviderPreset(
            id="custom",
            label="Other OpenAI-compatible",
            base_url=None,
            detection_prefixes=(),
            models=(),
            key_issuer="your provider",
        ),
        ProviderPreset(
            id="ollama",
            label="Local model (Ollama)",
            base_url=None,
            detection_prefixes=(),
            models=(),
            key_issuer="",
            key_required=False,
        ),
        ProviderPreset(
            id="llama",
            label="Local llama.cpp",
            base_url=None,
            detection_prefixes=(),
            models=(),
            key_issuer="",
            key_required=False,
        ),
    )
}

# Longest prefix first: "sk-or-v1-" must win over the bare "sk-", and
# "sk-ant-" over both.
_DETECTION_ORDER: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            (prefix, preset.id)
            for preset in PRESETS.values()
            for prefix in preset.detection_prefixes
        ),
        key=lambda pair: (-len(pair[0]), pair[1]),
    )
)

# The slots for which the OPENAI_API_KEY environment variable is the
# documented override. A named provider's preset is not one of them.
_ENV_OVERRIDABLE = frozenset({"openai", "custom"})


def api_keys_path() -> Path:
    """The private key store, next to config.json and the databases."""

    from stella.app import default_data_dir

    return default_data_dir() / "api_keys.json"


def _load_store() -> tuple[dict[str, str], dict[str, str], bool]:
    """The stored keys, the stored named secrets, and whether the file is
    present but untrusted.

    A missing store is empty and sound. A store that exists but cannot be
    parsed is empty *and* suspect: reads stay inert (grant nothing), but a
    write must refuse rather than merge onto nothing and silently drop
    everything the unreadable file was holding. A file written before named
    secrets existed simply carries no ``secrets`` map — that is an empty
    secrets set, not a suspect file: the ``keys`` half is what must parse.
    """

    path = api_keys_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, {}, False
    except (OSError, ValueError):
        return {}, {}, True
    if not isinstance(raw, dict):
        return {}, {}, True
    keys = raw.get("keys")
    if not isinstance(keys, dict):
        return {}, {}, True
    clean_keys = {
        preset_id: key
        for preset_id, key in keys.items()
        if isinstance(preset_id, str) and isinstance(key, str)
    }
    raw_secrets = raw.get("secrets")
    clean_secrets: dict[str, str] = {}
    if isinstance(raw_secrets, dict):
        clean_secrets = {
            name: value
            for name, value in raw_secrets.items()
            if isinstance(name, str) and isinstance(value, str)
        }
    return clean_keys, clean_secrets, False


def _read_store() -> dict[str, str]:
    return _load_store()[0]


def _read_secrets() -> dict[str, str]:
    return _load_store()[1]


def stored_api_key(preset_id: str | None) -> str | None:
    """The stored key for one preset, or None. Never raises."""

    if not preset_id:
        return None
    return _read_store().get(preset_id)


def effective_api_key(preset_id: str | None) -> str | None:
    """The key a session should use: environment first, then the store.

    ``OPENAI_API_KEY`` overrides only the OpenAI and custom slots — it is
    an OpenAI-namespace credential, and handing it to a Claude or Grok
    preset would recreate exactly the mismatch this store prevents. A
    missing preset (a config written before presets existed) is the
    legacy OpenAI slot.
    """

    slot = preset_id or "openai"
    if slot in _ENV_OVERRIDABLE:
        env_key = os.environ.get("OPENAI_API_KEY")
        if env_key:
            return env_key
    return stored_api_key(slot)


def _write_store(keys: dict[str, str], secrets: dict[str, str]) -> None:
    """Merge-then-atomic-replace: the file is never left half-written,
    and saving one provider's key cannot clobber another's or wipe the
    named secrets that share the file. The temp name is unique per write,
    so two instances saving concurrently race on distinct files and the
    last replace wins whole. Two instances saving the *same* entry are
    still last-writer-wins, which is honest for a single-user desktop.
    """

    path = api_keys_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "version": STORE_VERSION,
            "keys": keys,
            "secrets": secrets,
        },
        indent=2,
        sort_keys=True,
    )
    descriptor, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def save_api_key(preset_id: str, key: str) -> None:
    """Verify-on-entry is the caller's job; this only stores what it's given."""

    preset = PRESETS.get(preset_id)
    if preset is None or not preset.key_required:
        raise ValueError(f"unknown provider preset: {preset_id!r}")
    key = key.strip()
    if not key or len(key) > MAX_KEY_CHARS or not _KEY_SHAPE.match(key):
        raise ValueError("an API key is a single printable token")
    keys, secrets, suspect = _load_store()
    if suspect:
        raise ValueError(
            "the stored key file could not be read, so nothing was "
            f"overwritten — repair or remove {api_keys_path()} and retry"
        )
    keys[preset_id] = key
    _write_store(keys, secrets)


def delete_api_key(preset_id: str) -> None:
    keys, secrets, _suspect = _load_store()
    if keys.pop(preset_id, None) is None:
        return
    _write_store(keys, secrets)


def stored_presets() -> tuple[str, ...]:
    """Preset ids that have a stored key — names only, never values."""

    return tuple(sorted(_read_store()))


def stored_secret(name: str) -> str | None:
    """The stored value for one named secret, or None. Never raises.

    A named secret is a non-provider credential (a web-tool key, say) that
    shares this private file with the model keys: same 0600, same atomic
    replace, same never-returned-in-an-error discipline. It is *not* a
    provider preset and so never routes through the mismatch gates.
    """

    if not name:
        return None
    return _read_secrets().get(name)


def save_secret(name: str, value: str) -> None:
    """Store one named non-provider secret; the caller verifies its worth."""

    if not name or not _SECRET_NAME.match(name):
        raise ValueError("a secret name is a lowercase identifier")
    value = value.strip()
    if not value or len(value) > MAX_KEY_CHARS or not _KEY_SHAPE.match(value):
        raise ValueError("a secret is a single printable token")
    keys, secrets, suspect = _load_store()
    if suspect:
        raise ValueError(
            "the stored key file could not be read, so nothing was "
            f"overwritten — repair or remove {api_keys_path()} and retry"
        )
    secrets[name] = value
    _write_store(keys, secrets)


def delete_secret(name: str) -> None:
    keys, secrets, _suspect = _load_store()
    if secrets.pop(name, None) is None:
        return
    _write_store(keys, secrets)


def stored_secret_names() -> tuple[str, ...]:
    """Names that have a stored secret — names only, never values."""

    return tuple(sorted(_read_secrets()))


def detect_key_provider(key: str) -> str | None:
    """Which known provider issues this key's shape, or None.

    Prefix matching only; it identifies the *kind* of credential, never
    its validity.
    """

    key = key.strip()
    for prefix, preset_id in _DETECTION_ORDER:
        if key.startswith(prefix):
            return preset_id
    return None


def mismatch_hint(preset_id: str | None, key: str) -> str | None:
    """A friendly correction when the pasted key is another provider's.

    None means "proceed and let the provider judge": unknown shapes, the
    custom slot and matching presets all fall through to the real
    verification call. The text names labels and issuers only — never
    any part of the key.
    """

    if not key:
        return None
    preset = PRESETS.get(preset_id or "")
    if preset is None or not preset.key_required or preset.id == "custom":
        return None
    detected = detect_key_provider(key)
    if detected is None or detected == preset.id:
        return None
    other = PRESETS[detected]
    return (
        f"That key looks like it belongs to {other.label}, not "
        f"{preset.label}. Pick {other.label} in the provider list — or "
        f"paste the key from {preset.key_issuer}."
    )


def redacted_hint(key: str) -> str:
    """The only form a stored key may ever be displayed in."""

    return f"…{key[-4:]}" if len(key) >= 8 else "stored"


def tool_dialect_for(preset_id: str | None) -> str:
    """Which tool-calling API the endpoint for this preset answers."""

    preset = PRESETS.get(preset_id or "")
    if preset is None or preset.id == "openai":
        return "responses"
    return preset.tool_dialect


def base_url_for(preset_id: str | None) -> str | None:
    """The endpoint for a preset, or None for the SDK default."""

    preset = PRESETS.get(preset_id or "")
    return preset.base_url if preset else None


__all__ = [
    "MAX_KEY_CHARS",
    "PRESETS",
    "TINYFISH_SECRET",
    "ProviderPreset",
    "api_keys_path",
    "base_url_for",
    "delete_api_key",
    "delete_secret",
    "detect_key_provider",
    "effective_api_key",
    "mismatch_hint",
    "redacted_hint",
    "save_api_key",
    "save_secret",
    "stored_api_key",
    "stored_presets",
    "stored_secret",
    "stored_secret_names",
    "tool_dialect_for",
]
