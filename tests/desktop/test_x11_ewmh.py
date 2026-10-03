"""The X11/EWMH adapter, against the measured surface facts.

Nothing here needs a display: the runner is a fake that answers with the
literal bytes ``xprop`` and ``import`` were measured producing on this
machine's XWayland root (the wording of an unset property, the ``0x`` id
form, the half-written stdout of a dead window id, ImageMagick's
misleading capture error). Those argv-level assertions are what keep the
measurements from being "simplified" away — and what prove no adapter
call ever puts a path, or an unvalidated window id, into a command line.

What these tests cannot prove is said in the adapter's docstring: focus
behaviour, typing and capture-to-stdout are UNVERIFIED, because
``xdotool`` is absent here and no X11 window-manager session was touched.
"""

from collections.abc import Mapping

import pytest

from stella.desktop.backends.x11_ewmh import (
    WINDOW_ID_RE,
    X11Ewmh,
    probe,
)
from stella.desktop.capabilities import (
    MAX_KEY_TEXT_CHARS,
    CaptureUnavailable,
    Region,
    SendUnavailable,
)

DISPLAY = ":1"
X11_ENV = {
    "XDG_SESSION_TYPE": "x11",
    "DISPLAY": DISPLAY,
    "HOME": "/home/someone",
    "XAUTHORITY": "/home/someone/.Xauthority",
    "XDG_RUNTIME_DIR": "/run/user/1000",
}

CHECK_ID = "0x200005"
SPOTIFY_ID = "0xc00004"
BROWSER_ID = "0xa00033"

# The literal line shapes measured with ``xprop -notype``.
def ids_line(name: str, ids: list[str]) -> bytes:
    """``NAME: window id # 0x…, 0x…`` — the measured WINDOW-list spelling."""

    return f"{name}: window id # {', '.join(ids)}\n".encode()


def strings_line(name: str, *fields: str) -> bytes:
    quoted = ", ".join(f'{chr(34)}{field}{chr(34)}' for field in fields)
    return f"{name} = {quoted}\n".encode()


def number_line(name: str, value: int) -> bytes:
    return f"{name} = {value}\n".encode()


def not_found(name: str) -> bytes:
    """An unset *known* property: measured wording, measured rc 0."""

    return f"{name}:  not found.\n".encode()


SPOTIFY = {
    "_NET_WM_NAME": strings_line("_NET_WM_NAME", "M. S. Subbulakshmi - Suprabhatham"),
    "WM_CLASS": strings_line("WM_CLASS", "spotify", "Spotify"),
    "_NET_WM_PID": number_line("_NET_WM_PID", 31990),
    # measured: an xdg-external-mapped client carries no desktop index
    "_NET_WM_DESKTOP": not_found("_NET_WM_DESKTOP"),
}
BROWSER = {
    # measured: this client carried no _NET_WM_PID at all
    "WM_NAME": strings_line("WM_NAME", "Welcome to Stella"),
    "WM_CLASS": not_found("WM_CLASS"),
}
ROOT = {
    "_NET_SUPPORTING_WM_CHECK": ids_line("_NET_SUPPORTING_WM_CHECK", [CHECK_ID]),
    "_NET_ACTIVE_WINDOW": ids_line("_NET_ACTIVE_WINDOW", [SPOTIFY_ID]),
    "_NET_CLIENT_LIST": ids_line("_NET_CLIENT_LIST", [SPOTIFY_ID, BROWSER_ID]),
}
WM_WINDOW = {
    # measured: the check window names itself, which is EWMH's proof
    "_NET_SUPPORTING_WM_CHECK": ids_line("_NET_SUPPORTING_WM_CHECK", [CHECK_ID]),
    "_NET_WM_NAME": strings_line("_NET_WM_NAME", "Hyprland :D"),
}

BADWINDOW_STDERR = b"X Error of failed request:  BadWindow (invalid Window parameter)"


class FakeProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


PNG = b"\x89PNG fake"
TYPED_OK = FakeProcess(0)
# The literal refusal measured on this box's ImageMagick 7.1.2-31 when
# pointed at the XWayland root — with any stdout spelling.
CAPTURE_REFUSED = FakeProcess(
    1, b"import: missing an image filename `png:-' @ error/import.c", b""
)


