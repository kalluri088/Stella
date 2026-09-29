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
from dataclasses import dataclass
from pathlib import Path

STORE_VERSION = 1
MAX_KEY_CHARS = 4096

# A key is an opaque token: no surrounding or embedded whitespace, no
# control characters. Anything else is a paste accident at best.
_KEY_SHAPE = re.compile(r"^[!-~]+$")


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
            models=("llama-3.3-70b-versatile",),
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


def _read_store() -> dict[str, str]:
    try:
        raw = json.loads(api_keys_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    keys = raw.get("keys")
    if not isinstance(keys, dict):
        return {}
    return {
        preset_id: key
        for preset_id, key in keys.items()
        if isinstance(preset_id, str) and isinstance(key, str)
    }


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


def _write_store(keys: dict[str, str]) -> None:
    """Merge-then-atomic-replace: the file is never left half-written,
    and saving one provider's key cannot clobber another's. Two
    instances saving the *same* preset concurrently are last-writer-wins,
    which is honest for a single-user desktop.
    """

    path = api_keys_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {"version": STORE_VERSION, "keys": keys},
        indent=2,
        sort_keys=True,
    )
    tmp = path.with_suffix(".json.tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
    keys = _read_store()
    keys[preset_id] = key
    _write_store(keys)


def delete_api_key(preset_id: str) -> None:
    keys = _read_store()
    if keys.pop(preset_id, None) is None:
        return
    _write_store(keys)


def stored_presets() -> tuple[str, ...]:
    """Preset ids that have a stored key — names only, never values."""

    return tuple(sorted(_read_store()))


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
        f"That looks like a {other.label} key, not a {preset.label} "
        f"key. Pick {other.label} in the provider list — or paste the "
        f"key from {preset.key_issuer}."
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
    "ProviderPreset",
    "api_keys_path",
    "base_url_for",
    "delete_api_key",
    "detect_key_provider",
    "effective_api_key",
    "mismatch_hint",
    "redacted_hint",
    "save_api_key",
    "stored_api_key",
    "stored_presets",
    "tool_dialect_for",
]
