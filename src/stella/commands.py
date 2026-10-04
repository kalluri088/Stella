"""Slash commands: typed input the runtime handles before the model sees it.

A line that starts with ``/`` in an interactive session is a *command*,
never an utterance: it is intercepted by the I/O layer (CLI and TUI)
before ``StellaSession.run_turn()`` and never reaches the Brain as a
proposal to interpret. Two tiers exist:

* **Control commands** (``exit``, ``trace``, ``debug``, ``status``,
  ``help``, ``version``, ``clear``, ``history``, ``usage``) are built-in,
  local, and deterministic. They
  change what the *terminal* shows or ends; they grant no authority.
* **Prompt templates** are user-owned Markdown files in
  ``~/.config/stella/commands/<name>.md``. ``/name args`` expands the
  template (``$ARGUMENTS`` substitution, or the argument appended when
  the token is absent) and the expanded text flows through the normal
  turn path as *ordinary user input*: it gets no more authority than if
  it had been typed out in full, and approvals still gate every tool.

Deliberately absent: inline shell execution, permission frontmatter,
``@file`` embedding. Stella is not a coding agent; the template is the
whole feature (rules 4, 13, 15).

Provenance: only direct keyboard input is ever parsed here. Voice
transcripts and events call ``run_turn`` on other paths, so
a spoken "/exit" is simply a thing the user said (rule 7).

This module classifies and renders strings; it executes nothing. The
I/O layer owns behavior.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from stella.audit import RETAINED_WINDOW, format_line
from stella.persona import _read_regular_bounded, persona_directory

COMMAND_DIR_NAME = "commands"
MAX_COMMAND_BYTES = 8_192
ARGUMENTS_TOKEN = "$ARGUMENTS"

CONTROL_NAMES = frozenset(
    {
        "exit",
        "trace",
        "debug",
        "status",
        "help",
        "version",
        "clear",
        "history",
        "usage",
    }
)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class CommandCall:
    """One parsed command line: name (casefolded) plus the rest of it."""

    name: str
    argument: str
    is_control: bool


def parse_command_line(line: str) -> CommandCall | None:
    """Return the command a typed line invokes, or None if it is speech.

    Multi-line input (a TUI paste) is never a command. The name is the
    first token after the slash, casefolded; anything else on the line
    is the argument. A name that fails the template rules still parses
    as a command so the caller reports an unknown command instead of
    silently forwarding ``/foo`` to the model.
    """

    if "\n" in line:
        return None
    stripped = line.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(None, 1)
    name = parts[0].casefold() if parts else ""
    argument = parts[1].strip() if len(parts) > 1 else ""
    return CommandCall(name=name, argument=argument, is_control=name in CONTROL_NAMES)


def commands_directory() -> Path:
    """Where the user's prompt templates live (next to the persona)."""

    return persona_directory() / COMMAND_DIR_NAME


def available_template_names(directory: Path | None = None) -> list[str]:
    """Template names discoverable right now; a missing dir is empty."""

    base = commands_directory() if directory is None else directory
    try:
        entries = os.listdir(base)
    except OSError:
        return []
    names = [
        entry[: -len(".md")]
        for entry in entries
        if entry.endswith(".md") and _NAME_RE.fullmatch(entry[: -len(".md")])
    ]
    return sorted(name for name in names if (base / f"{name}.md").is_file())


def load_template_body(
    name: str, directory: Path | None = None
) -> tuple[str | None, str | None]:
    """Read one template: ``(body, None)`` or ``(None, error)``.

    Never raises. The name is validated before the filesystem is
    touched, the resolved path must stay inside the commands directory,
    and reads reuse the persona discipline (no symlinks, regular files
    only, hard size cap).
    """

    if not _NAME_RE.fullmatch(name):
        return None, f"/{name} is not a valid command name."
    base = commands_directory() if directory is None else directory
    file_name = f"{name}.md"
    candidate = base / file_name
    try:
        contained = os.path.realpath(candidate) == os.path.join(
            os.path.realpath(base), file_name
        )
    except OSError:
        contained = False
    if not contained:
        return None, f"/{name} resolves outside the commands directory; refusing."
    data = _read_regular_bounded(candidate, MAX_COMMAND_BYTES)
    if data is None:
        if not candidate.exists():
            return None, (
                f"There is no /{name} command. Type /help for what exists, "
                f"or add {base / file_name}."
            )
        return None, (
            f"/{name}: the template is unreadable or larger than "
            f"{MAX_COMMAND_BYTES} bytes."
        )
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, f"/{name}: the template is not valid UTF-8 text."


def expand_template(body: str, argument: str) -> str:
    """Substitute ``$ARGUMENTS``; append the argument when the token is absent."""

    if ARGUMENTS_TOKEN in body:
        return body.replace(ARGUMENTS_TOKEN, argument)
    if argument:
        return f"{body.rstrip()}\n\n{argument}"
    return body.rstrip()


def suggest_commands(name: str, directory: Path | None = None) -> list[str]:
    """Up to three near-miss command names across both tiers."""

    known = sorted(CONTROL_NAMES | set(available_template_names(directory)))
    return difflib.get_close_matches(name, known, n=3, cutoff=0.6)


def template_summary(
    name: str, directory: Path | None = None
) -> str | None:
    """The one line ``/help`` shows for a template, or None.

    A template is a Markdown file, so a leading ``#`` heading is the natural
    place to say what the command is for. A file with no heading gets no
    description rather than a quote of its prompt text: the first line of a
    prompt is usually an instruction, and rendering it as a description
    would mislabel it.
    """

    body, error = load_template_body(name, directory)
    if error is not None or body is None:
        return None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            return None
        return (stripped.lstrip("#").strip() or None)
    return None


