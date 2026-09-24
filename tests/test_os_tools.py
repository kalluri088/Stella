"""Tests for the desktop tools, against the measured surface facts.

Reports 03/11/13 are full of traps a fake can too easily paper over,
so the fake compositor answers exactly like the machine did: garbage
with rc 0, errors on stdout, and focus through this session's Lua
dispatch API (the classic ``dispatch focuswindow "(address:0x…)"``
string form the reports measured is rejected by Omarchy 0.56.2).
Nothing here needs a desktop — the runner is injected.
"""

import json

import pytest

from stella.os_tools import (
    ADDRESS_RE,
    Hyprland,
    KeySendTool,
    ScreenReadTool,
    WindowFocusTool,
    build_desktop_tools,
    mask_secrets,
)
from stella.tools import ApprovalRequest, RiskLevel, action_summary

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
        self.calls.append((tuple(argv), {"stdin": stdin, "env": env}))
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


def json_process(payload) -> FakeProcess:
    return FakeProcess(0, json.dumps(payload).encode("utf-8"), b"")


def clients_process() -> FakeProcess:
    return json_process(CLIENTS)


def active_process(entry: dict) -> FakeProcess:
    return json_process(window_of(entry))


def is_focus_call(argv: tuple) -> bool:
    """The Lua focus dispatch, not a plain ``-j`` read."""

    return "eval" in argv and any("hl.dsp.focus" in part for part in argv)


def make_hyprland(responders, *, signature: str | None = SIGNATURE):
    """A client wired to the fake runner plus the fake itself."""

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


# ------------------------------------------------------------- client


def test_active_window_parses_the_measured_shape() -> None:
    hyprland, runner = make_hyprland({"hyprctl": json_process(ACTIVE_FOOT)})
    window = hyprland.active_window()
    assert window is not None
    assert (window.address, window.class_name, window.pid) == (
        "0x1234abcd",
        "foot",
        4001,
    )
    argv = runner.hyprctl()[0]
    assert argv[:3] == ("hyprctl", "-i", SIGNATURE)  # rule 4
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
    assert hyprland.clients() is None


def test_clients_parses_the_measured_shape() -> None:
    hyprland, _ = make_hyprland({"hyprctl": json_process(CLIENTS)})
    windows = hyprland.clients()
    assert windows is not None
    assert [window.address for window in windows] == [
        "0x1234abcd",
        "0xfeedface",
    ]


def test_focus_uses_the_lua_dispatch_api_and_re_queries() -> None:
    entry = window_of(CLIENTS[1])
    hyprland, runner = make_hyprland(
        {"hyprctl": [FakeProcess(0, b"ok\n", b""), json_process(entry)]}
    )
    assert hyprland.focus("0xfeedface") is True
    eval_call = runner.hyprctl()[0]
    assert eval_call[:4] == ("hyprctl", "-i", SIGNATURE, "eval")
    lua = eval_call[-1]
    assert "hl.get_windows()" in lua  # resolves the window object…
    assert "hl.dsp.focus" in lua  # …and focuses it through the Lua API
    assert "0xfeedface" in lua  # the exact, validated address
    assert "focuswindow" not in lua  # the rejected classic string form
    assert runner.hyprctl()[1][-1] == "activewindow"  # verified, not trusted


def test_focus_denied_by_the_compositor_reads_as_false() -> None:
    hyprland, _ = make_hyprland(
        {
            "hyprctl": [
                json_process(window_of(CLIENTS[1])),
                json_process(window_of(CLIENTS[0])),
            ]
        }
    )
    assert hyprland.focus("0xfeedface") is False


def test_a_missing_signature_fails_closed_without_spawning() -> None:
    runner = FakeRunner()
    hyprland = Hyprland(None, runner=runner)
    assert hyprland.active_window() is None
    assert hyprland.focus("0x1") is False
    assert runner.calls == []