def x_server(
    root: Mapping[str, bytes],
    windows: Mapping[str, Mapping[str, bytes]],
    *,
    dead: tuple[str, ...] = (),
) -> object:
    """A fake display that answers xprop exactly like the machine did.

    An unlisted property reads ``not found`` at rc 0; a *dead* window id
    reproduces the measured failure — rc 1, the error on stderr and a
    half-written property name on stdout.
    """

    def responder(argv, stdin=None):
        name = argv[-1]
        if argv[2] == "-root":
            line = root.get(name)
        else:
            target = argv[3]
            if target in dead:
                return FakeProcess(1, name[:4].encode(), BADWINDOW_STDERR)
            line = windows.get(target, {}).get(name)
        if line is None:
            return FakeProcess(0, not_found(name), b"")
        return FakeProcess(0, line, b"")

    return responder


class FakeRunner:
    """Answers by argv[0]; a list responder pops in script order."""

    def __init__(self, **responders) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.responders = dict(responders)

    def __call__(self, argv, *, timeout, stdin=None, env=None) -> FakeProcess:
        self.calls.append(
            (tuple(argv), {"timeout": timeout, "stdin": stdin, "env": env})
        )
        key = argv[0]
        if key not in self.responders:
            raise AssertionError(f"unscripted command {argv!r}")
        responder = self.responders[key]
        if callable(responder):
            return responder(tuple(argv), stdin)
        if isinstance(responder, list):
            responder = responder.pop(0)
        return responder

    def argvs(self, binary: str) -> list[tuple]:
        return [argv for argv, _ in self.calls if argv[0] == binary]

    def first(self, binary: str) -> tuple:
        return next(argv for argv, _ in self.calls if argv[0] == binary)

    def env_of(self, binary: str) -> dict:
        return next(kwargs["env"] for argv, kwargs in self.calls if argv[0] == binary)


def _raising(argv, stdin=None):
    """A responder for a binary that vanished since registration."""

    raise OSError("No such file or directory")


def make_x11(
    *,
    root: Mapping[str, bytes] | None = None,
    windows: Mapping[str, Mapping[str, bytes]] | None = None,
    dead: tuple[str, ...] = (),
    xdotool: object | None = None,
    capture: object | None = None,
    display: str | None = DISPLAY,
) -> tuple[X11Ewmh, FakeRunner]:
    """The adapter wired to a fake display, plus the fake itself."""

    runner = FakeRunner(
        xprop=x_server(
            ROOT if root is None else root,
            {CHECK_ID: WM_WINDOW, SPOTIFY_ID: SPOTIFY, BROWSER_ID: BROWSER}
            if windows is None
            else windows,
            dead=dead,
        ),
        xdotool=TYPED_OK if xdotool is None else xdotool,
        **{"import": CAPTURE_REFUSED if capture is None else capture},
    )
    x11 = X11Ewmh.from_environment(
        {**X11_ENV, "DISPLAY": display} if display else {}, runner
    )
    return x11, runner


CAPTURED = FakeProcess(0, PNG, b"")
FULL_WINDOWS = {CHECK_ID: WM_WINDOW, SPOTIFY_ID: SPOTIFY, BROWSER_ID: BROWSER}


# ------------------------------------------------------------- window reads


def test_reads_ask_xprop_by_argv_and_pass_the_display_explicitly() -> None:
    x11, runner = make_x11()
    window = x11.active_window()
    assert window is not None
    assert (window.id, window.class_name, window.title, window.pid) == (
        SPOTIFY_ID,
        "Spotify",
        "M. S. Subbulakshmi - Suprabhatham",
        31990,
    )
    assert window.workspace == ""  # measured: no _NET_WM_DESKTOP on this client
    assert window.region is None  # no geometry source; the tools widen nothing
    assert runner.argvs("xprop")[0] == (
        "xprop",
        "-notype",
        "-root",
        "_NET_ACTIVE_WINDOW",
    )
    assert runner.env_of("xprop")["DISPLAY"] == DISPLAY
    assert runner.env_of("xprop")["XAUTHORITY"] == "/home/someone/.Xauthority"