def help_lines(directory: Path | None = None) -> list[str]:
    """Render the command listing shown by ``/help``."""

    base = commands_directory() if directory is None else directory
    lines = [
        "Commands (typed input only; voice never triggers them):",
        "  /exit             end this session",
        "  /status           what Stella is connected to right now",
        "  /help             this listing",
        "  /version          the Stella version",
        "  /trace on|off     show or hide the per-turn action timeline (terminal)",
        "  /debug on|off     show or hide raw model decisions (terminal)",
        "  /clear            forget this session's conversation (memories stay)",
        "  /history [n]      the most recent action records (default 10)",
        "  /usage            token counts this session's model calls used",
    ]
    templates = available_template_names(base)
    if templates:
        lines.append(
            "  prompt templates (yours; the expanded text is ordinary input):"
        )
        for name in templates:
            summary = template_summary(name, base) or ""
            lines.append(f"  /{name:<16} {summary}".rstrip())
    else:
        lines.append(
            f"  (no prompt templates yet — add a Markdown file named "
            f"<command>.md in {base}; $ARGUMENTS in it is replaced by what "
            f"you type after the command)"
        )
    return lines


def version_line() -> str:
    """Render ``/version`` from the installed package metadata."""

    from importlib.metadata import PackageNotFoundError, version

    try:
        return f"stella {version('stella')}"
    except PackageNotFoundError:
        return "stella (version unknown: the package metadata is missing)"


def parse_limit(argument: str, default: int = 10) -> int | None:
    """Read an optional count argument; None means the caller says usage."""

    if not argument:
        return default
    try:
        value = int(argument)
    except ValueError:
        return None
    if not 1 <= value <= RETAINED_WINDOW:
        return None
    return value


def action_history_lines(stella: object | None, limit: int = 10) -> list[str]:
    """Render the newest dispatcher action records (``/history``)."""

    dispatcher = getattr(stella, "tools", None)
    history = getattr(dispatcher, "history", None)
    if history is None or not callable(getattr(history, "recent", None)):
        return ["No action trail is available in this session."]
    entries = history.recent(limit)
    if not entries:
        return ["No action records yet (the trail is bounded and local)."]
    return [format_line(entry) for entry in entries]


def usage_lines(stella: object | None) -> list[str]:
    """Render ``/usage``: what this session's model calls have cost.

    The numbers are the provider's own counts, tallied since launch and
    never estimated. A provider that reports nothing is said so plainly,
    because a quiet zero reads like "this session was cheap" when the
    truth is "this provider does not tell us".
    """

    llm = getattr(getattr(stella, "brain", None), "llm", None)
    usage = getattr(llm, "usage", None)
    if usage is None or not callable(getattr(usage, "snapshot", None)):
        return ["This session's model reports no token counts."]
    snapshot = usage.snapshot()
    if snapshot.requests == 0:
        return ["No model calls yet this session."]
    lines = [
        f"model calls:  {snapshot.requests}",
        (
            f"tokens:       {snapshot.prompt_tokens:,} in · "
            f"{snapshot.completion_tokens:,} out"
        ),
        f"largest prompt: {snapshot.largest_prompt:,} tokens",
    ]
    if not snapshot.prompt_tokens and not snapshot.completion_tokens:
        lines.append(
            "(this provider reported no token counts, so those totals are "
            "zero rather than measured)"
        )
    return lines


def status_lines(
    *,
    settings: object | None = None,
    session: object | None = None,
    stella: object | None = None,
) -> list[str]:
    """Render ``/status``: what is connected where. Never a secret.

    Every read is defensive so a partial object still yields an honest
    line instead of an exception.
    """

    llm = getattr(getattr(stella, "brain", None), "llm", None)
    base_url = str(getattr(getattr(llm, "client", None), "base_url", "") or "")
    memory = getattr(stella, "memory", None)
    memory_db = getattr(memory, "database_path", None) if memory else None
    web_enabled = bool(getattr(settings, "web_tools_enabled", False))
    if not web_enabled:
        web_line = "off"
    elif os.environ.get("TINYFISH_API_KEY"):
        web_line = "on (TinyFish; your query leaves this machine)"
    else:
        web_line = "on (keyless DuckDuckGo; your query leaves this machine)"
    transcripts = getattr(session, "transcripts", None)
    capabilities = [
        label
        for label, flag in (
            ("desktop", getattr(settings, "os_tools_enabled", False)),
            ("outline", getattr(settings, "outline_tools_enabled", False)),
            ("shell", getattr(settings, "shell_tools_enabled", False)),
            ("browser", getattr(settings, "browser_tools_enabled", False)),
            ("wake", getattr(settings, "wake_word_enabled", False)),
        )
        if flag
    ]
    lines = [
        f"provider:  {type(llm).__name__ if llm is not None else 'unknown'}",
        f"model:     {getattr(llm, 'model', None) or 'unknown'}",
        f"endpoint:  {base_url or 'default'}",
        f"memory db: {memory_db or 'in-memory'}",
        f"capabilities: {', '.join(capabilities) if capabilities else 'core only'}",
        f"web:       {web_line}",
        f"transcripts: {'on (bounded local file)' if transcripts is not None else 'off'}",
        f"persona:   {persona_directory()}",
        f"commands:  {commands_directory()}",
    ]
    return lines
