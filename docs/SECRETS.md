# Secrets: where API keys live and how they are handled

Stella may talk to hosted model providers, which means the user may paste
an API key. This document is the complete story of what happens to that
key: where it is stored, who may read it, what may never contain it, and
what Stella deliberately does not do.

## The storage decision

Keys live in one private file, `api_keys.json`, inside the Stella data
directory (`~/.local/share/stella`, or `$XDG_DATA_HOME/stella`):

```json
{ "version": 1, "keys": { "anthropic": "sk-ant-…", "openai": "sk-…" } }
```

- The file is created with mode `0600` (owner read/write only) and every
  rewrite goes through a freshly created, uniquely named `0600` temporary
  file plus an atomic `os.replace`, so a crash cannot leave a
  half-written or world-readable key file and two saves cannot stamp on
  each other's scratch file.
- A corrupt store is inert to reads and fatal to writes: reads of an
  unparseable file grant nothing, but saving into one is refused rather
  than silently rebuilding the file from empty — which would drop every
  other provider's key. The user is told to repair or remove the file.
- There is **no keyring and no encryption at rest**, on purpose. A
  keyring would add a system dependency and a silent failure mode;
  encryption with a local key is theater — the key must be usable by a
  process running as the same user that could read the plaintext anyway.
  File permissions are the honest, understandable guarantee; the README's
  "honest limits" says exactly this.
- The store is written by exactly one module, `stella/provider_keys.py`.
  Nothing else touches the file.

## Precedence: which key a launch uses

For each provider preset, the effective key is resolved in this order:

1. an `OPENAI_API_KEY` exported in the environment — but **only for the
   `openai` and `custom` slots**; a named preset (Claude, Grok, Groq,
   OpenRouter, Gemini, FreeLLMAPI) resolves against its own stored key
   only, so an
   OpenAI environment key can never be shipped to Anthropic;
2. the stored key for that preset in `api_keys.json`;
3. no key — startup fails with a message naming both paths (enter one in
   setup, or export `OPENAI_API_KEY`).

Legacy compatibility: a configuration saved before presets existed has
`preset: null`, which means the `openai` slot — exactly the historical
behavior, including the environment precedence.

## Provider presets

The picker is data, not code: one table in `provider_keys.py`.

| id | label | endpoint | key shape | tools dialect |
| --- | --- | --- | --- | --- |
| `openai` | OpenAI | SDK default | `sk-proj-`, `sk-` | Responses API |
| `anthropic` | Claude (Anthropic) | `api.anthropic.com/v1` | `sk-ant-` | chat/completions |
| `xai` | Grok (xAI) | `api.x.ai/v1` | `xai-` | chat/completions |
| `groq` | Groq | `api.groq.com/openai/v1` | `gsk_` | chat/completions |
| `openrouter` | OpenRouter | `openrouter.ai/api/v1` | `sk-or-v1-` | chat/completions |
| `google` | Google Gemini | `generativelanguage.googleapis.com/…/openai/` | `AIza…` | chat/completions |
| `custom` | Other OpenAI-compatible | user's base URL | any | chat/completions |
| `ollama`, `llama` | local modes | — | no key | native / chat |

Only OpenAI serves the Responses API; every other endpoint speaks
`chat/completions`, so `OpenAILLMClient` takes a `tool_dialect` and the
preset table records which is which. A keyless "Other OpenAI-compatible"
endpoint is assumed to be the chat dialect — the safer default.

## Verification and the mismatch gate

A key is only trusted after a connection test passes, and the test itself
is gated:

- **Offline first.** Before any client is constructed, a pasted key whose
  shape belongs to a *different* known provider (a `sk-ant-` key in the
  OpenAI slot) is refused with a hint that names the provider it looks
  like and the page the real key comes from. Detection is longest
  prefix first, so `sk-or-v1-` never reads as bare `sk-`.
- **Online second.** The existing `models.list()` authentication probe
  stays; providers that do not serve `/models` (a 404 there) get exactly
  one bounded fallback — a single-token chat completion — which still
  only authenticates the key without running a real completion.
- The UI saves the key only after a passing test, clears the entry
  immediately, and thereafter shows only `…last4` as a hint. The full
  key is never displayed again in Stella.

## Invariants (each is pinned by a test)

