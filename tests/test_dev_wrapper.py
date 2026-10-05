"""The dev wrapper's promise: this tree runs against this tree's state.

An installed release and a working tree are the same program reading the same
XDG directories and the same voice socket, which means testing a change here
would otherwise write into the memories of the Stella actually in use. That is
a laptop-safety property, not a convenience, so it is pinned here rather than
assumed from a comment in a shell script.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "bin" / "stella-dev"
POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="a bash wrapper for POSIX launches"
)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(WRAPPER), *args],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_the_wrapper_exists_and_is_executable() -> None:
    assert WRAPPER.is_file()
    assert os.access(WRAPPER, os.X_OK), "bin/stella-dev must be executable"


def test_the_wrapper_fails_loudly_and_sets_every_state_path() -> None:
    # `set -euo pipefail` is what stops a half-resolved path from quietly
    # sending the tree back at the owner's real data directory.
    script = WRAPPER.read_text("utf-8")
    assert "set -euo pipefail" in script
    for declaration in (
        'export XDG_DATA_HOME="$repo/.dev/share"',
        'export XDG_CONFIG_HOME="$repo/.dev/config"',
        'export STELLA_VOICE_SOCKET="$repo/.dev/stella-voice.sock"',
    ):
        assert declaration in script
    # XDG_RUNTIME_DIR must NOT be redirected: PipeWire and Wayland find their
    # sockets there, so a fake value breaks the microphone and the window.
    assert "export XDG_RUNTIME_DIR" not in script


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_the_state_directory_is_ignored_at_any_depth() -> None:
    probe = REPO_ROOT / "src" / ".dev" / "share" / "stella_memory.db"
    listed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", str(probe)],
        check=False,
    ).returncode
    assert listed == 0, ".dev/ must be ignored, and unanchored"


@POSIX_ONLY
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_the_wrapper_runs_the_tree_against_the_tree_not_the_owner(tmp_path) -> None:
    # The end-to-end form of the isolation rule: ask the wrapper's own Stella
    # where she keeps her state, and every answer must live inside the repo's
    # .dev directory rather than the real ~/.local/share/stella.
    home = tmp_path / "home"
    (home / ".local" / "share").mkdir(parents=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    # A clean-room XDG_RUNTIME_DIR would defeat the point of the check, and
    # the wrapper's child needs the real one only to look, not to connect.
    env.pop("XDG_DATA_HOME", None)
    env.pop("XDG_CONFIG_HOME", None)
    result = subprocess.run(
        [str(WRAPPER), "doctor", "--json"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    checks = json.loads(result.stdout)["checks"]
    paths = [check["detail"] for check in checks if check["group"] == "state"]
    assert paths, "doctor reported no state at all"
    for detail in paths:
        assert "/.dev/" in detail, detail
        assert str(home) not in detail, detail
    # and nothing was created, for the tree or for pretend-owner state
    assert not (REPO_ROOT / ".dev").exists()
    assert list((home / ".local" / "share").iterdir()) == []
