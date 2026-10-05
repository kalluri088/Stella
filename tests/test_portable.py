"""The cross-platform decisions, proven with fakes.

Nothing here pretends to be Windows or macOS by faking ``sys.platform``
globally — that would make the *decision* untestable and the *result*
meaningless on this host. Each function takes the platform as a
parameter or resolves it through one seam, so the branch that runs on
another OS is exercised here even though only Linux can really run it.
"""

import os
import signal
import subprocess

import pytest
from platformdirs.windows import Windows

from stella import portable
from stella.portable import (
    LINUX,
    MACOS,
    OTHER,
    WINDOWS,
    Hardening,
    default_editor,
    platform_name,
    polite_stop,
    split_command,
)

#: platformdirs' supported hook for a Windows user profile. tests/conftest.py
#: and every isolation site in the suite are built on this one name.
WIN_APPDATA = "WIN_PD_OVERRIDE_LOCAL_APPDATA"


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


class _Child:
    """A ``Popen``-shaped recorder: which rungs were asked for, in order.

    ``windows=True`` makes it answer a requested ``SIGINT`` the way a real
    Windows child does — with ``ValueError``, because the platform cannot
    deliver that signal to another process. That is the failure this
    decision exists to avoid, so the tests can be proven on Linux.
    """

    def __init__(self, *, windows: bool = False) -> None:
        self.calls: list[str] = []
        self._windows = windows

    def send_signal(self, signal_number: int) -> None:
        self.calls.append(f"send_signal:{signal_number}")
        if self._windows and signal_number == int(signal.SIGINT):
            raise ValueError(f"Unsupported signal: {signal_number}")

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")


def test_polite_stop_asks_a_posix_child_to_interrupt_itself():
    # Unchanged Linux behaviour, rung for rung: SIGINT is the clean
    # shutdown path a recorder and llama.cpp both read.
    for name in ("linux", "darwin", "freebsd14"):
        child = _Child()
        polite_stop(child, sys_platform=name)
        assert child.calls == [f"send_signal:{int(signal.SIGINT)}"]


def test_polite_stop_never_asks_a_windows_child_to_interrupt_itself():
    child = _Child(windows=True)
    polite_stop(child, sys_platform="win32")
    # terminate() is the closest thing Windows has, and the rung that
    # would raise is never reached at all. That is what the shared cancel
    # ladder needed: on Windows it used to die here, before terminate.
    assert child.calls == ["terminate"]


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


# platformdirs consults the Windows override before it consults the
# platform at all, so these exercise the real Windows resolver rather than
# a fake: the same seam doctrine as the rest of this file — decide the
# platform, do not skip it. test_isolation.py pins the suite's use of the
# variable these three describe.


def _windows_answer() -> str | None:
    """What the Windows resolver answers for Stella's data directory here.

    None off Windows, where there is no profile to read and the resolver says
    so — which is itself part of the proof that XDG was never an option.
    """

    try:
        return Windows(appname="stella", appauthor=False).user_data_dir
    except NotImplementedError:
        return None


def test_xdg_is_not_what_a_windows_install_reads(monkeypatch) -> None:
    # XDG pointing at a scratch directory does not move the Windows answer.
    # Off Windows the resolver refuses outright; on Windows it answers with the
    # caller's real profile. Either way the directory the test meant to isolate
    # is not the one a Windows install used.
    monkeypatch.setenv("XDG_DATA_HOME", "C:\\scratch\\xdg")  # isolation-probe
    monkeypatch.delenv(WIN_APPDATA, raising=False)
    answer = _windows_answer()
    assert answer is None or "scratch" not in answer


def test_the_windows_knob_is_the_one_that_isolates_every_state_directory(
    monkeypatch,
) -> None:
    # A Windows path, so a literal rather than tmp_path: this host has no drive
    # letter to offer, and the resolver wants one either way.
    monkeypatch.setenv(WIN_APPDATA, "C:\\scratch\\profile")
    dirs = Windows(appname="stella", appauthor=False)
    assert dirs.user_data_dir == os.path.join("C:\\scratch\\profile", "stella")
    # One knob covers four: config is the same directory on Windows, and cache
    # and runtime hang off the same CSIDL_LOCAL_APPDATA — which is why the
    # conftest net can be a single variable.
    assert dirs.user_config_dir == dirs.user_data_dir
    assert dirs.user_cache_dir.startswith("C:\\scratch\\profile")
    assert dirs.user_runtime_dir.startswith("C:\\scratch\\profile")


def test_the_windows_knob_ignores_a_path_it_cannot_qualify(monkeypatch) -> None:
    # An override platformdirs cannot qualify is dropped rather than rejected,
    # so a mirror that lost its drive letter would mean "the real profile
    # again", not an error. pytest's tmp_path is always drive-qualified, which
    # is why the sites mirror it instead of assembling a path of their own.
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv(WIN_APPDATA, "relative\\profile")
    answer = _windows_answer()
    assert answer is None or "relative" not in answer
