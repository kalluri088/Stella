"""Containment guard: the two local-only names stay uncommittable, any depth.

stella_workspace/ (the app's own file-tool workspace) and SECURITY-AUDIT.md
must never be committed (standing rule). This test pins the .gitignore
patterns themselves — anchoring them to the root would let a nested copy
slip through — so drift is caught here rather than in a leak.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINED_NAMES = ("stella_workspace/", "SECURITY-AUDIT.md")


def _patterns() -> list[str]:
    lines = (REPO_ROOT / ".gitignore").read_text("utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


@pytest.mark.parametrize("name", CONTAINED_NAMES)
def test_containment_pattern_is_present_and_unanchored(name: str) -> None:
    patterns = _patterns()
    assert name in patterns, f"{name} missing from .gitignore"
    # An anchored pattern (/name) only guards the repo root.
    assert not name.startswith("/"), f"{name} must match at any depth"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.parametrize(
    "probe",
    [
        "SECURITY-AUDIT.md",
        "stella_workspace/notes.txt",
        "some/dir/SECURITY-AUDIT.md",
        "some/dir/stella_workspace/file.txt",
    ],
)
def test_git_actually_ignores_the_probe_paths(probe: str) -> None:
    # check-ignore matches patterns without needing the path to exist.
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"git does not ignore {probe!r}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_contained_names_are_not_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    for line in tracked:
        parts = line.split("/")
        assert "stella_workspace" not in parts, f"{line} is tracked"
        assert "SECURITY-AUDIT.md" not in parts, f"{line} is tracked"
