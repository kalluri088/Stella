"""The contract between `install.sh` and the workflow that publishes a release.

Neither side can see the other: the installer names the assets it downloads,
and the workflow names the assets it uploads. A rename on one side is a broken
install command that only shows up on a user's machine, so the names are pinned
against each other here instead of left to match by habit.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = (REPO_ROOT / "install.sh").read_text("utf-8")
RELEASE = (REPO_ROOT / ".github/workflows/release.yml").read_text("utf-8")
CI = (REPO_ROOT / ".github/workflows/ci.yml").read_text("utf-8")


def test_the_installer_and_the_workflow_agree_on_asset_names() -> None:
    # install.sh builds the name from the version and asks GitHub for
    # <wheel> and <wheel>.sha256; release.yml must write out exactly those
    # two. Literal strings on purpose: this is the seam that breaks silently.
    assert 'wheel="${WHEEL_BASE}-${version}-py3-none-any.whl"' in INSTALL
    assert '"stella-${VERSION}-py3-none-any.whl"' in RELEASE
    assert '> "stella-${VERSION}-py3-none-any.whl.sha256"' in RELEASE
    # The line it writes has to be the shape both `sha256sum -c` and the
    # installer's parser expect: "<hash>  <filename>".
    assert "sha256sum " in RELEASE


def test_a_release_never_moves_a_tag() -> None:
    # Standing rule: v1.0.0 … v1.4.0 are published history, and a wheel
    # installed by checksum has to stay the bytes it was published as.
    assert 'tags:\n      - "v*"' in RELEASE
    assert "--verify-tag" in RELEASE
    for forbidden in ("git tag -d", "git push --force", "push -f", "gh release delete"):
        assert forbidden not in RELEASE, forbidden


def test_a_release_proves_the_wheel_installs_before_publishing_it() -> None:
    # v1.4.0 shipped a wheel no job had ever opened. The order is the point:
    # the clean-install proof comes before the upload step.
    installed = RELEASE.index("Install the wheel in a clean environment")
    published = RELEASE.index("name: Publish the release")
    assert installed < published
    assert "uv pip install --python /tmp/released/bin/python" in RELEASE
    assert "stella doctor --json" in RELEASE


def test_the_tag_is_narrowed_to_a_version_before_it_reaches_a_shell() -> None:
    # A ref name is input someone with push access controls. It is read from
    # the environment rather than interpolated, and then restricted to digits
    # and dots, because later steps do expand it into a command.
    assert "${{ github.ref_name }}" not in RELEASE
    assert 'tag="${GITHUB_REF_NAME}"' in RELEASE
    assert "*[!0-9.]*)" in RELEASE


def test_ci_installs_the_artifact_it_builds() -> None:
    assert "package:" in CI
    assert "uv build --out-dir dist" in CI
    # The installer is exercised against the wheel CI just built, with the
    # install itself short-circuited so CI never touches a real tool dir.
    assert "STELLA_WHEEL" in CI and "STELLA_CHECK=1" in CI