def test_capture_and_recognize_carry_the_measured_arguments() -> None:
    png = b"\x89PNG fake"
    hyprland, runner = make_hyprland(
        {
            "grim": FakeProcess(0, png, b""),
            "tesseract": FakeProcess(0, b"read text\n", b""),
        }
    )
    captured = hyprland.capture("10,20 749x402", timeout=10)
    assert captured.stdout == png
    grim = runner.argvs()[0]
    assert grim == ("grim", "-g", "10,20 749x402", "-")
    env = runner.calls[0][1]["env"]
    assert env["WAYLAND_DISPLAY"] == "wayland-1"
    recognized = hyprland.recognize(png, "6", timeout=30)
    assert recognized.stdout == b"read text\n"
    assert runner.argvs()[1] == ("tesseract", "-", "stdout", "--psm", "6")
    assert runner.calls[1][1]["stdin"] == png


# ---------------------------------------------------------- secrets mask


def test_mask_blanks_tokens_and_password_glyphs_only() -> None:
    long_token = "aX9" + "B" * 45 + "=="
    masked = mask_secrets(
        f"password: {long_token}\ncard **** **** 1234\nplain sentence 3.5"
    )
    assert long_token not in masked
    assert "[redacted]" in masked
    assert "plain sentence 3.5" in masked
    assert "1234" in masked


# ------------------------------------------------------------ registration


def test_desktop_tools_need_both_the_session_and_the_binaries() -> None:
    env = {
        "HYPRLAND_INSTANCE_SIGNATURE": SIGNATURE,
        "WAYLAND_DISPLAY": "wayland-1",
    }
    tools = build_desktop_tools(env, which=lambda _name: "/usr/bin/fake")
    assert [tool.name for tool in tools] == [
        "screen_read",
        "window_focus",
        "key_send",
    ]
    assert build_desktop_tools({}, which=lambda _name: "/usr/bin/fake") == []
    assert (
        build_desktop_tools(
            env,
            which=lambda name: None if name == "grim" else "/usr/bin/x",
        )
        == []
    )


def test_desktop_risk_levels_follow_the_reports() -> None:
    read, focus, keys = build_desktop_tools(
        {"HYPRLAND_INSTANCE_SIGNATURE": SIGNATURE},
        which=lambda _name: "/usr/bin/fake",
    )
    assert read.risk_level is RiskLevel.SENSITIVE
    assert focus.risk_level is RiskLevel.SENSITIVE
    assert keys.risk_level is RiskLevel.DANGEROUS


def test_action_summaries_are_plain_about_the_desktop_capabilities() -> None:
    assert "local OCR" in action_summary(
        ApprovalRequest("screen_read", {"scope": "active_window"})
    )
    assert "0xfeedface" in action_summary(
        ApprovalRequest("window_focus", {"address": "0xfeedface"})
    )
    summary = action_summary(
        ApprovalRequest("key_send", {"address": "0xfeedface", "text": "hi"})
    )
    assert "2 characters" in summary and "can never be undone" in summary


# ----------------------------------------------------------- screen_read


def screen_tool(responders, **kwargs):
    hyprland, runner = make_hyprland(responders, **kwargs)
    return ScreenReadTool(hyprland), runner


def test_screen_read_of_the_active_window_is_bounded_ocr_text() -> None:
    tool, runner = screen_tool(
        {
            "hyprctl": json_process(ACTIVE_FOOT),
            "grim": FakeProcess(0, b"\x89PNG image", b""),
            "tesseract": FakeProcess(0, b"visible words\n", b""),
        }
    )
    result = tool.execute({"scope": "active_window"})
    assert result.success
    assert "foot" in result.output and "visible words" in result.output
    assert "read locally" in result.output and "OCR" in result.output
    assert runner.first("grim") == ("grim", "-g", "10,20 749x402", "-")
    assert runner.first("grim")[-1] == "-"  # PNG to stdout, not a file
    assert runner.first("tesseract")[-2:] == ("--psm", "6")


