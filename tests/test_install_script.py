"""`install.sh` — the one command the owner asked for, and what it refuses to do.

The script is the machine's front door: it downloads code and installs it.
These tests pin the two properties that make that acceptable — a wheel is
installed only when it matches its published checksum, and the script stays
usable as `curl … | sh` on a system whose `/bin/sh` is not bash — plus the
scope limit the owner set: Python side only, no privilege escalation, no
system packages, no config edits.
"""

import hashlib
import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "install.sh"


def _wheel(directory: Path, *, tamper: bool = False) -> Path:
    """A file named like a release wheel, plus the checksum line beside it.

    The script never opens the wheel in check mode — it copies and hashes it —
    so a real build is not needed to prove the gate works, and
    ``hashlib`` keeps the test portable instead of depending on which of
    sha256sum or shasum a platform happens to have.
    """

    wheel = directory / "stella-9.9.9-py3-none-any.whl"
    wheel.write_bytes(b"not really a wheel, and that is fine here")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if tamper:
        digest = "0" * 64
    Path(str(wheel) + ".sha256").write_text(f"{digest}  {wheel.name}\n")
    return wheel


def _run(**env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, **env},
    )


def test_a_wheel_that_matches_its_checksum_is_accepted(tmp_path) -> None:
    wheel = _wheel(tmp_path)
    result = _run(STELLA_WHEEL=str(wheel), STELLA_CHECK="1")
    assert result.returncode == 0, result.stderr
    assert "checksum verified" in result.stdout
    assert "stella[wake,barge-in,web]" in result.stdout


def test_a_wheel_that_does_not_match_is_refused(tmp_path) -> None:
    # The whole point of publishing a checksum beside a release: a mismatch
    # stops the install rather than warning about it.
    wheel = _wheel(tmp_path, tamper=True)
    result = _run(STELLA_WHEEL=str(wheel), STELLA_CHECK="1")
    assert result.returncode != 0
    assert "checksum" in result.stderr
    assert "STELLA_CHECK is set" not in result.stdout


def test_minimal_drops_every_extra_and_embed_adds_one(tmp_path) -> None:
    wheel = _wheel(tmp_path)
    minimal = _run(STELLA_WHEEL=str(wheel), STELLA_CHECK="1", STELLA_MINIMAL="1")
    assert minimal.returncode == 0, minimal.stderr
    assert "'stella'" in minimal.stdout
    embedded = _run(STELLA_WHEEL=str(wheel), STELLA_CHECK="1", STELLA_EMBED="1")
    assert "'stella[wake,barge-in,web,embed]'" in embedded.stdout


def test_a_missing_wheel_is_an_error_not_a_download(tmp_path) -> None:
    result = _run(STELLA_WHEEL=str(tmp_path / "absent.whl"))
    assert result.returncode != 0
    assert "no such wheel" in result.stderr


def test_the_script_stays_posix_sh_compatible() -> None:
    # `curl … | sh` runs under dash on Debian and Ubuntu, where one bashism
    # is a broken install command for most users.
    syntax = subprocess.run(
        ["sh", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr
    script = SCRIPT.read_text("utf-8")
    for bashism in ("[[", "<<<", "mapfile", "readarray", "shopt"):
        assert bashism not in script, bashism


def test_the_installer_never_reaches_for_the_machine() -> None:
    # The scope the owner set for this script: install the Python side and
    # report the rest. Nothing here escalates, packages or pokes the
    # compositor — the voice and model pieces come back as doctor lines and
    # a shortcut snippet the human is asked to add by hand.
    script = SCRIPT.read_text("utf-8")
    # "no sudo" is something the script says about itself; what must not
    # appear is sudo in command position.
    assert not re.search(r"(^|[;&|]\s*)sudo\s", script, re.MULTILINE), script
    for forbidden in ("apt-get", "pacman", "dnf", "brew", "hyprctl"):
        assert forbidden not in script, forbidden
    assert "stella doctor" in script
