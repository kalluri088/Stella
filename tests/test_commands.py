"""Tests for the slash-command parser, template reader and renderers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from stella.commands import (
    ARGUMENTS_TOKEN,
    CommandCall,
    action_history_lines,
    available_template_names,
    commands_directory,
    expand_template,
    help_lines,
    load_template_body,
    parse_command_line,
    parse_limit,
    status_lines,
    suggest_commands,
    template_summary,
    usage_lines,
    version_line,
)
from stella.llm import UsageRecorder


def write_template(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_plain_speech_is_not_a_command() -> None:
    assert parse_command_line("hello stella") is None
    assert parse_command_line("  ") is None
    assert parse_command_line("path/to/file") is None


def test_multiline_paste_is_never_a_command() -> None:
    assert parse_command_line("/status\nand then some") is None


def test_command_parses_name_and_argument() -> None:
    call = parse_command_line("/Plan  the launch  ")
    assert call == CommandCall(name="plan", argument="the launch", is_control=False)


def test_control_names_are_control() -> None:
    assert parse_command_line("/exit").is_control
    assert parse_command_line("/STATUS").is_control
    assert parse_command_line("/trace on").name == "trace"
    assert parse_command_line("/trace on").argument == "on"


def test_bare_slash_is_an_unknown_not_speech() -> None:
    call = parse_command_line("/")
    assert call is not None and call.name == "" and not call.is_control


def test_invalid_names_never_reach_the_filesystem(tmp_path) -> None:
    for name in ("..", ".", "a/../../x", "a b", "", "x" * 65, "Ünicode"):
        body, error = load_template_body(name, directory=tmp_path)
        assert body is None and error is not None


def test_missing_template_is_an_error_string_not_an_exception(tmp_path) -> None:
    body, error = load_template_body("nope", directory=tmp_path)
    assert body is None
    assert "no /nope command" in error


def test_template_symlink_escape_is_refused(tmp_path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "sneaky.md").symlink_to(outside)

    body, error = load_template_body("sneaky", directory=commands)
    assert body is None
    assert "refusing" in error or "unreadable" in error


def test_oversize_template_is_refused(tmp_path) -> None:
    path = write_template(tmp_path, "big", "x" * 9000)
    assert path.stat().st_size > 8192
    body, error = load_template_body("big", directory=tmp_path)
    assert body is None
    assert "8192" in error


def test_non_utf8_template_is_refused(tmp_path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "binary.md").write_bytes(b"\xff\xfe\x00bad")
    body, error = load_template_body("binary", directory=tmp_path)
    assert body is None
    assert "UTF-8" in error


def test_available_template_names_filters_junk(tmp_path) -> None:
    write_template(tmp_path, "plan", "p")
    write_template(tmp_path, "9bad", "x")
    (tmp_path / "no-extension").write_text("x", encoding="utf-8")
    (tmp_path / "dir.md").mkdir()
    assert available_template_names(tmp_path) == ["plan"]


def test_available_names_with_no_directory() -> None:
    assert available_template_names(Path("/does/not/exist/stella-commands")) == []


def test_expand_template_replaces_every_occurrence() -> None:
    body = f"{ARGUMENTS_TOKEN} and again {ARGUMENTS_TOKEN}"
    assert expand_template(body, "hi") == "hi and again hi"


def test_expand_template_appends_when_token_absent() -> None:
    assert expand_template("Summarize:\n", "the text") == "Summarize:\n\nthe text"
    assert expand_template("Hello", "") == "Hello"
    assert expand_template(f"Say {ARGUMENTS_TOKEN}", "") == "Say "


def test_suggestions_cover_both_tiers(tmp_path) -> None:
    write_template(tmp_path, "summarize", "s")
    assert "status" in suggest_commands("stauts", directory=tmp_path)
    assert "summarize" in suggest_commands("sumarize", directory=tmp_path)
    assert suggest_commands("zzzzqqqq", directory=tmp_path) == []


def test_help_lists_controls_and_templates(tmp_path) -> None:
    write_template(tmp_path, "plan", "p")
    text = "\n".join(help_lines(directory=tmp_path))
    assert "/exit" in text and "/status" in text and "/plan" in text


def test_help_without_templates_points_at_the_directory(tmp_path) -> None:
    text = "\n".join(help_lines(directory=tmp_path))
    assert str(tmp_path) in text


def test_status_never_raises_on_partial_objects() -> None:
    lines = status_lines(settings=None, session=None, stella=None)
    joined = "\n".join(lines)
    assert "provider:  unknown" in joined
    assert "web:       off" in joined


def test_status_names_the_web_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    settings = type("S", (), {"web_tools_enabled": True})()
    joined = "\n".join(status_lines(settings=settings))
    assert "DuckDuckGo" in joined
    monkeypatch.setenv("TINYFISH_API_KEY", "sk-not-a-real-key")
    joined = "\n".join(status_lines(settings=settings))
    assert "TinyFish" in joined
    assert "sk-not-a-real-key" not in joined


def test_commands_directory_follows_the_persona_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STELLA_PERSONA_DIR", str(tmp_path))
    assert commands_directory() == tmp_path / "commands"


def test_version_line_is_honest() -> None:
    assert version_line().startswith("stella ")


def test_clear_and_history_are_control_names() -> None:
    assert parse_command_line("/clear").is_control
    assert parse_command_line("/history 5").is_control
    assert parse_command_line("/history 5").argument == "5"


def test_help_lists_the_new_commands() -> None:
    text = "\n".join(help_lines(directory=Path("/nonexistent")))
    assert "/clear" in text and "/history" in text


def test_parse_limit_defaults_and_rejects() -> None:
    assert parse_limit("") == 10
    assert parse_limit("25") == 25
    assert parse_limit("0") is None
    assert parse_limit("99999") is None
    assert parse_limit("many") is None


def test_action_history_lines_without_a_trail() -> None:
    assert action_history_lines(None) == [
        "No action trail is available in this session."
    ]


def test_action_history_lines_renders_entries() -> None:
    from datetime import UTC, datetime

    from stella.history import InMemoryActionHistory
    from stella.tools import ToolDispatcher

    class Silent:  # a registered name is all the dispatcher needs here.
        name = "audit_probe"
        description = "probe"

    dispatcher = ToolDispatcher([])
    dispatcher._history = InMemoryActionHistory(max_records=8)
    dispatcher.history.append(
        {
            "timestamp": datetime(2026, 9, 29, 12, 0, tzinfo=UTC).isoformat(),
            "capability": "audit_probe",
            "arguments": {},
        }
    )
    stella = type("S", (), {"tools": dispatcher})()
    lines = action_history_lines(stella)
    assert len(lines) == 1
    assert "audit_probe" in lines[0]


# --- /usage ---------------------------------------------------------------


def _stella_with_usage(usage: object) -> object:
    """A stella-shaped stub: brain.llm is where the client's recorder lives."""

    return SimpleNamespace(brain=SimpleNamespace(llm=SimpleNamespace(usage=usage)))