- The key never enters `StellaSettings` (a frozen dataclass whose repr
  flows into notices, logs and saved-state paths). The dataclass carries
  the non-secret `preset` string only; `config.json` contains no key
  material.
- Mismatch hints and errors are built from preset labels and key
  prefixes, never key content; `sanitize()` scrubs the key from any
  provider error text.
- `stella backup` never carries `api_keys.json` (the backup scope is a
  whitelist of state databases + config, and a test pins the key file
  out of it).
- The UI path performs **no `os.environ` writes at all**; keys move from
  the paste field to the store, nowhere else in the process environment.
- Voice (Whisper/TTS) resolves strictly the `openai` slot: a Claude or
  Grok key is never offered to a voice endpoint.
- A malformed store file reads as "no stored keys" — inert, never fatal,
  never echoed.

## What is deliberately not done

- No keyring integration (even optional) and no encryption at rest.
- No OAuth flows or native per-provider client classes; hosted providers
  ride the OpenAI-compatible path by design.
- No full key re-display (suffix hint only) and no multi-profile keys.
- The TinyFish web key is unchanged: environment-only, never stored by
  Stella.
- No file lock around the store: two simultaneous setups writing the
  same preset is last-writer-wins per slot, which is documented rather
  than engineered around.
- Nothing voice is ever stored there. A transcriber or synthesizer
  command, the model a wake ear or the VAD loads, the capture source each
  reads and the OpenAI model identifiers (`whisper-1`, `tts-1`, `alloy`)
  are settings, so they live in `config.json` or the environment and never
  in `api_keys.json` — which is also why no voice path can name a key file.
  A cloud voice request uses the OpenAI slot's key like any other hosted
  call, and only that slot's (`VOICE.md`).

## Residual risks

- Anything running as your user can read `api_keys.json`. That is the
  accepted boundary of a permission-only store; it is why the README
  calls it a private plaintext file rather than a vault.
- The containing directory's permissions are whatever `$XDG_DATA_HOME`
  already has — Stella sets `0600` on the key file itself, not on the
  directory. A world-writeable data directory would weaken the story;
  that setup is broken for the config file (also `0600`) anyway.
- On Windows the store lands in `%LOCALAPPDATA%\stella`, and the mode
  bit Stella sets there is not a permission: privacy there comes from
  the owner ACL `stella.portable.harden_private_file` asks `icacls` to
  apply, and it reports honestly when it could not. Nothing on that
  platform is verified by this repository's tests, so the NTFS answer is
  asserted against fakes (`tests/test_portable.py`) and the *location* is
  isolated on every platform (`tests/test_isolation.py`) — a test suite
  that forgot to is one that writes real keys into a user's profile.

## Validation

- Offline: `tests/test_provider_keys.py` (store roundtrip, permissions,
  merge, malformed reads inert and writes refused, precedence, detection
  traps, key-free hints),
  `tests/test_config.py` (mismatch gate constructs no client, 404 chat
  probe, preset roundtrip), `tests/test_openai_client.py` and
  `tests/test_conformance.py` (chat dialect against the full conformance
  matrix), `tests/test_app.py` (store-backed build, voice scoping),
  `tests/test_ui.py` (setup stores after a verified test, Apply refuses
  a foreign key, a disk error during storage surfaces as a status line
  and not a crash, no environment writes), `tests/test_backup_cli.py`
  (key file excluded from backups), `tests/test_isolation.py` (every test
  that isolates the store isolates it on Windows too, not only on Linux).
- Live (2026-09-29, real display + real local Ollama, all state in temp
  directories): the first-run dialog listed installed models, the
  connection test passed, Start gated on that success, the saved
  `config.json` carried no secret and no key file was created for the
  keyless flow, and a real `qwen3:4b` turn answered correctly through
  the new build path. A Claude-shaped key pasted under the OpenAI
  preset was refused with the switch-preset hint, stored nothing, and
  left Start disabled — observed on the live window, not only in tests.
- Live: at least one non-OpenAI preset must be validated against the
  real endpoint (a tool turn for Claude, `models.list` for Grok) before
  the preset is claimed to work — unit tests alone never prove
  model-dependent behavior. This still awaits a real hosted key; the
  presets are offline-proven only.