def test_full_screen_skips_geometry_and_swaps_to_the_sparse_psm() -> None:
    tool, runner = screen_tool(
        {
            "grim": FakeProcess(0, b"\x89PNG image", b""),
            "tesseract": FakeProcess(0, b"scattered\n", b""),
        }
    )
    result = tool.execute({"scope": "full_screen"})
    assert result.success
    assert "full screen" in result.output
    assert runner.hyprctl() == []  # no focused-window query at all
    assert runner.first("grim") == ("grim", "-")  # whole screen to stdout
    assert "-g" not in runner.first("grim")
    assert runner.first("tesseract")[-2:] == ("--psm", "11")


def test_screen_read_masks_credentials_before_the_model_sees_them() -> None:
    token = "Zk9" + "x" * 44
    tool, _ = screen_tool(
        {
            "hyprctl": json_process(ACTIVE_FOOT),
            "grim": FakeProcess(0, b"\x89PNG image", b""),
            "tesseract": FakeProcess(0, f"api key {token}\n".encode(), b""),
        }
    )
    result = tool.execute({"scope": "active_window"})
    assert result.success
    assert token not in result.output
    assert "[redacted]" in result.output


def test_a_failed_capture_fails_honestly_without_ocr() -> None:
    tool, runner = screen_tool(
        {
            "hyprctl": json_process(ACTIVE_FOOT),
            "grim": FakeProcess(1, b"", b"failed to capture"),
        }
    )
    result = tool.execute({"scope": "active_window"})
    assert not result.success
    assert "grim" in result.output
    assert not [argv for argv, _ in runner.calls if argv[0] == "tesseract"]


def test_ocr_failure_is_reported_as_ocr_not_as_blank_screen() -> None:
    tool, _ = screen_tool(
        {
            "hyprctl": json_process(ACTIVE_FOOT),
            "grim": FakeProcess(0, b"\x89PNG image", b""),
            "tesseract": FakeProcess(1, b"Error opening data file", b""),
        }
    )
    result = tool.execute({"scope": "active_window"})
    assert not result.success
    assert "OCR failed" in result.output
    # report 03 rule 2: the compositor/tesseract error text is on stdout
    assert "Error opening data file" in result.output


def test_a_blank_screen_is_a_success_saying_so() -> None:
    tool, _ = screen_tool(
        {
            "hyprctl": json_process(ACTIVE_FOOT),
            "grim": FakeProcess(0, b"\x89PNG image", b""),
            "tesseract": FakeProcess(0, b"   \n", b""),
        }
    )
    result = tool.execute({"scope": "active_window"})
    assert result.success
    assert "no readable text" in result.output


def test_long_ocr_text_is_truncated_with_an_announcement() -> None:
    tool, _ = screen_tool(
        {
            "hyprctl": json_process(ACTIVE_FOOT),
            "grim": FakeProcess(0, b"\x89PNG image", b""),
            "tesseract": FakeProcess(0, ("word " * 4000).encode(), b""),
        }
    )
    result = tool.execute({"scope": "active_window"})
    assert result.success
    assert "Truncated" in result.output
    assert len(result.output) < 8_000


def test_without_a_session_no_capture_happens() -> None:
    tool, runner = screen_tool(
        {"hyprctl": json_process(ACTIVE_FOOT)}, signature=None
    )
    result = tool.execute({"scope": "active_window"})
    assert not result.success
    assert "Hyprland" in result.output
    assert runner.calls == []


def test_screen_read_needs_exactly_a_known_scope() -> None:
    tool, _ = screen_tool({})
    assert tool.validate_arguments({"scope": "full_screen"})
    for bad in (
        {},
        {"scope": "second_monitor"},
        {"scope": "full_screen", "extra": 1},
    ):
        assert not tool.validate_arguments(bad)


# --------------------------------------------------------- window_focus


def focus_tool(responders, **kwargs):
    hyprland, runner = make_hyprland(responders, **kwargs)
    return WindowFocusTool(hyprland), runner


