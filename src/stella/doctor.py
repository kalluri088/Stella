"""``stella doctor`` — a read-only answer to "what will work on this box?".

Why this exists: a wheel carries Python and nothing else. The pieces that
make Stella's voice and capabilities real — a capture binary, a transcriber,
a speech worker, the VAD and wake ONNX files, a pulled Ollama model, a
browser — all live outside the package, and until now the only way to find
one was missing was to press the shortcut and hear nothing. This is the
command the one-command installer prints at the end, and the one the release
workflow runs against a freshly installed wheel.

Rules this module upholds:

- **Read-only.** No directory is created, no database is opened, nothing is
  started and no device is touched. The two probes that leave the filesystem
  are Ollama's model list (a GET that downloads nothing, reused from
  ``stella.config``) and connecting to the voice server's control socket,
  whose handler answers a word it does not recognise and acts on nothing.
- **No secrets, ever.** Keys and named secrets are reported as present or
  absent, and by name. The value is discarded in the same expression that
  asks for it and never reaches a ``Check``.
- **It reports what Stella will actually do**, by reusing the same resolvers
  (``default_data_dir``, ``default_speech_worker``, ``find_browser``,
  ``sandbox_available``, ``resolve_settings``) instead of re-deriving them,
  so a green line here means the same check passed at runtime.
- **It works on an unconfigured machine** — that is its main job — so a
  settings object that refuses to build is one line of output, never a
  traceback.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from stella import __version__, provider_keys
from stella.app import (
    StellaSettings,
    default_data_dir,
    default_history_db,
    default_memory_db,
    default_semantic_db,
    default_transcripts_db,
    default_vad_model,
    default_workspace,
)
from stella.browser_tools import find_browser
from stella.config import (
    config_path,
    load_configuration,
    resolve_settings,
    scan_ollama_models,
)
from stella.headless_voice import _socket_alive, control_path
from stella.persona import persona_directory
from stella.sandbox import sandbox_available
from stella.voice import PLAY_BINARIES, RECORD_BINARIES, default_speech_worker
from stella.wake import SHARED_WAKE_MODELS, default_wake_model_dir

READY = "ok"
MISSING = "missing"
INFO = "info"


@dataclass(frozen=True)
class Check:
    """One line of the report: what was asked, what is true, what to do."""

    group: str
    name: str
    state: str
    detail: str
    fix: str = ""


@dataclass(frozen=True)
class Report:
    checks: tuple[Check, ...]

    def missing(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.state == MISSING)


def _on_path(names: tuple[str, ...]) -> str | None:
    return next((found for name in names if (found := shutil.which(name))), None)


def _installed(distribution: str) -> bool:
    """Whether an optional extra's import is present — without importing it.

    ``find_spec`` answers "could this be imported" in milliseconds and never
    loads the module, which matters on this machine: ``sentence_transformers``
    pulls torch with it, and a diagnostic that costs gigabytes of RAM to say
    "yes, it's installed" is worse than no diagnostic.
    """

    try:
        return importlib.util.find_spec(distribution) is not None
    except (ImportError, ValueError):
        return False


def _has_key(preset: str | None) -> bool:
    # Presence only; the value is dropped by this expression.
    return provider_keys.effective_api_key(preset) is not None


def _wake_classifiers(model_dir: str) -> tuple[str, ...]:
    """Phrase classifiers actually on disk, not the names Stella would try.

    ``wake.default_wake_models`` deliberately falls back to a hardcoded
    name when the directory is empty, which is right for a startup message
    and wrong for a preflight: doctor has to distinguish "one classifier
    placed here" from "nothing here at all".
    """

    try:
        entries = os.listdir(model_dir)
    except OSError:
        return ()
    return tuple(
        sorted(
            name
            for name in entries
            if name.endswith(".onnx") and name not in SHARED_WAKE_MODELS
        )
    )


def _build_checks() -> tuple[Check, ...]:
    from stella import __file__ as package_file

    here = str(Path(package_file).parent)
    # A wheel lands under site-packages; anything else is a source tree —
    # the distinction the dev/released split depends on.
    flavour = (
        "installed release"
        if "site-packages" in here or "dist-packages" in here
        else "source tree"
    )
    return (
        Check(
            "build",
            "version",
            READY,
            f"stella {__version__} · python {platform.python_version()}",
        ),
        Check("build", "running from", READY, f"{flavour}: {here}"),
        Check("build", "interpreter", READY, sys.executable),
    )


def _state_checks() -> tuple[Check, ...]:
    data = default_data_dir()
    configured = load_configuration()
    persona = persona_directory()
    socket = control_path()
    server_up = _socket_alive(socket)
    checks = [
        Check(
            "state",
            "data dir",
            READY if data.is_dir() else INFO,
            str(data) + ("" if data.is_dir() else " (created on first run)"),
        ),
        Check(
            "state",
            "config",
            READY if configured else INFO,
            f"{config_path()}"
            + (
                f" — provider {configured.get('provider')}, "
                f"model {configured.get('model') or 'unset'}"
                if configured
                else " — not configured yet"
            ),
            fix="run stella-ui once, or export STELLA_MODEL",
        ),
        Check(
            "state",
            "persona",
            READY if (persona / "persona.md").is_file() else MISSING,
            str(persona)
            + (
                ""
                if (persona / "persona.md").is_file()
                else " — no persona.md, so she answers as the built-in default"
            ),
            fix="give her one: stella persona preset warm",
        ),
    ]
    for label, path in (
        ("memory db", default_memory_db()),
        ("action history db", default_history_db()),
        ("transcript db", default_transcripts_db()),
        ("semantic db", default_semantic_db()),
    ):
        written = Path(path).is_file()
        checks.append(
            Check(
                "state",
                label,
                READY if written else INFO,
                path + ("" if written else " (not yet written)"),
            )
        )
    workspace = Path(default_workspace())
    checks.append(
        Check(
            "state",
            "workspace",
            READY if workspace.is_dir() else INFO,
            str(workspace),
        )
    )
    checks.append(
        Check(
            "state",
            "voice server",
            READY if server_up else INFO,
            f"running at {socket}" if server_up else f"not running ({socket})",
        )
    )
    return tuple(checks)


def _model_checks(settings: StellaSettings | None) -> tuple[Check, ...]:
    if settings is None:
        return (
            Check(
                "model",
                "provider",
                MISSING,
                "nothing resolved — Stella cannot start a session yet",
                fix="run stella-ui once to choose a provider and model",
            ),
        )
    checks: list[Check] = [
        Check(
            "model",
            "provider",
            READY,
            settings.provider
            + (f" (preset {settings.preset})" if settings.preset else "")
            + f" · model {settings.model or 'unset'}",
        )
    ]
    if settings.provider == "ollama":
        scan = scan_ollama_models(settings.ollama_base_url)
        checks.append(
            Check(
                "model",
                "ollama",
                READY if scan.reachable else MISSING,
                scan.message
                if not scan.reachable
                else f"{len(scan.models)} model(s) at {settings.ollama_base_url}",
                fix="start it: ollama serve",
            )
        )
        if scan.reachable:
            wanted = settings.model or ""
            have = any(
                name == wanted or name.startswith(wanted + ":")
                for name in scan.models
            )
            checks.append(
                Check(
                    "model",
                    "model pulled",
                    READY if have or not wanted else MISSING,
                    f"'{wanted}' is available"
                    if have
                    else f"'{wanted}' is not in {', '.join(sorted(scan.models))}",
                    fix=f"ollama pull {wanted}",
                )
            )
        return tuple(checks)
    key_present = _has_key(settings.preset)
    checks.append(
        Check(
            "model",
            "api key",
            READY if key_present else MISSING,
            "present" if key_present else "none stored for this preset",
            fix="add it in stella-ui (Settings → provider)",
        )
    )
    return tuple(checks)


def _voice_checks(settings: StellaSettings) -> tuple[Check, ...]:
    capture = _on_path(RECORD_BINARIES)
    playback = _on_path(PLAY_BINARIES)
    checks: list[Check] = [
        Check(
            "voice",
            "microphone",
            READY if capture else MISSING,
            capture or f"none of {', '.join(RECORD_BINARIES)} on PATH",
            fix="install pw-record (pipewire) or arecord (alsa-utils)",
        ),
        Check(
            "voice",
            "playback",
            READY if playback else MISSING,
            playback or f"none of {', '.join(PLAY_BINARIES)} on PATH",
            fix="install pw-play (pipewire) or aplay (alsa-utils)",
        ),
    ]

    command = settings.transcription_command or os.environ.get(
        "STELLA_TRANSCRIPTION_COMMAND", ""
    )
    voxtype = shutil.which("voxtype")
    if settings.voice_transcription == "off":
        checks.append(
            Check("voice", "transcription", INFO, "switched off in settings")
        )
    elif command:
        runnable = shutil.which(command.split()[0]) is not None
        checks.append(
            Check(
                "voice",
                "transcription",
                READY if runnable else MISSING,
                f"command: {command}",
                fix="the first word of the transcription command is not on PATH",
            )
        )
    else:
        checks.append(
            Check(
                "voice",
                "transcription",
                READY if voxtype else MISSING,
                f"voxtype at {voxtype}"
                if voxtype
                else "no voxtype on PATH and no transcription command set",
                fix="install voxtype, or point STELLA_TRANSCRIPTION_COMMAND at "
                "any command that prints words when given a WAV file",
            )
        )

    worker = default_speech_worker()
    worker_ready = os.access(worker, os.X_OK)
    speech_ready = worker_ready or bool(settings.speech_command) or _has_key(
        settings.preset
    )
    checks.append(
        Check(
            "voice",
            "speech",
            READY if speech_ready else MISSING,
            f"worker {worker}"
            if worker_ready
            else (
                f"command: {settings.speech_command}"
                if settings.speech_command
                else "no resident worker, no speech command, no provider TTS key"
            ),
            fix=f"place an executable speech server at {worker}, or set a "
            "speech command, or add a provider key",
        )
    )

    vad = settings.vad_model or default_vad_model()
    onnx = _installed("onnxruntime")
    checks.append(
        Check(
            "voice",
            "barge-in",
            READY if Path(vad).is_file() and onnx else INFO,
            vad
            if Path(vad).is_file()
            else f"{vad} not found — voice works, you just cannot interrupt it",
            fix="place silero_vad.onnx there (and the barge-in extra) or set "
            "STELLA_VAD_MODEL",
        )
    )
    wake_dir = settings.wake_model_dir or default_wake_model_dir()
    classifiers = _wake_classifiers(wake_dir)
    checks.append(
        Check(
            "voice",
            "wake word",
            READY if classifiers and onnx else INFO,
            f"{len(classifiers)} classifier(s) in {wake_dir}: "
            + ", ".join(classifiers)
            if classifiers
            else f"{wake_dir} holds no classifiers — the shortcut still works, "
            "only the wake word does not",
            fix=f"drop openWakeWord .onnx files in {wake_dir} (needs the wake "
            "extra)",
        )
    )
    checks.append(
        Check(
            "voice",
            "onnxruntime",
            READY if onnx else INFO,
            "installed" if onnx else "not installed (barge-in and wake need it)",
            fix="reinstall with the extras: uv tool install 'stella[wake,barge-in]'",
        )
    )
    return tuple(checks)


def _capability_checks(settings: StellaSettings) -> tuple[Check, ...]:
    display = os.environ.get("WAYLAND_DISPLAY")
    compositor = _on_path(("hyprctl",))
    browser = find_browser(os.environ)
    jailed = sandbox_available()
    tinyfish = bool(provider_keys.stored_secret(provider_keys.TINYFISH_SECRET))
    ddgs = _installed("ddgs")
    checks: list[Check] = [
        Check(
            "desktop",
            "compositor",
            READY if display and compositor else INFO,
            f"{display} via {compositor}"
            if display and compositor
            else "no Wayland session or hyprctl — desktop tools stay off",
            fix="desktop control needs a Hyprland session",
        ),
        Check(
            "desktop",
            "sandbox",
            READY if jailed else MISSING,
            "bubblewrap jail available"
            if jailed
            else "no bwrap, or unprivileged user namespaces are switched off",
            fix="install bubblewrap; without it the shell and file tools fall "
            "back to a confined directory instead of a jail",
        ),
        Check(
            "desktop",
            "browser",
            READY if browser else INFO,
            browser or "no Chromium-family browser on PATH — page reading is off",
            fix="install a Chromium-family browser or set STELLA_BROWSER",
        ),
        Check(
            "desktop",
            "web search",
            READY if tinyfish or ddgs else INFO,
            "TinyFish key"
            if tinyfish
            else (
                "keyless ddgs"
                if ddgs
                else "neither a TinyFish key nor ddgs — web tools say 'web is off'"
            ),
            fix="add the TinyFish secret in Settings, or install the web extra",
        ),
    ]
    secrets = provider_keys.stored_secret_names()
    checks.append(
        Check(
            "desktop",
            "stored secrets",
            READY if secrets else INFO,
            ", ".join(secrets) or "none",
        )
    )
    embedded = settings.semantic_provider != "local-hash" or _installed(
        "sentence_transformers"
    )
    checks.append(
        Check(
            "desktop",
            "semantic recall",
            READY if embedded else INFO,
            f"provider {settings.semantic_provider}"
            + (
                ""
                if settings.semantic_memory_enabled
                else " (disabled in settings)"
            ),
            fix="install the embed extra for local embeddings",
        )
    )
    return tuple(checks)


def collect() -> Report:
    """Run every probe and return the checks. Performs no writes."""

    try:
        settings = resolve_settings()
    except (SystemExit, Exception) as error:  # noqa: BLE001
        # Doctor's whole purpose is a machine that will not start, so
        # anything a settings object can raise on the way in — a bad saved
        # value, a SystemExit from an invalid environment variable — is one
        # line of output here rather than a traceback on the owner's shell.
        # KeyboardInterrupt is deliberately not caught.
        return Report(
            checks=(
                *_build_checks(),
                Check(
                    "state",
                    "settings",
                    MISSING,
                    f"could not be resolved: {type(error).__name__}: {error}",
                    fix="correct the named value, or move config.json aside "
                    "and configure again",
                ),
            )
        )
    checks = [*_build_checks(), *_state_checks(), *_model_checks(settings)]
    checks = [*_build_checks(), *_state_checks(), *_model_checks(settings)]
    # Voice and desktop shape itself from the machine, not the model choice,
    # so those checks still run against defaults on an unconfigured box: "is
    # there a recorder, is the jail available" is the question a first-run
    # user is really asking. _model_checks above already says plainly when
    # nothing is resolved.
    resolved = settings or StellaSettings()
    checks.extend(_voice_checks(resolved))
    checks.extend(_capability_checks(resolved))
    tkinter = _installed("tkinter")
    checks.append(
        Check(
            "interface",
            "settings window",
            READY if tkinter else MISSING,
            "tkinter available"
            if tkinter
            else "python's Tk bindings are missing — stella-ui cannot open",
            fix="install this distribution's tk package (e.g. python-tk)",
        )
    )
    return Report(checks=tuple(checks))


def render(report: Report) -> str:
    """The human form: grouped lines, then what to do about any misses."""

    lines = ["stella doctor — read-only; nothing here starts or writes", ""]
    width = max(len(check.name) for check in report.checks)
    group = None
    for check in report.checks:
        if check.group != group:
            group = check.group
            lines.append(group)
        mark = {READY: "ok  ", MISSING: "MISS", INFO: "-   "}[check.state]
        lines.append(f"  {mark} {check.name:<{width}}  {check.detail}")
    missing = report.missing()
    lines.append("")
    if missing:
        lines.append("to fix")
        lines.extend(
            f"  {check.name}: {check.fix}" for check in missing if check.fix
        )
    else:
        lines.append("nothing missing — everything Stella needs here is present.")
    return "\n".join(lines)


def as_json(report: Report) -> str:
    """The machine form, asserted on by the release workflow."""

    return json.dumps(
        {
            "version": __version__,
            "python": platform.python_version(),
            "checks": [
                {
                    "group": check.group,
                    "name": check.name,
                    "state": check.state,
                    "detail": check.detail,
                    "fix": check.fix,
                }
                for check in report.checks
            ],
            "missing": [check.name for check in report.missing()],
        },
        indent=2,
    )


__all__ = [
    "INFO",
    "MISSING",
    "READY",
    "Check",
    "Report",
    "as_json",
    "collect",
    "render",
]
