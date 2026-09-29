"""The cross-platform decisions, proven with fakes.

Nothing here pretends to be Windows or macOS by faking ``sys.platform``
globally — that would make the *decision* untestable and the *result*
meaningless on this host. Each function takes the platform as a
parameter or resolves it through one seam, so the branch that runs on
another OS is exercised here even though only Linux can really run it.
"""

import os
import subprocess

import pytest

from stella import portable
from stella.portable import (
    LINUX,
    MACOS,
    OTHER,
    WINDOWS,
    Hardening,
    default_editor,
    platform_name,
    split_command,
)


def test_platform_name_maps_the_three_targets_and_keeps_the_rest_posix():
    assert platform_name("linux") == LINUX
    assert platform_name("linux2") == LINUX
    assert platform_name("darwin") == MACOS
    assert platform_name("win32") == WINDOWS
    # Anything else follows the POSIX branches: the safe direction, since
    # those are the paths this project actually exercises.
    assert platform_name("freebsd14") == OTHER
    assert platform_name("cygwin") == OTHER


def test_split_command_keeps_posix_escape_processing():
    # Today's Linux behaviour, byte for byte: backslash is an escape.
    assert split_command(
        "whisper --model /home/me/m.bin", sys_platform="linux"
    ) == ["whisper", "--model", "/home/me/m.bin"]
    assert split_command("espeak -v 'en gb'", sys_platform="darwin") == [
        "espeak",
        "-v",
        "en gb",
    ]
    assert split_command(r"a\ b", sys_platform="linux") == ["a b"]


def test_split_command_preserves_windows_backslashes():
    # posix mode would delete the backslashes of a profile path.
    argv = split_command(
        r'C:\Users\me\bin\tts.exe "C:\Users\me\out.wav"', sys_platform="win32"
    )
    assert argv == [r"C:\Users\me\bin\tts.exe", r"C:\Users\me\out.wav"]
    assert split_command("notepad 'my file.txt'", sys_platform="win32") == [
        "notepad",
        "my file.txt",
    ]


def test_split_command_branch_choice_is_visible_to_a_fake_splitter():
    calls = []

    def fake_split(text, *, posix=True):
        calls.append(posix)
        return [text]

    split_command("anything", sys_platform="win32", split_fn=fake_split)
    split_command("anything", sys_platform="linux", split_fn=fake_split)
    assert calls == [False, True]


def test_default_editor_prefers_visual_then_editor():
    assert default_editor({"VISUAL": "emacs", "EDITOR": "nano"}) == "emacs"
    assert default_editor({"VISUAL": "  ", "EDITOR": "nano"}) == "nano"
    assert default_editor({}) == "vi"
    assert default_editor({"VISUAL": "", "EDITOR": ""}) == "vi"


def test_default_editor_falls_back_per_platform():
    assert default_editor({}, sys_platform="linux") == "vi"
    assert default_editor({}, sys_platform="darwin") == "vi"
    assert default_editor({}, sys_platform="win32") == "notepad"
    # An explicit choice wins over the platform default everywhere.
    assert default_editor({"EDITOR": "code"}, sys_platform="win32") == "code"


def test_harden_private_file_uses_the_real_mode_bit_on_posix(tmp_path):
    path = tmp_path / "stella.token"
    path.write_text("secret", encoding="utf-8")
    result = harden_for("linux", path)
    assert result.applied is True
    assert result.mechanism == "posix-mode-600"
    assert result.note() == ""
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_harden_private_file_does_not_swallow_a_posix_failure(tmp_path):
    missing = tmp_path / "not-here"
    with pytest.raises(OSError):
        portable.harden_private_file(missing, sys_platform="linux")


def harden_for(sys_platform, path):
    return portable.harden_private_file(path, sys_platform=sys_platform)


def test_windows_hardening_grants_the_owners_sid_and_reports_success(tmp_path):
    seen = {}

    def fake_sid_lookup():
        return "*S-1-5-21-1-2-3-500"

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, b"processed: 1", b"")

    result = portable._harden_windows(
        tmp_path / "config.json", sid_lookup=fake_sid_lookup, run=fake_run
    )
    assert result.applied is True
    assert result.mechanism == "windows-acl"
    assert seen["argv"] == [
        "icacls",
        os.fspath(tmp_path / "config.json"),
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-21-1-2-3-500:F",
    ]
    # An argv list, never a shell string, and no shell=True anywhere.
    assert isinstance(seen["argv"], list)
    assert "shell" not in seen["kwargs"]


def test_windows_hardening_says_so_when_icacls_refuses(tmp_path):
    def fake_sid_lookup():
        return "*S-1-5-21-1-2-3-500"

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 5, b"", b"Access is denied.")

    result = portable._harden_windows(
        tmp_path / "t", sid_lookup=fake_sid_lookup, run=fake_run
    )
    assert result.applied is False
    assert "Access is denied" in result.detail
    # The caller-visible addendum must not imply privacy.
    assert result.note().startswith("could not restrict permissions")


def test_windows_hardening_reports_no_mechanism_without_a_sid(tmp_path):
    result = portable._harden_windows(
        tmp_path / "t", sid_lookup=lambda: None, run=None
    )
    assert result.applied is False
    assert result.mechanism == portable.HARDEN_NO_MECHANISM


def test_windows_hardening_reports_when_icacls_is_not_installed(monkeypatch, tmp_path):
    def missing(argv, **kwargs):
        raise FileNotFoundError("icacls")

    result = portable._harden_windows(
        tmp_path / "t", sid_lookup=lambda: "*S-1-5-18", run=missing
    )
    assert result.applied is False
    assert "icacls unavailable" in result.detail


def test_hardening_report_is_the_only_place_success_is_claimed():
    # A Hardening that did nothing can never look like one that worked.
    assert Hardening(False, "none").note() != ""
    assert Hardening(True, "posix-mode-600").note() == ""


def test_sid_lookup_is_absent_not_fatal_off_windows():
    # ctypes.WinDLL does not exist in CPython on Linux. The lookup has to
    # answer None so the caller reports "not hardened" instead of
    # raising out of a file write.
    assert portable._current_user_sid() is None or isinstance(
        portable._current_user_sid(), str
    )


def test_sid_lookup_reports_none_instead_of_raising():
    class Broken:
        def __getattr__(self, name):
            raise OSError("advapi32 is not there")

    assert portable._current_user_sid(advapi=Broken()) is None