def test_windows_parses_the_measured_client_list() -> None:
    x11, _ = make_x11()
    windows = x11.windows()
    assert windows is not None
    assert [window.id for window in windows] == [SPOTIFY_ID, BROWSER_ID]
    # WM_CLASS is instance then class (measured), so the class is the last
    # field; a client with no _NET_WM_NAME falls back to WM_NAME.
    assert windows[1].class_name == ""
    assert windows[1].title == "Welcome to Stella"
    assert windows[1].pid == 0  # measured: this hint can simply be absent


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"_NET_CLIENT_LIST:  not found.\n",  # an EWMH-ignorant manager
        b"_NET_CLIENT_LIST:  no such atom on any window.\n",
        b"unknown property\n",  # rc 0 and nothing usable
        b"_NET_CLIENT_LIST: window id # hello\n",  # right shape, foreign id
        rb"\_NET_CLIENT_LIST = \"0xc00004\"\n",  # wrong separator entirely
    ],
)
def test_an_unusable_answer_is_never_an_empty_window_list(stdout) -> None:
    # Obligation 1: xprop answers garbage *at rc 0*, and "no windows are
    # open" is a different fact from "the desktop did not answer".
    x11, _ = make_x11(root={"_NET_CLIENT_LIST": stdout})
    assert x11.windows() is None


def test_a_genuine_empty_client_list_is_no_windows() -> None:
    # Measured twice on this box, once empty and once with one client: the
    # empty form is ``window id #`` with nothing after the marker, and it
    # means "nothing is managed right now" — the honest [].
    x11, _ = make_x11(root={"_NET_CLIENT_LIST": ids_line("_NET_CLIENT_LIST", [])})
    assert x11.windows() == []


def test_a_dead_active_window_id_reads_as_none_not_a_window() -> None:
    # Measured: _NET_ACTIVE_WINDOW named 0xa02259 and xprop then failed on
    # it with BadWindow at rc 1, half a property name on stdout.
    x11, runner = make_x11(
        root={"_NET_ACTIVE_WINDOW": ids_line("_NET_ACTIVE_WINDOW", ["0xa02259"])},
        dead=("0xa02259",),
    )
    assert x11.active_window() is None
    assert runner.argvs("xprop")[1] == (
        "xprop",
        "-notype",
        "-id",
        "0xa02259",
        "_NET_WM_NAME",
    )


def test_the_none_window_is_recognised_and_never_queried() -> None:
    # Measured: no X window has focus answers 0x0, and xprop refuses to
    # query it ("Invalid window id format"), so nothing is spawned for it.
    x11, runner = make_x11(
        root={"_NET_ACTIVE_WINDOW": ids_line("_NET_ACTIVE_WINDOW", ["0x0"])}
    )
    assert x11.active_window() is None
    assert [argv for argv, _ in runner.calls] == [
        ("xprop", "-notype", "-root", "_NET_ACTIVE_WINDOW")
    ]


def test_a_client_the_manager_cannot_describe_refuses_the_whole_list() -> None:
    # Half a trusted list is not a list: it would hide a real window from
    # target resolution, so the answer is None.
    x11, _ = make_x11(dead=(BROWSER_ID,))
    assert x11.windows() is None


# ------------------------------------------------------- focus, then prove


def test_activate_ignores_its_own_success_and_believes_the_re_query() -> None:
    # The command answers rc 0 (and even rc 1) while the focus stays put:
    # some managers honour the request and refuse the change.
    x11, runner = make_x11(
        root={
            "_NET_SUPPORTING_WM_CHECK": ROOT["_NET_SUPPORTING_WM_CHECK"],
            "_NET_ACTIVE_WINDOW": ids_line("_NET_ACTIVE_WINDOW", [BROWSER_ID]),
        },
        xdotool=FakeProcess(0, b"", b""),
    )
    assert x11.activate(SPOTIFY_ID) is False
    assert runner.argvs("xdotool") == [
        ("xdotool", "windowactivate", "--sync", str(int(SPOTIFY_ID, 16)))
    ]
    # the act went out first, and the very next thing read was the focused
    # window: the proof is the re-query, never the command's own reply
    assert runner.calls[0][0][0] == "xdotool"
    assert runner.argvs("xprop")[0] == (
        "xprop",
        "-notype",
        "-root",
        "_NET_ACTIVE_WINDOW",
    )