def test_window_focus_verifies_before_claiming_success() -> None:
    tool, runner = focus_tool(
        {
            "hyprctl": [
                clients_process(),  # the tool's target lookup
                FakeProcess(0, b"", b""),  # the dispatch answer (ignored;
                active_process(CLIENTS[1]),  # the re-query is the proof)
            ]
        }
    )
    result = tool.execute({"address": "0xfeedface"})
    assert result.success
    assert "firefox" in result.output and "confirmed" in result.output
    assert result.action_receipt is not None
    assert result.action_receipt.status == "verified"
    focus_calls = [argv for argv in runner.hyprctl() if is_focus_call(argv)]
    assert any("0xfeedface" in part for part in focus_calls[0])


def test_focusing_an_unknown_address_is_missing_not_failed() -> None:
    tool, runner = focus_tool({"hyprctl": [clients_process()]})
    result = tool.execute({"address": "0x99999999"})
    assert not result.success
    assert result.action_receipt.status == "missing"
    assert not [argv for argv in runner.hyprctl() if is_focus_call(argv)]


def test_unconfirmed_focus_never_reports_success() -> None:
    tool, _ = focus_tool(
        {
            "hyprctl": [
                clients_process(),  # target lookup
                FakeProcess(0, b"", b""),  # dispatch
                active_process(CLIENTS[0]),  # focus(): still the old window
                active_process(CLIENTS[0]),  # the failure message's query
            ]
        }
    )
    result = tool.execute({"address": "0xfeedface"})
    assert not result.success
    assert result.action_receipt.status == "unverified"
    assert "could not be confirmed" in result.output


def test_garbage_window_list_refuses_before_any_dispatch() -> None:
    tool, runner = focus_tool(
        {"hyprctl": FakeProcess(0, b"unknown request", b"")}
    )
    result = tool.execute({"address": "0xfeedface"})
    assert not result.success
    assert result.action_receipt.status == "failed"
    assert "usable window list" in result.output
    assert not [argv for argv in runner.hyprctl() if is_focus_call(argv)]


def test_focus_preview_names_the_resolved_window() -> None:
    tool, _ = focus_tool({"hyprctl": clients_process()})
    preview = tool.preview(
        ApprovalRequest("window_focus", {"address": "0xfeedface"})
    )
    assert preview is not None
    joined = " ".join(preview.detail_lines)
    assert "firefox" in joined and "pid 55" in joined


@pytest.mark.parametrize(
    "arguments, valid",
    [
        ({"address": "0x1234abcd"}, True),
        ({"address": "foot"}, False),  # titles are never addresses
        ({"address": "0x1234abcd; touch /evil"}, False),
        ({"address": "0x1234abcd", "workspace": 2}, False),
        ({}, False),
    ],
)
def test_focus_argument_contract(arguments, valid) -> None:
    tool, _ = focus_tool({})
    assert tool.validate_arguments(arguments) is valid


# ------------------------------------------------------------- key_send


def send_tool(responders, **kwargs):
    hyprland, runner = make_hyprland(responders, **kwargs)
    return KeySendTool(hyprland), runner


def test_key_send_types_only_after_a_verified_focus() -> None:
    tool, runner = send_tool(
        {
            "hyprctl": [
                clients_process(),
                FakeProcess(0, b"", b""),  # dispatch (ignored, re-query rules)
                active_process(CLIENTS[1]),
            ],
            "wtype": FakeProcess(0),
        }
    )
    result = tool.execute({"address": "0xfeedface", "text": "echo hi"})
    assert result.success
    assert runner.typed()[0] == ("wtype", "echo hi")
    assert result.action_receipt.status == "inconclusive"
    assert result.action_receipt.size_bytes == len(b"echo hi")
    assert "cannot be verified" in result.output


def test_the_send_sequence_is_resolve_focus_confirm_type() -> None:
    tool, runner = send_tool(
        {
            "hyprctl": [
                clients_process(),
                FakeProcess(0, b"", b""),
                active_process(CLIENTS[1]),
            ],
            "wtype": FakeProcess(0),
        }
    )
    assert tool.execute({"address": "0xfeedface", "text": "hello"}).success
    kinds = []
    for argv in runner.hyprctl():
        if "clients" in argv:
            kinds.append("clients")
        elif is_focus_call(argv):
            kinds.append("focus")
        elif "activewindow" in argv:
            kinds.append("active")
    if runner.typed():
        kinds.append("wtype")
    assert kinds == ["clients", "focus", "active", "wtype"]


