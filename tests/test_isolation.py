"""The test suite's own isolation rule, checked rather than remembered.

Every test that gives Stella a scratch state directory says so by setting
``XDG_DATA_HOME``. That is the right knob on Linux and the wrong one on
Windows, where platformdirs ignores it and ``user_data_dir`` answers with the
caller's real ``%LOCALAPPDATA%`` — so a site that sets only XDG passes while
writing config.json, the databases and other tests' API keys into a user's
profile. The mirror line is inert on Linux, which is exactly why it is easy to
forget: forgetting costs nothing here and everything there.

A shared helper would be the usual answer, and it is bypassable — a test can
always call ``monkeypatch.setenv`` directly. So the rule is pinned against the
source instead: the scan below is the thing that fails when a site forgets.

One line in the suite is deliberately not an isolation site: the probe in
``test_portable.py`` that sets ``XDG_DATA_HOME`` precisely to show it isolates
nothing on Windows. It declares itself with ``# isolation-probe``, so the
exemption is visible in review instead of granted by silence.
"""

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
WIN_APPDATA = "WIN_PD_OVERRIDE_LOCAL_APPDATA"

SITE = re.compile(
    r'^\s*monkeypatch\.setenv\("XDG_DATA_HOME", (.+)\)\s*(?:#.*)?$'
)
MIRROR = re.compile(rf'^\s*monkeypatch\.setenv\("{WIN_APPDATA}", (.+)\)\s*$')
# Any trailing comment would be a loophole, so the exemption is a specific
# string and nothing else.
EXEMPT = "# isolation-probe"


def _sites() -> list[tuple[Path, int, str]]:
    """Every XDG isolation line the rule applies to, with its expression."""

    return [
        (path, number, expression)
        for path, number, expression, exempted in _all_sites()
        if not exempted
    ]


def _all_sites() -> list[tuple[Path, int, str, bool]]:
    found = []
    for path in sorted(TESTS.rglob("test_*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if (site := SITE.match(line)) is not None:
                found.append((path, number, site.group(1), EXEMPT in line))
    return found


def test_the_one_exemption_is_the_probe_and_nothing_else() -> None:
    # A declared exemption is a rule with an exception; an undeclared one is a
    # hole. The marker has to be this specific string, in this one file, so a
    # second one arrives with a conversation about it rather than with a
    # comment.
    exempt = {path.name for path, _, _, on in _all_sites() if on}
    assert exempt == {"test_portable.py"}


def test_every_isolation_site_also_isolates_the_windows_knob() -> None:
    misses = []
    for path, number, expression in _sites():
        lines = path.read_text(encoding="utf-8").splitlines()
        following = lines[number] if number < len(lines) else ""
        mirror = MIRROR.match(following)
        # The same base, not merely any base: a mirror pointed somewhere else
        # isolates a different directory and leaves the assertions wrong.
        if mirror is None or mirror.group(1) != expression:
            misses.append(f"{path.name}:{number}")
    assert misses == [], (
        "these sites isolate XDG only, so on Windows they write into the "
        "real user profile: " + ", ".join(misses)
    )


def test_the_scan_is_not_passing_because_it_found_nothing() -> None:
    # A scanner that matches zero lines is a green light with no meaning, so
    # the population it checks is pinned as well. Changing the idiom in the
    # tests is the only way this fails, and that is the moment the check above
    # stops being a check.
    assert len(_sites()) >= 40, (
        f"only {len(_sites())} isolation sites scanned — the idiom moved"
    )


def test_the_session_net_covers_the_tests_that_never_isolated() -> None:
    # conftest.py is the safety net for code that resolves a default state
    # directory with no test steering it. It has to be session-scoped and
    # autouse, or it is a fixture nobody gets.
    conftest = (TESTS / "conftest.py").read_text(encoding="utf-8")
    assert 'scope="session", autouse=True' in conftest
    assert WIN_APPDATA in conftest
    assert "tmp_path_factory" in conftest
