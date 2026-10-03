"""The Hyprland adapter, against the measured surface facts.

Research reports 03/11/13 are full of traps a fake can too easily paper
over, so the fake compositor answers exactly like the machine did: garbage
with rc 0, errors on stdout, and focus through this session's Lua dispatch
API (the classic ``dispatch focuswindow "(address:0x…)"`` string form the
reports measured is rejected by Omarchy 0.56.2). Nothing here needs a
desktop — the runner is injected, and these are the argv-level assertions
that keep the measurements from being "simplified" away.
"""

import json

import pytest

from stella.desktop.backends.hyprland import WINDOW_ID_RE, Hyprland, probe
from stella.desktop.capabilities import (
    OCR_SCREEN,
    OCR_WINDOW,
    CaptureUnavailable,
    RecognizeUnavailable,
    Region,
    SendUnavailable,
)
from stella.desktop.ocr import TesseractRecognizer

SIGNATURE = "sig_1_2"

ACTIVE_FOOT = {
    "address": "0x1234abcd",
    "class": "foot",
    "title": "Shell - ~",
    "pid": 4001,
    "workspace": {"id": 2},
    "at": [10, 20],
    "size": [749, 402],
    "floating": False,
}
CLIENTS = [
    {
        "address": "0x1234abcd",
        "class": "foot",
        "title": "Shell - ~",
        "pid": 4001,
        "workspace": {"id": 2},
    },
    {
        "address": "0xfeedface",
        "class": "firefox",
        "title": "Error page - Mozilla Firefox",
        "pid": 55,
        "workspace": {"id": 1},
    },
]


def window_of(entry: dict) -> dict:
    """The client-list projection of a window record."""

    keys = {"address", "class", "title", "pid", "workspace"}
    return {key: value for key, value in entry.items() if key in keys}


class FakeProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Answers by argv prefix; a list responder pops in script order."""

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

    def argvs(self) -> list[tuple]:
        return [argv for argv, _ in self.calls]

    def hyprctl(self) -> list[tuple]:
        return [argv for argv, _ in self.calls if argv[0] == "hyprctl"]

    def typed(self) -> list[tuple]:
        return [argv for argv, _ in self.calls if argv[0] == "wtype"]

    def first(self, binary: str) -> tuple:
        return next(argv for argv, _ in self.calls if argv[0] == binary)

    def env_of(self, binary: str) -> dict:
        return next(
            kwargs["env"] for argv, kwargs in self.calls if argv[0] == binary
        )


def json_process(payload) -> FakeProcess:
    return FakeProcess(0, json.dumps(payload).encode("utf-8"), b"")


def clients_process() -> FakeProcess:
    return json_process(CLIENTS)


def active_process(entry: dict) -> FakeProcess:
    return json_process(window_of(entry))


def is_focus_call(argv: tuple) -> bool:
    """The Lua focus dispatch, not a plain ``-j`` read."""

    return "eval" in argv and any("hl.dsp.focus" in part for part in argv)


PNG = b"\x89PNG fake"


def make_hyprland(responders, *, signature: str | None = SIGNATURE):
    """The adapter wired to the fake runner plus the fake itself."""

    runner = FakeRunner(**responders)
    if signature is None:
        hyprland = Hyprland.from_environment({})
    else:
        hyprland = Hyprland.from_environment(
            {
                "HYPRLAND_INSTANCE_SIGNATURE": signature,
                "WAYLAND_DISPLAY": "wayland-1",
                "XDG_RUNTIME_DIR": "/run/user/1000",
            }
        )
    wired = Hyprland(
        hyprland.signature,
        wayland_display=hyprland.wayland_display,
        xdg_runtime_dir=hyprland.xdg_runtime_dir,
        runner=runner,
    )
    return wired, runner


# ------------------------------------------------------------- window reads


def test_active_window_parses_the_measured_shape() -> None:
    hyprland, runner = make_hyprland({"hyprctl": json_process(ACTIVE_FOOT)})
    window = hyprland.active_window()
    assert window is not None
    assert (window.id, window.class_name, window.pid) == ("0x1234abcd", "foot", 4001)
    assert window.workspace == "2"
    assert window.region == Region(10, 20, 749, 402)
    argv = runner.hyprctl()[0]
    assert argv[:3] == ("hyprctl", "-i", SIGNATURE)  # rule 4: explicit instance
    assert argv[-2:] == ("-j", "activewindow")


@pytest.mark.parametrize(
    "garbage",
    [
        b"unknown request\n",  # the rc=0 trap, verbatim from report 03
        b"",
        b"[not json",
        b'{"address": "hello"}',  # right JSON, wrong shape
        b'{"address": "0x1", "class": "c", "title": "t", "pid": 1}',  # no workspace
    ],
)
def test_reads_refuse_unshaped_answers_even_at_rc_zero(garbage) -> None:
    hyprland, _ = make_hyprland({"hyprctl": FakeProcess(0, garbage, b"")})
    assert hyprland.active_window() is None
    assert hyprland.windows() is None


def test_windows_parses_the_measured_shape() -> None:
    hyprland, _ = make_hyprland({"hyprctl": json_process(CLIENTS)})
    windows = hyprland.windows()
    assert windows is not None
    assert [window.id for window in windows] == ["0x1234abcd", "0xfeedface"]


def test_an_empty_client_list_is_no_windows_not_an_unusable_answer() -> None:
    # None means "the answer was unusable"; [] means "nothing is open".
    # Collapsing them is the trap the reader contract exists to prevent.
    hyprland, _ = make_hyprland({"hyprctl": json_process([])})
    assert hyprland.windows() == []


def test_a_window_without_geometry_keeps_its_id_and_reports_no_region() -> None:
    hyprland, _ = make_hyprland({"hyprctl": json_process(window_of(ACTIVE_FOOT))})
    window = hyprland.active_window()
    assert window is not None and window.region is None


# ------------------------------------------------------- focus, then prove


def test_activate_uses_the_lua_dispatch_api_and_re_queries() -> None:
    entry = window_of(CLIENTS[1])
    hyprland, runner = make_hyprland(
        {"hyprctl": [FakeProcess(0, b"ok\n", b""), json_process(entry)]}
    )
    assert hyprland.activate("0xfeedface") is True
    eval_call = runner.hyprctl()[0]
    assert eval_call[:4] == ("hyprctl", "-i", SIGNATURE, "eval")
    lua = eval_call[-1]
    assert "hl.get_windows()" in lua  # resolves the window object…
    assert "hl.dsp.focus" in lua  # …and focuses it through the Lua API
    assert "0xfeedface" in lua  # the exact, validated id
    assert "focuswindow" not in lua  # the rejected classic string form
    assert runner.hyprctl()[1][-1] == "activewindow"  # verified, not trusted


def test_activate_denied_by_the_compositor_reads_as_false() -> None:
    hyprland, _ = make_hyprland(
        {
            "hyprctl": [
                json_process(window_of(CLIENTS[1])),
                json_process(window_of(CLIENTS[0])),
            ]
        }
    )
    assert hyprland.activate("0xfeedface") is False


def test_activate_rejects_a_foreign_id_shape_without_spawning() -> None:
    # The id reaches a Lua chunk, so shape validation lives in the
    # adapter: a value that is not a Hyprland address never gets near it.
    hyprland, runner = make_hyprland({"hyprctl": clients_process()})
    assert hyprland.activate("0x1234abcd; dispatch exec x") is False
    assert hyprland.activate("foot") is False
    assert runner.calls == []


# ------------------------------------------------------------ fail closed


def test_a_missing_signature_fails_closed_without_spawning() -> None:
    runner = FakeRunner()
    hyprland = Hyprland(None, runner=runner)
    assert hyprland.active_window() is None
    assert hyprland.windows() is None
    assert hyprland.activate("0x1") is False
    with pytest.raises(CaptureUnavailable):
        hyprland.capture(None)
    with pytest.raises(SendUnavailable):
        hyprland.send_text("hi")
    assert runner.calls == []


# ------------------------------------------------------------ capture/OCR


def test_capture_formats_the_region_for_this_grim_and_passes_the_session() -> None:
    hyprland, runner = make_hyprland({"grim": FakeProcess(0, PNG, b"")})
    assert hyprland.capture(Region(10, 20, 749, 402)) == PNG
    assert runner.first("grim") == ("grim", "-g", "10,20 749x402", "-")
    env = runner.env_of("grim")
    assert env["WAYLAND_DISPLAY"] == "wayland-1"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"


def test_full_screen_capture_is_grim_with_only_the_stdout_target() -> None:
    hyprland, runner = make_hyprland({"grim": FakeProcess(0, PNG, b"")})
    assert hyprland.capture(None) == PNG
    assert runner.first("grim") == ("grim", "-")


@pytest.mark.parametrize(
    "answer",
    [
        FakeProcess(1, b"", b"failed to capture"),
        FakeProcess(0, b"not a png", b""),  # rc 0 and garbage, again
    ],
)
def test_a_non_png_capture_raises_one_safe_message(answer) -> None:
    hyprland, runner = make_hyprland({"grim": answer})
    with pytest.raises(CaptureUnavailable) as failure:
        hyprland.capture(Region(0, 0, 10, 10))
    assert "grim" in str(failure.value)
    assert runner.first("grim")[-1] == "-"  # PNG to stdout, never a file


def test_recognizer_carries_the_measured_tesseract_arguments() -> None:
    runner = FakeRunner(tesseract=FakeProcess(0, b"read text\n", b""))
    recognizer = TesseractRecognizer(runner)
    assert recognizer.recognize(PNG, OCR_WINDOW) == "read text"
    assert runner.argvs()[0] == ("tesseract", "-", "stdout", "--psm", "6")
    assert runner.calls[0][1]["stdin"] == PNG


def test_the_sparse_screen_hint_buys_the_slower_page_segmentation_mode() -> None:
    # Report 11: a window is one uniform block (--psm 6); a whole desktop
    # is scattered text and needs --psm 11, at about 9 s.
    runner = FakeRunner(tesseract=FakeProcess(0, b"scattered\n", b""))
    assert TesseractRecognizer(runner).recognize(PNG, OCR_SCREEN) == "scattered"
    assert runner.argvs()[0] == ("tesseract", "-", "stdout", "--psm", "11")


def test_a_failing_tesseract_raises_one_safe_message() -> None:
    recognizer = TesseractRecognizer(
        FakeRunner(tesseract=FakeProcess(1, b"Error opening data file", b""))
    )
    with pytest.raises(RecognizeUnavailable) as failure:
        recognizer.recognize(PNG, OCR_WINDOW)
    assert "OCR failed" in str(failure.value)
    # report 03 rule 2: this family of tools writes its trouble on stdout
    assert "Error opening data file" in str(failure.value)


# ------------------------------------------------------------------ typing


def test_send_text_uses_wtype_on_the_focused_surface() -> None:
    hyprland, runner = make_hyprland({"wtype": FakeProcess(0)})
    assert hyprland.send_text("echo hi") is None
    assert runner.typed()[0] == ("wtype", "echo hi")


def test_a_failing_wtype_raises_instead_of_claiming_delivery() -> None:
    hyprland, _ = make_hyprland({"wtype": FakeProcess(1, b"", b"not a keyboard")})
    with pytest.raises(SendUnavailable) as failure:
        hyprland.send_text("hi")
    assert "wtype" in str(failure.value)


# ------------------------------------------------------------------- probe


def test_probe_needs_the_session_and_every_binary_it_will_spawn() -> None:
    env = {
        "HYPRLAND_INSTANCE_SIGNATURE": SIGNATURE,
        "WAYLAND_DISPLAY": "wayland-1",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    desktop = probe(env, FakeRunner(), which=lambda _name: "/usr/bin/fake")
    assert desktop is not None
    assert desktop.name == "hyprland"
    assert SIGNATURE in desktop.session_note
    assert probe({}, FakeRunner(), which=lambda _name: "/usr/bin/fake") is None
    assert (
        probe(
            env,
            FakeRunner(),
            which=lambda name: None if name == "grim" else "/usr/bin/x",
        )
        is None
    )
    assert (
        probe(
            env,
            FakeRunner(),
            which=lambda name: None if name == "tesseract" else "/usr/bin/x",
        )
        is None
    )


def test_ids_must_look_like_hyprland_window_ids() -> None:
    assert WINDOW_ID_RE.match("0x1234ABCD")
    assert not WINDOW_ID_RE.match("0x1234abcd; dispatch exec x")
    assert not WINDOW_ID_RE.match("foot")