def test_a_denied_focus_blocks_every_keystroke() -> None:
    tool, runner = send_tool(
        {
            "hyprctl": [
                clients_process(),
                FakeProcess(0, b"", b""),
                active_process(CLIENTS[0]),
                active_process(CLIENTS[0]),
            ]
        }
    )
    result = tool.execute({"address": "0xfeedface", "text": "nope"})
    assert not result.success
    assert result.action_receipt.status == "failed"
    assert "Refusing to type" in result.output
    assert runner.typed() == []


def test_a_failing_wtype_is_honest_about_delivery() -> None:
    tool, _ = send_tool(
        {
            "hyprctl": [
                clients_process(),
                FakeProcess(0, b"", b""),
                active_process(CLIENTS[1]),
            ],
            "wtype": FakeProcess(1, b"", b"not a keyboard"),
        }
    )
    result = tool.execute({"address": "0xfeedface", "text": "hi"})
    assert not result.success
    assert result.action_receipt.status == "failed"
    assert "wtype" in result.output


def test_restore_focus_returns_and_says_so() -> None:
    tool, runner = send_tool(
        {
            "hyprctl": [
                clients_process(),
                active_process(CLIENTS[0]),  # the current focus, captured
                FakeProcess(0, b"", b""),  # focus the target
                active_process(CLIENTS[1]),  # verified
                FakeProcess(0, b"", b""),  # restore dispatch
                active_process(CLIENTS[0]),  # restore verified
            ],
            "wtype": FakeProcess(0),
        }
    )
    result = tool.execute(
        {"address": "0xfeedface", "text": "hi", "restore_focus": True}
    )
    assert result.success
    assert "0x1234abcd" in result.output
    focused = [
        next(part for part in argv if "hl.dsp.focus" in part)
        for argv in runner.hyprctl()
        if is_focus_call(argv)
    ]
    assert len(focused) == 2
    assert "0xfeedface" in focused[0]  # focus the typing target first
    assert "0x1234abcd" in focused[1]  # then restore the previous focus


def test_key_send_contract_is_exact() -> None:
    tool, _ = send_tool({})
    assert tool.validate_arguments(
        {"address": "0x1234abcd", "text": "hi", "restore_focus": True}
    )
    assert tool.validate_arguments({"address": "0x1234abcd", "text": "hi"})
    for bad in (
        {"address": "0x1234abcd"},  # no text
        {"address": "0x1234abcd", "text": ""},
        {"address": "0x1234abcd", "text": "x" * 2001},
        {"address": "0x1234abcd", "text": "have\x00null"},
        {"address": "0x1234abcd", "text": "hi", "restore_focus": "yes"},
        {"address": "0x1234abcd", "text": "hi", "keysym": "return"},
    ):
        assert not tool.validate_arguments(bad)


def test_key_send_preview_shows_the_exact_text_and_target() -> None:
    tool, _ = send_tool({"hyprctl": clients_process()})
    preview = tool.preview(
        ApprovalRequest(
            "key_send", {"address": "0xfeedface", "text": "rm -rf /"}
        )
    )
    assert preview is not None
    joined = " ".join(preview.detail_lines)
    assert "firefox" in joined and "rm -rf /" in joined


def test_key_send_without_a_signature_never_touches_a_process() -> None:
    tool, runner = send_tool({"hyprctl": clients_process()}, signature=None)
    result = tool.execute({"address": "0xfeedface", "text": "hi"})
    assert not result.success
    assert runner.calls == []


def test_addresses_must_look_like_compositor_addresses() -> None:
    assert ADDRESS_RE.match("0x1234ABCD")
    assert not ADDRESS_RE.match("0x1234abcd; dispatch exec x")
    assert not ADDRESS_RE.match("foot")
