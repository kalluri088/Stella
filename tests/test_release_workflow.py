"""The contract between `install.sh` and the workflows that ship it.

Neither side can see the other: the installer names the assets it downloads,
and the workflow names the assets it uploads. A rename on one side is a broken
install command that only shows up on a user's machine, so the names are pinned
against each other here instead of left to match by habit — along with the
other two things that fail silently: which command the README tells people to
copy, and whether the job that runs the tests gave the window suite a display
to run on.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = (REPO_ROOT / "install.sh").read_text("utf-8")
RELEASE = (REPO_ROOT / ".github/workflows/release.yml").read_text("utf-8")
CI = (REPO_ROOT / ".github/workflows/ci.yml").read_text("utf-8")
README = (REPO_ROOT / "README.md").read_text("utf-8")

# install.sh hardcodes the repository it downloads from, and everything else
# that mentions the front door — the README, CI, the release job — has to name
# the same one. A transfer or a rename moves the repo and every one of those
# strings together, or the first command a new user copies starts 404ing. The
# URL is built from the installer's own line rather than repeated four times
# here, so it cannot drift into agreeing with itself.
REPO = re.search(r'^REPO="([^"]+)"', INSTALL, re.MULTILINE).group(1)
INSTALL_URL = f"https://raw.githubusercontent.com/{REPO}/HEAD/install.sh"


def test_the_documented_command_is_the_tested_command() -> None:
    assert INSTALL_URL in README, "the README's install line must be the real one"
    assert INSTALL_URL in INSTALL


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
    # install itself short-circuited so this step costs no resolver run.
    assert "STELLA_WHEEL" in CI and "STELLA_CHECK=1" in CI


def test_ci_also_installs_from_the_url_a_stranger_copies() -> None:
    # The step above can only ever see the runner's own build. What a user
    # gets is the published release, reached through the raw URL — a private
    # repository, a moved default branch or a wheel uploaded without its
    # checksum are all invisible to the local-wheel check and obvious here.
    assert INSTALL_URL in CI, "CI must fetch the same command the README prints"
    assert "STELLA_MINIMAL=1 sh /tmp/install.sh" in CI
    # And it compares what installed against the release the tag redirect
    # names, not against the checkout: master is allowed to be ahead of the
    # published build without making this step a lie.
    assert 'test "$("$stella" --version)" = "stella $released"' in CI


def test_a_release_proves_the_published_command_installs_after_publishing() -> None:
    published = RELEASE.index("name: Publish the release")
    proved = RELEASE.index("name: Prove the published release installs")
    assert published < proved
    assert INSTALL_URL in RELEASE
    # Pinned to the version this tag just released, because /releases/latest
    # is a race with whoever else pushed a tag minutes ago.
    assert 'STELLA_VERSION="$VERSION" STELLA_MINIMAL=1' in RELEASE
    assert 'test "$("$stella" --version)" = "stella $VERSION"' in RELEASE


def test_the_window_suite_is_given_a_display_it_can_actually_run_on() -> None:
    # Without an X server every Tk test skips and the job still reports
    # success — which is how 74 window tests went unrun for the whole life of
    # this workflow. So Linux gets a display, and a skip is a failure.
    assert "xvfb-run -a uv run pytest" in CI
    assert "*skipped*)" in CI, "the no-skip guard must stay"
    # macOS and Windows CI have no GUI session to join, so the window tests
    # skip there by choice instead of crashing the interpreter mid-run; the
    # gate is CI-and-platform, never a bare platform check.
    ui = (REPO_ROOT / "tests/test_ui.py").read_text("utf-8")
    assert 'os.environ.get("GITHUB_ACTIONS") == "true"' in ui
    assert 'sys.platform in ("darwin", "win32")' in ui