def test_usage_is_a_control_command() -> None:
    call = parse_command_line("/usage")
    assert call is not None
    assert call.is_control is True


def test_usage_is_listed_in_help() -> None:
    text = "\n".join(help_lines(directory=Path("/nonexistent")))
    assert "/usage" in text


def test_usage_says_so_when_the_session_reports_nothing() -> None:
    assert usage_lines(None) == [
        "This session's model reports no token counts."
    ]
    assert usage_lines(_stella_with_usage(None)) == [
        "This session's model reports no token counts."
    ]


def test_usage_before_the_first_model_call() -> None:
    lines = usage_lines(_stella_with_usage(UsageRecorder()))
    assert lines == ["No model calls yet this session."]


def test_usage_renders_the_providers_own_counts() -> None:
    usage = UsageRecorder()
    usage.record(1200, 80)
    usage.record(5300, 40)

    text = "\n".join(usage_lines(_stella_with_usage(usage)))
    assert "model calls:  2" in text
    assert "6,500 in · 120 out" in text
    assert "largest prompt: 5,300 tokens" in text
    # Counts exist here, so the "provider told us nothing" note stays off.
    assert "reported no token counts" not in text


def test_usage_calls_out_a_provider_that_reports_nothing() -> None:
    # A local endpoint that omits the usage block still counts requests;
    # saying only "0 in" would read as a cheap session rather than a
    # provider that does not tell us.
    usage = UsageRecorder()
    usage.record(None, None)

    text = "\n".join(usage_lines(_stella_with_usage(usage)))
    assert "model calls:  1" in text
    assert "reported no token counts" in text


# --- /help template descriptions and /status capabilities ----------------


def test_template_summary_uses_the_leading_heading(tmp_path) -> None:
    write_template(tmp_path, "plan", "# Plan a release\n\nSteps: $ARGUMENTS")
    assert template_summary("plan", directory=tmp_path) == "Plan a release"


def test_template_without_a_heading_has_no_invented_description(tmp_path) -> None:
    # The first line of a prompt is an instruction, not a label; quoting it
    # as a description would mislabel the file.
    write_template(tmp_path, "draft", "Draft this: $ARGUMENTS")
    assert template_summary("draft", directory=tmp_path) is None
    write_template(tmp_path, "empty", "#   ")
    assert template_summary("empty", directory=tmp_path) is None


def test_help_lists_each_template_with_its_description(tmp_path) -> None:
    write_template(tmp_path, "plan", "# Plan a release\n\n$ARGUMENTS")
    write_template(tmp_path, "draft", "Draft this: $ARGUMENTS")
    lines = help_lines(directory=tmp_path)
    text = "\n".join(lines)
    assert "/plan" in text and "Plan a release" in text
    # A heading-less template is still listed, just bare.
    assert "/draft" in text
    assert "prompt templates" in text


def test_status_names_the_connected_capabilities() -> None:
    settings = type(
        "S",
        (),
        {
            "web_tools_enabled": False,
            "os_tools_enabled": True,
            "outline_tools_enabled": True,
            "shell_tools_enabled": False,
            "browser_tools_enabled": False,
            "wake_word_enabled": False,
        },
    )()
    joined = "\n".join(status_lines(settings=settings))
    assert "capabilities: desktop, outline" in joined


def test_status_says_core_only_when_nothing_is_connected() -> None:
    joined = "\n".join(status_lines(settings=None))
    assert "capabilities: core only" in joined