def test_activate_is_true_only_when_the_re_query_names_the_window() -> None:
    x11, runner = make_x11(xdotool=FakeProcess(1, b"", b"window not found"))
    # A failing command plus a focus that really moved still counts: the
    # re-query is the only proof, in either direction.
    assert x11.activate(SPOTIFY_ID) is True
    assert runner.argvs("xdotool")[0][-1] == "12582916"  # decimal, from the id


def test_a_foreign_window_id_never_reaches_argv() -> None:
    # Obligation 5: the id is the one model-influenced string this adapter
    # interpolates into a command line, so a bad shape spawns nothing.
    for hostile in (
        "0xc00004; windowactivate 1",
        "12582916",
        "0x",
        "0xzzzzzzzz",
        "spotify",
        "0xc00004\n_NET_CLIENT_LIST",
        "",
        "0x1" * 40,
    ):
        x11, runner = make_x11()
        assert x11.activate(hostile) is False
        assert runner.calls == []


def test_a_foreign_id_in_a_client_list_is_not_queried_either() -> None:
    x11, runner = make_x11(
        root={"_NET_CLIENT_LIST": b"_NET_CLIENT_LIST: window id # 0xc00004; rm -rf /\n"}
    )
    assert x11.windows() is None
    assert [argv for argv, _ in runner.calls if argv[3:4] == ["-id"]] == []


def test_activate_refuses_the_none_window_without_spawning() -> None:
    x11, runner = make_x11()
    assert x11.activate("0x0") is False
    assert runner.calls == []


def test_an_uppercase_id_is_canonicalised_before_it_is_used() -> None:
    # The id xprop emits is lowercase hex; a caller that shouted it still
    # reaches the same command and the same re-query comparison.
    x11, runner = make_x11(
        root={"_NET_ACTIVE_WINDOW": ids_line("_NET_ACTIVE_WINDOW", ["0xC00004"])}
    )
    assert x11.activate("0xC00004") is True
    assert runner.argvs("xdotool")[0][-1] == str(int(SPOTIFY_ID, 16))
    assert WINDOW_ID_RE.match("0xC00004")  # the shape tolerates either case


# ------------------------------------------------------------- capture


def test_capture_returns_stdout_png_and_never_names_a_path() -> None:
    x11, runner = make_x11(capture=FakeProcess(0, PNG, b""))
    assert x11.capture(None) == PNG
    argv = runner.first("import")
    assert argv == ("import", "-window", "root", "png:-")
    assert not any("/" in argument for argument in argv)
    assert not any(argument.endswith(".png") for argument in argv)


def test_a_region_uses_imagemagick_geometry_not_grims() -> None:
    x11, runner = make_x11(capture=FakeProcess(0, PNG, b""))
    assert x11.capture(Region(10, 20, 749, 402)) == PNG
    assert runner.first("import") == (
        "import",
        "-window",
        "root",
        "-crop",
        "749x402+10+20",
        "png:-",
    )


@pytest.mark.parametrize(
    "answer",
    [
        # the literal failure measured on this box's ImageMagick 7.1.2-31
        FakeProcess(
            1, b"import: missing an image filename `png:-' @ error/import.c", b""
        ),
        FakeProcess(0, b"not a png at all", b""),  # rc 0 and garbage, again
    ],
)
def test_a_capture_that_is_not_png_raises_without_a_path(answer) -> None:
    x11, runner = make_x11(capture=answer)
    with pytest.raises(CaptureUnavailable) as failure:
        x11.capture(Region(0, 0, 10, 10))
    assert "import" in str(failure.value)
    assert "nothing was saved to disk" in str(failure.value)
    argv = runner.first("import")
    assert argv[-1] == "png:-"
    assert not any("/" in argument for argument in argv)


def test_a_capture_tool_that_cannot_run_raises_instead_of_touching_disk(
    tmp_path, monkeypatch
) -> None:
    # The runner is a fake, so this asserts the argv that *would* have run:
    # no argument is a path, and nothing appeared in the working directory.
    monkeypatch.chdir(tmp_path)
    x11, _ = make_x11(capture=_raising)
    with pytest.raises(CaptureUnavailable) as failure:
        x11.capture(None)
    assert "could not run" in str(failure.value)
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------- typing


