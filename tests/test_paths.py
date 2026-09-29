"""Where Stella puts things, and the contract that choice has to keep.

The Linux answers are load-bearing: they are where existing users'
databases, personas and Outline tokens already are. The tests here pin
both the resolved Linux paths and the *call shape* — the same
``user_data_dir("<name>", appauthor=False)`` the Outline server uses for
the token file it writes — because a change in that shape silently breaks
the handoff between the two programs on every platform at once.
"""

import os
from pathlib import Path

import platformdirs
import pytest

from stella import app as stella_app
from stella import outline_tools, persona
from stella.app import (
    default_data_dir,
    default_history_db,
    default_memory_db,
    default_reminders_db,
    default_semantic_db,
    default_transcripts_db,
    default_vad_model,
    default_workspace,
)


def _clear_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def test_data_dir_still_resolves_to_the_linux_xdg_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_data_dir() == tmp_path / "xdg" / "stella"


def test_data_dir_without_xdg_is_the_local_share_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_xdg(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_data_dir() == tmp_path / ".local" / "share" / "stella"


def test_every_state_path_hangs_off_the_one_data_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "xdg" / "stella"
    assert Path(default_memory_db()) == root / "stella_memory.db"
    assert Path(default_reminders_db()) == root / "stella_reminders.db"
    assert Path(default_history_db()) == root / "stella_action_history.db"
    assert Path(default_transcripts_db()) == root / "stella_transcript.db"
    assert Path(default_semantic_db()) == root / "stella_semantic_index.db"
    assert Path(default_workspace()) == root / "workspace"


def test_data_dir_uses_the_platformdirs_call_shape_the_server_shares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    def fake_user_data_dir(appname, *, appauthor):
        seen.append((appname, appauthor))
        return "/tmp/resolved/stella"

    monkeypatch.setattr(stella_app, "user_data_dir", fake_user_data_dir)
    assert str(default_data_dir()) == "/tmp/resolved/stella"
    assert seen == [("stella", False)]


def test_vad_model_stays_under_home_and_overridable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A hand-downloaded model file: home is the one spelling that means
    # the right thing on all three platforms, so it does not move.
    _clear_xdg(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Path(default_vad_model()) == (
        tmp_path / "models" / "silero" / "silero_vad.onnx"
    )


def test_persona_directory_prefers_the_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STELLA_PERSONA_DIR", str(tmp_path / "mine"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert persona.persona_directory() == tmp_path / "mine"


def test_persona_directory_uses_the_xdg_config_home_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("STELLA_PERSONA_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert persona.persona_directory() == tmp_path / "xdg" / "stella"


def test_persona_directory_defaults_to_dot_config_on_linux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("STELLA_PERSONA_DIR", raising=False)
    _clear_xdg(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert persona.persona_directory() == tmp_path / ".config" / "stella"


def test_persona_directory_uses_the_shared_platformdirs_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    def fake_user_config_dir(appname, *, appauthor):
        seen.append((appname, appauthor))
        return "/tmp/resolved/stella"

    monkeypatch.delenv("STELLA_PERSONA_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(persona, "user_config_dir", fake_user_config_dir)
    assert str(persona.persona_directory()) == "/tmp/resolved/stella"
    assert seen == [("stella", False)]


def test_outline_token_dir_honours_the_explicit_data_dir(
    tmp_path: Path,
) -> None:
    (tmp_path / "outline.token").write_text("tok\n", encoding="utf-8")
    client = outline_tools._client_from_environment(
        {"OUTLINE_DATA_DIR": str(tmp_path)}
    )
    assert client is not None and client.token == "tok"


def test_outline_token_dir_follows_xdg_data_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # This used to be hardcoded to ~/.local/share/outline, so a server
    # honouring XDG_DATA_HOME wrote a token Stella could never see.
    root = tmp_path / "xdg" / "outline"
    root.mkdir(parents=True)
    (root / "outline.token").write_text("from-xdg", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OUTLINE_TOKEN", raising=False)
    monkeypatch.delenv("OUTLINE_DATA_DIR", raising=False)
    client = outline_tools._client_from_environment({})
    assert client is not None and client.token == "from-xdg"


def test_outline_token_uses_the_app_name_the_server_writes_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    def fake_user_data_dir(appname, *, appauthor):
        seen.append((appname, appauthor))
        return "/nonexistent"

    monkeypatch.delenv("OUTLINE_TOKEN", raising=False)
    monkeypatch.delenv("OUTLINE_DATA_DIR", raising=False)
    monkeypatch.setattr(outline_tools, "user_data_dir", fake_user_data_dir)
    assert outline_tools._client_from_environment({}) is None
    assert seen == [("outline", False)]
    # The shape is the handoff: the Outline server resolves its own data
    # directory the same way, on every platform.
    assert platformdirs.user_data_dir("outline", appauthor=False)


def test_split_command_default_is_the_posix_mode_on_this_host():
    # app.py and cli.py call split_command() with no platform argument:
    # on Linux that must still be plain posix shlex behaviour.
    from stella.portable import split_command

    assert split_command(r"prog --path /a\ b") == ["prog", "--path", "/a b"]


@pytest.mark.skipif(os.name != "posix", reason="mode bits are POSIX's mechanism")
def test_hardened_file_is_owner_only_on_this_platform(tmp_path: Path):
    from stella.portable import harden_private_file

    path = tmp_path / "token"
    path.write_text("secret", encoding="utf-8")
    result = harden_private_file(path)
    assert result.applied is True
    assert os.stat(path).st_mode & 0o777 == 0o600