def test_send_text_types_literal_text_into_the_focused_surface() -> None:
    x11, runner = make_x11(xdotool=FakeProcess(0))
    assert x11.send_text("echo hi") is None
    assert runner.argvs("xdotool") == [
        ("xdotool", "type", "--clearmodifiers", "echo hi")
    ]


@pytest.mark.parametrize(
    "text",
    ["", "-", "--clearmodifiers", "x" * (MAX_KEY_TEXT_CHARS + 1)],
)
def test_text_that_could_read_as_a_flag_or_a_storm_is_refused(text) -> None:
    x11, runner = make_x11()
    with pytest.raises(SendUnavailable):
        x11.send_text(text)
    assert runner.calls == []


def test_a_failing_xdotool_raises_instead_of_claiming_delivery() -> None:
    x11, _ = make_x11(xdotool=FakeProcess(1, b"xdotool: No such window", b""))
    with pytest.raises(SendUnavailable) as failure:
        x11.send_text("hi")
    assert "xdotool" in str(failure.value)
    # this tool family writes its trouble on stdout, so the message keeps it
    assert "No such window" in str(failure.value)


# ------------------------------------------------------------- fail closed


def test_a_display_less_adapter_never_spawns() -> None:
    runner = FakeRunner()
    x11 = X11Ewmh(None, runner=runner)
    assert x11.active_window() is None
    assert x11.windows() is None
    assert x11.activate(SPOTIFY_ID) is False
    with pytest.raises(CaptureUnavailable):
        x11.capture(None)
    with pytest.raises(SendUnavailable):
        x11.send_text("hi")
    assert runner.calls == []


# ------------------------------------------------------------------ probe


def full_toolchain(name: str) -> str:
    """Every binary this adapter spawns exists; nothing else does."""

    return (
        "/usr/bin/fake"
        if name in {"xprop", "xdotool", "import", "tesseract"}
        else None
    )


def display_runner(
    root: Mapping[str, bytes] = ROOT,
    windows: Mapping[str, Mapping[str, bytes]] = FULL_WINDOWS,
    *,
    capture: object = CAPTURED,
) -> FakeRunner:
    return FakeRunner(
        xprop=x_server(root, windows),
        xdotool=FakeProcess(0),
        **{"import": capture},
    )


def test_probe_needs_the_x11_session_display_and_every_binary() -> None:
    desktop = probe(X11_ENV, display_runner(), which=full_toolchain)
    assert desktop is not None
    assert desktop.name == "x11-ewmh"
    assert DISPLAY in desktop.session_note
    assert "Hyprland :D" in desktop.session_note
    assert desktop.session_note.count("\n") == 0
    for missing in ("xprop", "xdotool", "import", "tesseract"):
        # Focus and typing have no substitute, so one missing tool — the
        # real situation on this laptop, where xdotool and xwd are absent
        # and nothing may be installed — means no tools at all.
        runner = display_runner()
        assert (
            probe(
                X11_ENV,
                runner,
                which=lambda name, _missing=missing: None
                if name == _missing
                else "/usr/bin/fake",
            )
            is None
        )
        assert runner.calls == []


@pytest.mark.parametrize(
    "env",
    [
        {**X11_ENV, "XDG_SESSION_TYPE": "wayland"},
        {**X11_ENV, "XDG_SESSION_TYPE": ""},
        {k: v for k, v in X11_ENV.items() if k != "XDG_SESSION_TYPE"},
        {k: v for k, v in X11_ENV.items() if k != "DISPLAY"},
        # the trap: a stale XDG_SESSION_TYPE=x11 in a shell profile while
        # the user is typing into a Wayland compositor whose XWayland
        # answers xprop perfectly well
        {**X11_ENV, "WAYLAND_DISPLAY": "wayland-1"},
        {**X11_ENV, "HYPRLAND_INSTANCE_SIGNATURE": "sig_1_2"},
        {**X11_ENV, "SWAYSOCK": "/tmp/sway.sock"},
    ],
)
def test_probe_refuses_anything_that_is_not_a_plain_x11_session(env) -> None:
    runner = display_runner()
    assert probe(env, runner, which=full_toolchain) is None
    # refusing a session costs no spawn: the veto is cheaper than a query,
    # and a query could reach a compositor the user is not in
    assert runner.calls == []


@pytest.mark.parametrize(
    "root",
    [
        {},  # no EWMH property at all: the fake display says "not found"
        {"_NET_SUPPORTING_WM_CHECK": not_found("_NET_SUPPORTING_WM_CHECK")},
        {"_NET_SUPPORTING_WM_CHECK": ids_line("_NET_SUPPORTING_WM_CHECK", [])},
        {"_NET_SUPPORTING_WM_CHECK": b"garbage\n"},
        {"_NET_SUPPORTING_WM_CHECK": ids_line(
            "_NET_SUPPORTING_WM_CHECK", ["no-window"]
        )},
        {"_NET_SUPPORTING_WM_CHECK": ids_line("_NET_SUPPORTING_WM_CHECK", ["0x0"])},
    ],
)
def test_probe_only_believes_an_ewmh_reply(root) -> None:
    # XDG_SESSION_TYPE=x11 and $DISPLAY are the hint; the check window
    # answering is the proof, and every unusable reply means no tools.
    assert probe(X11_ENV, display_runner(root=root), which=full_toolchain) is None


def test_probe_refuses_a_check_window_that_does_not_check_itself() -> None:
    # EWMH's proof is self-reference: an id that names a *different* window
    # (or nothing) is not a manager this adapter will act through.
    windows = {CHECK_ID: {"_NET_WM_NAME": strings_line("_NET_WM_NAME", "Liar")}}
    assert probe(X11_ENV, display_runner(windows=windows), which=full_toolchain) is None


def test_probe_refuses_a_check_window_that_names_no_manager() -> None:
    windows = {
        CHECK_ID: {
            "_NET_SUPPORTING_WM_CHECK": ids_line(
                "_NET_SUPPORTING_WM_CHECK", [CHECK_ID]
            )
        }
    }
    assert probe(X11_ENV, display_runner(windows=windows), which=full_toolchain) is None


def test_probe_refuses_a_dead_check_window_id() -> None:
    # Measured shape of a reply that names a window the server rejects.
    runner = display_runner(windows={})
    assert probe(X11_ENV, runner, which=full_toolchain) is None


def test_the_manager_name_is_flattened_and_bounded() -> None:
    # The manager name is server-supplied text on its way into an approval
    # dialog, so it can neither smuggle a line nor run unbounded. xprop
    # escapes an embedded newline as ``\n`` inside the quotes, which is the
    # byte shape faked here.
    evil = b'_NET_WM_NAME = "Evil ' + b"x" * 200 + b'\\nsecond line"\n'
    windows = {
        CHECK_ID: {
            "_NET_SUPPORTING_WM_CHECK": ids_line(
                "_NET_SUPPORTING_WM_CHECK", [CHECK_ID]
            ),
            "_NET_WM_NAME": evil,
        }
    }
    desktop = probe(X11_ENV, display_runner(windows=windows), which=full_toolchain)
    assert desktop is not None
    assert "\n" not in desktop.session_note
    assert "\\n" not in desktop.session_note  # unescaped, then flattened
    assert "…" in desktop.session_note
    assert len(desktop.session_note) < 120


def test_no_adapter_call_writes_a_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    x11, runner = make_x11(capture=FakeProcess(0, PNG, b""))
    desktop = probe(X11_ENV, display_runner(), which=full_toolchain)
    assert desktop is not None
    assert x11.windows() is not None
    assert x11.active_window() is not None
    assert x11.capture(Region(0, 0, 8, 8)) == PNG
    assert x11.activate(SPOTIFY_ID) is True
    x11.send_text("hi")
    assert list(tmp_path.iterdir()) == []
    # the whole session ran on argv alone: no argument anywhere was a path
    for argv, _ in runner.calls:
        assert argv[0] in {"xprop", "xdotool", "import"}
        assert all(not argument.startswith("/") for argument in argv[1:])
