"""The desktop tools and the registry, against the seam contract.

Everything here is written against :class:`stella.desktop.capabilities.
Desktop` with Python fakes — no subprocess, no compositor, no display.
What these tests protect is the *safety model* the tools own: the
None-versus-empty distinction, resolve-then-activate-then-verify-then-type,
the bounded and masked output, and the rule that an unusable desktop
registers nothing. The measured argv-level facts live in
``test_hyprland.py``.
"""

import pytest

from stella.desktop import build_desktop_tools
from stella.desktop.backends import (
    gnome,
    hyprland,
    kde,
    sway,
    wayland_screencopy,
    x11_ewmh,
)
from stella.desktop.capabilities import (
    MAX_SCREEN_TEXT_CHARS,
    OCR_SCREEN,
    OCR_WINDOW,
    CaptureUnavailable,
    Desktop,
    RecognizeUnavailable,
    Region,
    SendUnavailable,
    Window,
)
from stella.desktop.registry import _CANDIDATES, select_desktop
from stella.desktop.tools import (
    DesktopControlTool,
    KeySendTool,
    ScreenReadTool,
    WindowFocusTool,
    tools_for,
)
from stella.tools import ApprovalRequest, RiskLevel, action_summary

FOOT = Window("0x1234abcd", "foot", "Shell - ~", 4001, "2", Region(10, 20, 749, 402))
FIREFOX = Window(
    "0xfeedface", "firefox", "Error page - Mozilla Firefox", 55, "1"
)
PNG = b"\x89PNG fake"
NOTE = "test session marker 7"


class Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []


class FakeReader:
    def __init__(
        self,
        recorder: Recorder,
        *,
        active: Window | None = FOOT,
        windows: list[Window] | None = None,
    ) -> None:
        self.recorder = recorder
        self.active = active
        self.windows_list = windows
        self.focus_history: list[Window] = []

    def active_window(self) -> Window | None:
        self.recorder.events.append("read:active")
        if self.focus_history:
            return self.focus_history[-1]
        return self.active

    def windows(self) -> list[Window] | None:
        self.recorder.events.append("read:windows")
        return self.windows_list


class FakeActivator:
    """Moves focus only for the ids it is told to, and proves it by re-reading."""

    def __init__(
        self,
        recorder: Recorder,
        reader: FakeReader,
        *,
        succeeds_for: list[str] | None,
    ) -> None:
        self.recorder = recorder
        self.reader = reader
        self.succeeds_for = succeeds_for or []

    def activate(self, window_id: str) -> bool:
        self.recorder.events.append(f"activate:{window_id}")
        if window_id not in self.succeeds_for:
            return False
        # A real adapter proves the move by re-querying; the fake moves the
        # focused window so a follow-up read sees it (invariant 2).
        target = next(
            (item for item in (self.reader.windows_list or []) if item.id == window_id),
            None,
        )
        if target is not None:
            self.reader.focus_history.append(target)
        return True


class FakeCapture:
    def __init__(self, recorder: Recorder, *, fail: str | None = None) -> None:
        self.recorder = recorder
        self.fail = fail
        self.regions: list[Region | None] = []

    def capture(self, region: Region | None) -> bytes:
        self.recorder.events.append("capture")
        self.regions.append(region)
        if self.fail is not None:
            raise CaptureUnavailable(self.fail)
        return PNG


class FakeKeys:
    def __init__(self, recorder: Recorder, *, fail: str | None = None) -> None:
        self.recorder = recorder
        self.fail = fail
        self.typed: list[str] = []

    def send_text(self, text: str) -> None:
        self.recorder.events.append("send")
        if self.fail is not None:
            raise SendUnavailable(self.fail)
        self.typed.append(text)


class FakeRecognizer:
    def __init__(
        self, recorder: Recorder, *, text: str = "visible words", fail: str | None = None
    ) -> None:
        self.recorder = recorder
        self.text = text
        self.fail = fail
        self.hints: list[str] = []

    def recognize(self, png: bytes, hint: str) -> str:
        self.recorder.events.append(f"recognize:{hint}")
        self.hints.append(hint)
        assert png == PNG
        if self.fail is not None:
            raise RecognizeUnavailable(self.fail)
        return self.text


class FakeManager:
    """A window manager whose every answer the test scripts directly."""

    def __init__(
        self,
        recorder: Recorder,
        *,
        launch_to: bool = True,
        close_to: bool = True,
        move_to: bool = True,
        raise_to: str | None = None,
    ) -> None:
        self.recorder = recorder
        self.launch_to = launch_to
        self.close_to = close_to
        self.move_to = move_to
        self.raise_to = raise_to
        self.calls: list[tuple] = []

    def _guard(self, name: str, *args) -> None:
        self.recorder.events.append(f"manage:{name}")
        self.calls.append((name, *args))
        if self.raise_to is not None:
            from stella.desktop.capabilities import DesktopUnavailable

            raise DesktopUnavailable(self.raise_to)

    def launch(self, command: str, workspace=None) -> bool:
        self._guard("launch", command, workspace)
        return self.launch_to

    def close(self, window_id: str) -> bool:
        self._guard("close", window_id)
        return self.close_to

    def move(self, window_id: str, workspace: int) -> bool:
        self._guard("move", window_id, workspace)
        return self.move_to


def make_desktop(
    *,
    active: Window | None = FOOT,
    windows: list[Window] | None = None,
    succeeds_for: list[str] | None = None,
    capture_fail: str | None = None,
    send_fail: str | None = None,
    ocr_text: str = "visible words",
    ocr_fail: str | None = None,
    manager: object | None = None,
) -> tuple[Desktop, Recorder]:
    recorder = Recorder()
    reader = FakeReader(recorder, active=active, windows=windows)
    if succeeds_for is None:
        succeeds_for = ["0xfeedface"]
    return (
        Desktop(
            name="fake",
            reader=reader,
            activator=FakeActivator(recorder, reader, succeeds_for=succeeds_for),
            capture=FakeCapture(recorder, fail=capture_fail),
            keys=FakeKeys(recorder, fail=send_fail),
            recognizer=FakeRecognizer(recorder, text=ocr_text, fail=ocr_fail),
            session_note=NOTE,
            manager=manager,  # type: ignore[arg-type]
        ),
        recorder,
    )


def default_windows() -> list[Window]:
    return [FOOT, FIREFOX]


# ------------------------------------------------------------- registration


def test_the_seam_offers_exactly_three_tools_and_no_new_verbs() -> None:
    desktop, _ = make_desktop(windows=default_windows())
    assert [tool.name for tool in tools_for(desktop)] == [
        "screen_read",
        "window_focus",
        "key_send",
    ]


def test_an_unusable_environment_registers_nothing() -> None:
    # No session marker at all: every candidate probe says None.
    assert build_desktop_tools({}, which=lambda _name: "/usr/bin/fake") == []


def test_registration_needs_the_session_and_every_binary() -> None:
    env = {
        "HYPRLAND_INSTANCE_SIGNATURE": "sig_1_2",
        "WAYLAND_DISPLAY": "wayland-1",
    }
    tools = build_desktop_tools(env, which=lambda _name: "/usr/bin/fake")
    assert [tool.name for tool in tools] == [
        "screen_read",
        "window_focus",
        "key_send",
        "desktop_control",
    ]
    assert (
        build_desktop_tools(env, which=lambda n: None if n == "grim" else "/x") == []
    )
    assert (
        build_desktop_tools(env, which=lambda n: None if n == "wtype" else "/x") == []
    )


@pytest.mark.parametrize(
    "stub", [sway, x11_ewmh, gnome, kde, wayland_screencopy]
)
def test_unwritten_adapters_register_nothing(stub) -> None:
    # Each stub must say None for any environment, so a half-written
    # adapter can never hand the model a capability that only fails.
    env = {
        "HYPRLAND_INSTANCE_SIGNATURE": "sig",
        "SWAYSOCK": "/tmp/sway.sock",
        "XDG_CURRENT_DESKTOP": "KDE:GNOME",
        "XDG_SESSION_TYPE": "x11",
        "DISPLAY": ":0",
        "WAYLAND_DISPLAY": "wayland-1",
    }
    assert stub.probe(env, lambda *a, **k: None, which=lambda _n: "/usr/bin/x") is None


def test_documented_probe_order_is_hyprland_first() -> None:
    assert [name for name, _ in _CANDIDATES] == [
        "hyprland",
        "sway",
        "kde",
        "gnome",
        "x11_ewmh",
        "wayland_screencopy",
    ]


def test_the_first_candidate_that_answers_wins_and_a_raising_one_is_skipped(
    monkeypatch,
) -> None:
    desktop, _ = make_desktop(windows=default_windows())
    calls: list[str] = []

    def raising(env, runner, *, which):
        calls.append("raising")
        raise RuntimeError("a broken adapter must not take the assistant down")

    def silent(env, runner, *, which):
        calls.append("silent")

    def answering(env, runner, *, which):
        calls.append("answering")
        return desktop

    monkeypatch.setattr(
        "stella.desktop.registry._CANDIDATES",
        (("raising", raising), ("silent", silent), ("answering", answering)),
    )
    assert select_desktop({}, which=lambda _name: "/usr/bin/fake") is desktop
    assert calls == ["raising", "silent", "answering"]


def test_no_answer_from_any_candidate_is_no_desktop(monkeypatch) -> None:
    def silent(env, runner, *, which):
        return None

    monkeypatch.setattr(
        "stella.desktop.registry._CANDIDATES", (("silent", silent),)
    )
    assert select_desktop({}, which=lambda _name: "/usr/bin/fake") is None


# --------------------------------------------------------------- risk floors


def test_desktop_risk_levels_follow_the_reports() -> None:
    desktop, _ = make_desktop(windows=default_windows())
    read, focus, keys = tools_for(desktop)
    assert read.risk_level is RiskLevel.SENSITIVE
    assert focus.risk_level is RiskLevel.SENSITIVE
    assert keys.risk_level is RiskLevel.DANGEROUS


def test_a_full_screen_capture_costs_an_explicit_approval() -> None:
    desktop, _ = make_desktop(windows=default_windows())
    read = ScreenReadTool(desktop)
    assert read.argument_risk({"scope": "full_screen"}) is RiskLevel.DANGEROUS
    assert read.argument_risk({"scope": "active_window"}) is None


def test_action_summaries_are_plain_about_the_desktop_capabilities() -> None:
    assert "local OCR" in action_summary(
        ApprovalRequest("screen_read", {"scope": "active_window"})
    )
    assert "0xfeedface" in action_summary(
        ApprovalRequest("window_focus", {"id": "0xfeedface"})
    )
    summary = action_summary(
        ApprovalRequest("key_send", {"id": "0xfeedface", "text": "hi"})
    )
    assert "2 characters" in summary and "can never be undone" in summary


def test_the_approval_preview_names_the_session_being_touched() -> None:
    desktop, _ = make_desktop(windows=default_windows())
    preview = WindowFocusTool(desktop).preview(
        ApprovalRequest("window_focus", {"id": "0xfeedface"})
    )
    assert preview is not None
    joined = " ".join(preview.detail_lines)
    assert NOTE in joined and "fake" in joined


# ---------------------------------------------------------------- screen_read


def test_screen_read_of_the_active_window_asks_the_seam_in_order() -> None:
    desktop, recorder = make_desktop(windows=default_windows())
    result = ScreenReadTool(desktop).execute({"scope": "active_window"})
    assert result.success
    assert "foot" in result.output and "visible words" in result.output
    assert "read locally" in result.output and "OCR" in result.output
    assert recorder.events == ["read:active", "capture", "recognize:window"]
    assert desktop.capture.regions == [FOOT.region]  # type: ignore[attr-defined]


def test_full_screen_captures_without_a_region_and_hints_the_sparse_pass() -> None:
    desktop, recorder = make_desktop(windows=default_windows())
    result = ScreenReadTool(desktop).execute({"scope": "full_screen"})
    assert result.success
    assert "full screen" in result.output
    assert "read:active" not in recorder.events  # no focused-window query
    assert desktop.capture.regions == [None]  # type: ignore[attr-defined]
    assert desktop.recognizer.hints == [OCR_SCREEN]  # type: ignore[attr-defined]
    assert ScreenReadTool(desktop).validate_arguments({"scope": "active_window"})
    assert OCR_WINDOW == "window" and OCR_SCREEN == "screen"


def test_screen_read_masks_credentials_before_the_model_sees_them() -> None:
    token = "Zk9" + "x" * 44
    desktop, _ = make_desktop(ocr_text=f"api key {token}")
    result = ScreenReadTool(desktop).execute({"scope": "active_window"})
    assert result.success
    assert token not in result.output
    assert "[redacted]" in result.output


def test_long_ocr_text_is_truncated_with_an_announcement() -> None:
    desktop, _ = make_desktop(ocr_text="word " * 4000)

    result = ScreenReadTool(desktop).execute({"scope": "active_window"})
    assert result.success
    assert "Truncated" in result.output
    assert len(result.output) < MAX_SCREEN_TEXT_CHARS + 2_000
    assert str(MAX_SCREEN_TEXT_CHARS) in result.output


def test_an_unusable_focused_window_is_not_an_excuse_to_grab_the_screen() -> None:
    # A window with no geometry would need a whole-screen capture, which is
    # a wider scope than the user approved: it fails instead.
    desktop, recorder = make_desktop(active=Window(
        "0x1", "foot", "no geometry", 1, "1"
    ))
    result = ScreenReadTool(desktop).execute({"scope": "active_window"})
    assert not result.success
    assert "focused-window description" in result.output
    assert "capture" not in recorder.events


def test_a_blank_screen_is_a_success_saying_so() -> None:
    # A recognizer strips its own output, so "nothing readable" is "".
    desktop, _ = make_desktop(ocr_text="")
    result = ScreenReadTool(desktop).execute({"scope": "active_window"})
    assert result.success
    assert "no readable text" in result.output


def test_a_failed_capture_stops_before_ocr() -> None:
    desktop, recorder = make_desktop(capture_fail="capture failed: no grim")
    result = ScreenReadTool(desktop).execute({"scope": "active_window"})
    assert not result.success
    assert "grim" in result.output
    assert not [event for event in recorder.events if event.startswith("recognize")]


def test_ocr_failure_is_reported_as_ocr_not_as_a_blank_screen() -> None:
    desktop, _ = make_desktop(ocr_fail="Local OCR failed: bad data file")
    result = ScreenReadTool(desktop).execute({"scope": "active_window"})
    assert not result.success
    assert "OCR failed" in result.output
    assert "no readable text" not in result.output


def test_screen_read_needs_exactly_a_known_scope() -> None:
    desktop, _ = make_desktop(windows=default_windows())
    tool = ScreenReadTool(desktop)
    assert tool.validate_arguments({"scope": "full_screen"})
    for bad in (
        {},
        {"scope": "second_monitor"},
        {"scope": "full_screen", "extra": 1},
    ):
        assert not tool.validate_arguments(bad)


# ------------------------------------------------------------------- focus


def test_window_focus_verifies_before_claiming_success() -> None:
    desktop, recorder = make_desktop(windows=default_windows())
    result = WindowFocusTool(desktop).execute({"id": "0xfeedface"})
    assert result.success
    assert "firefox" in result.output and "confirmed" in result.output
    assert result.action_receipt is not None
    assert result.action_receipt.status == "verified"
    assert recorder.events[:2] == ["read:windows", "activate:0xfeedface"]


def test_focusing_an_unknown_id_is_missing_not_failed() -> None:
    desktop, recorder = make_desktop(windows=default_windows())
    result = WindowFocusTool(desktop).execute({"id": "0x99999999"})
    assert not result.success
    assert result.action_receipt.status == "missing"
    assert "activate:0x99999999" not in recorder.events


def test_unconfirmed_focus_never_reports_success() -> None:
    desktop, recorder = make_desktop(
        windows=default_windows(), succeeds_for=["0x1234abcd"]
    )
    result = WindowFocusTool(desktop).execute({"id": "0xfeedface"})
    assert not result.success
    assert result.action_receipt.status == "unverified"
    assert "could not be confirmed" in result.output
    # The failure text names the window the desktop *did* focus, so the
    # honest report costs one more read after the refused activation.
    assert recorder.events == [
        "read:windows",
        "activate:0xfeedface",
        "read:active",
    ]


def test_an_unusable_window_list_refuses_the_act_entirely() -> None:
    # None ("the answer was unusable") must not be read as [] ("nothing is
    # open"): the receipt says failed, and nothing is ever dispatched.
    desktop, recorder = make_desktop(windows=None)
    result = WindowFocusTool(desktop).execute({"id": "0xfeedface"})
    assert not result.success
    assert result.action_receipt.status == "failed"
    assert "usable window list" in result.output
    assert not [event for event in recorder.events if event.startswith("activate")]


def test_definitely_no_windows_is_honestly_missing() -> None:
    desktop, _ = make_desktop(windows=[])
    result = WindowFocusTool(desktop).execute({"id": "0xfeedface"})
    assert result.action_receipt.status == "missing"


def test_focus_preview_names_the_resolved_window() -> None:
    desktop, _ = make_desktop(windows=default_windows())
    preview = WindowFocusTool(desktop).preview(
        ApprovalRequest("window_focus", {"id": "0xfeedface"})
    )
    assert preview is not None
    joined = " ".join(preview.detail_lines)
    assert "firefox" in joined and "pid 55" in joined


def test_focus_preview_says_a_refusal_is_coming() -> None:
    desktop, _ = make_desktop(windows=None)
    preview = WindowFocusTool(desktop).preview(
        ApprovalRequest("window_focus", {"id": "0xfeedface"})
    )
    assert preview is not None
    assert "refused at execution" in " ".join(preview.detail_lines)


@pytest.mark.parametrize(
    "arguments, valid",
    [
        ({"id": "0x1234abcd"}, True),
        ({"id": "3"}, True),  # an opaque id: sway-style numbers are legal
        ({"id": "0x1234abcd; touch /evil"}, False),  # whitespace is the tell
        ({"id": "foot 1"}, False),  # titles never survive as single tokens
        ({"id": "0x1234abcd", "workspace": 2}, False),
        ({"id": ""}, False),
        ({"id": "x" * 65}, False),
        ({}, False),
    ],
)
def test_focus_argument_contract(arguments, valid) -> None:
    desktop, _ = make_desktop(windows=default_windows())
    assert WindowFocusTool(desktop).validate_arguments(arguments) is valid


def test_the_adapter_half_of_the_id_contract_stays_with_the_adapter() -> None:
    # The tools accept an opaque id; the Hyprland adapter still refuses
    # anything that is not a hex address, without spawning anything.
    runner_calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        runner_calls.append(list(argv))

        class Done:
            returncode = 0
            stdout = b"{}"
            stderr = b""

        return Done()

    client = hyprland.Hyprland("sig", runner=runner)
    assert client.activate("3") is False
    assert runner_calls == []


# ---------------------------------------------------------------- key_send


def test_key_send_types_only_after_a_verified_focus() -> None:
    desktop, recorder = make_desktop(windows=default_windows())
    result = KeySendTool(desktop).execute({"id": "0xfeedface", "text": "echo hi"})
    assert result.success
    assert desktop.keys.typed == ["echo hi"]  # type: ignore[attr-defined]
    assert recorder.events == [
        "read:windows",
        "activate:0xfeedface",
        "send",
    ]
    assert result.action_receipt.status == "inconclusive"
    assert result.action_receipt.size_bytes == len(b"echo hi")
    assert "cannot be verified" in result.output


def test_a_denied_focus_blocks_every_keystroke() -> None:
    desktop, recorder = make_desktop(windows=default_windows(), succeeds_for=[])
    result = KeySendTool(desktop).execute({"id": "0xfeedface", "text": "nope"})
    assert not result.success
    assert result.action_receipt.status == "failed"
    assert "Refusing to type" in result.output
    assert "send" not in recorder.events


def test_undelivered_keystrokes_are_honest_about_delivery() -> None:
    desktop, _ = make_desktop(windows=default_windows(), send_fail="typing failed")
    result = KeySendTool(desktop).execute({"id": "0xfeedface", "text": "hi"})
    assert not result.success
    assert result.action_receipt.status == "failed"
    assert "typing failed" in result.output


def test_an_unusable_window_list_blocks_the_send_before_any_focus_move() -> None:
    desktop, recorder = make_desktop(windows=None)
    result = KeySendTool(desktop).execute({"id": "0xfeedface", "text": "hi"})
    assert not result.success
    assert result.action_receipt.status == "failed"
    assert recorder.events == ["read:windows"]


def test_restore_focus_returns_and_says_so() -> None:
    desktop, recorder = make_desktop(
        windows=default_windows(), succeeds_for=["0xfeedface", "0x1234abcd"]
    )
    result = KeySendTool(desktop).execute(
        {"id": "0xfeedface", "text": "hi", "restore_focus": True}
    )
    assert result.success
    assert "0x1234abcd" in result.output
    assert recorder.events == [
        "read:windows",
        "read:active",
        "activate:0xfeedface",
        "send",
        "activate:0x1234abcd",
    ]


def test_a_failed_restore_is_a_warning_not_a_failure() -> None:
    # The text was delivered, so the send itself succeeded; only the
    # nicety of giving the focus back failed, and that is said plainly.
    desktop, recorder = make_desktop(
        windows=default_windows(), succeeds_for=["0xfeedface"]
    )
    result = KeySendTool(desktop).execute(
        {"id": "0xfeedface", "text": "hi", "restore_focus": True}
    )
    assert result.success
    assert "WARNING" in result.output and "0x1234abcd" in result.output
    assert recorder.events[-1] == "activate:0x1234abcd"


def test_key_send_contract_is_exact() -> None:
    desktop, _ = make_desktop(windows=default_windows())
    tool = KeySendTool(desktop)
    assert tool.validate_arguments(
        {"id": "0x1234abcd", "text": "hi", "restore_focus": True}
    )
    assert tool.validate_arguments({"id": "0x1234abcd", "text": "hi"})
    for bad in (
        {"id": "0x1234abcd"},  # no text
        {"id": "0x1234abcd", "text": ""},
        {"id": "0x1234abcd", "text": "x" * 2001},
        {"id": "0x1234abcd", "text": "have\x00null"},
        {"id": "0x1234abcd", "text": "hi", "restore_focus": "yes"},
        {"id": "0x1234abcd", "text": "hi", "keysym": "return"},
        {"text": "hi"},
    ):
        assert not tool.validate_arguments(bad)


def test_key_send_preview_shows_the_exact_text_and_target() -> None:
    desktop, _ = make_desktop(windows=default_windows())
    preview = KeySendTool(desktop).preview(
        ApprovalRequest("key_send", {"id": "0xfeedface", "text": "rm -rf /"})
    )
    assert preview is not None
    joined = " ".join(preview.detail_lines)
    assert "firefox" in joined and "rm -rf /" in joined
    assert NOTE in joined


def test_key_send_preview_refuses_an_unknown_target_early() -> None:
    desktop, _ = make_desktop(windows=default_windows())
    preview = KeySendTool(desktop).preview(
        ApprovalRequest("key_send", {"id": "0xdeadbeef", "text": "hi"})
    )
    assert preview is not None
    assert "refused at execution" in " ".join(preview.detail_lines)


# ------------------------------------------------------------- desktop_control


def control_tool(**manager_kw) -> tuple[DesktopControlTool, FakeManager, Recorder]:
    recorder = Recorder()
    manager = FakeManager(recorder, **manager_kw)
    desktop = make_desktop(windows=default_windows(), manager=manager)[0]
    return DesktopControlTool(desktop, which=lambda name: f"/usr/bin/{name}"), manager, recorder


def test_control_joins_only_when_a_window_manager_exists() -> None:
    without, _ = make_desktop(windows=default_windows())
    assert [tool.name for tool in tools_for(without)] == [
        "screen_read",
        "window_focus",
        "key_send",
    ]
    recorder = Recorder()
    with_manager = make_desktop(
        windows=default_windows(), manager=FakeManager(recorder)
    )[0]
    assert [tool.name for tool in tools_for(with_manager)][-1] == "desktop_control"


def test_every_desktop_control_use_costs_an_approval() -> None:
    tool, _, _ = control_tool()
    assert tool.risk_level is RiskLevel.DANGEROUS


@pytest.mark.parametrize(
    ("arguments", "valid"),
    [
        ({"target": "app", "action": "launch", "name": "chromium"}, True),
        ({"target": "app", "action": "launch", "name": "foot --title t"}, True),
        ({"target": "app", "action": "close", "id": "0xfeedface"}, False),
        ({"target": "app", "action": "launch"}, False),
        ({"target": "app", "action": "launch", "name": "chromium; rm -rf /"}, False),
        ({"target": "app", "action": "launch", "name": "$(curl evil)"}, False),
        ({"target": "app", "action": "launch", "name": "ghostapp"}, False),  # not on PATH
        ({"target": "app", "action": "launch", "name": "chromium", "id": "0x1"}, False),
        ({"target": "window", "action": "close", "id": "0xfeedface"}, True),
        ({"target": "window", "action": "move", "id": "0xfeedface", "workspace": 3}, True),
        ({"target": "window", "action": "move", "id": "0xfeedface"}, False),  # no workspace
        ({"target": "window", "action": "move", "id": "0xfeedface", "workspace": True}, False),
        ({"target": "window", "action": "move", "id": "0xfeedface", "workspace": 0}, False),
        ({"target": "window", "action": "focus", "id": "0xfeedface"}, True),
        ({"target": "window", "action": "focus", "id": "0xfeedface", "workspace": 2}, False),
        ({"target": "window", "action": "launch", "id": "0xfeedface"}, False),
        ({"target": "window", "action": "close"}, False),
        ({"target": "elsewhere", "action": "close", "id": "0xfeedface"}, False),
    ],
)
def test_desktop_control_argument_contract(arguments, valid) -> None:
    tool, _, _ = control_tool()
    tool._which = lambda name: None if name == "ghostapp" else f"/usr/bin/{name}"
    assert tool.validate_arguments(arguments) is valid


def test_a_launch_the_desktop_confirms_returns_a_verified_receipt() -> None:
    tool, manager, recorder = control_tool()
    result = tool.execute({"target": "app", "action": "launch", "name": "chromium"})
    assert result.success and result.action_receipt.status == "verified"
    assert result.action_receipt.action == "launch chromium"
    assert manager.calls == [("launch", "chromium", None)]
    assert recorder.events == ["manage:launch"]


def test_a_launch_that_lands_on_a_workspace_passes_it_through() -> None:
    tool, manager, _ = control_tool()
    result = tool.execute(
        {"target": "app", "action": "launch", "name": "chromium", "workspace": 1}
    )
    assert result.success
    assert manager.calls == [("launch", "chromium", 1)]


def test_an_unconfirmed_launch_is_unverified_not_a_false_success() -> None:
    tool, _, _ = control_tool(launch_to=False)
    result = tool.execute({"target": "app", "action": "launch", "name": "chromium"})
    assert not result.success and result.action_receipt.status == "unverified"
    assert "no new window appeared" in result.output


def test_a_refused_act_is_a_failed_receipt() -> None:
    tool, _, _ = control_tool(raise_to="Hyprland rejected the request.")
    result = tool.execute({"target": "window", "action": "close", "id": "0xfeedface"})
    assert not result.success and result.action_receipt.status == "failed"
    assert "rejected" in result.output


def test_close_checks_the_live_list_before_acting() -> None:
    tool, manager, _ = control_tool()
    result = tool.execute({"target": "window", "action": "close", "id": "0x999999"})
    assert not result.success and result.action_receipt.status == "missing"
    assert manager.calls == []  # never dispatched at a window that is not there
    result = tool.execute({"target": "window", "action": "close", "id": "0xfeedface"})
    assert result.success and result.action_receipt.status == "verified"
    assert manager.calls == [("close", "0xfeedface")]


def test_move_requires_and_carries_the_workspace() -> None:
    tool, manager, _ = control_tool()
    result = tool.execute(
        {"target": "window", "action": "move", "id": "0xfeedface", "workspace": 4}
    )
    assert result.success and result.action_receipt.status == "verified"
    assert manager.calls == [("move", "0xfeedface", 4)]


def test_focus_through_control_is_the_same_verified_path() -> None:
    tool, manager, _ = control_tool()
    result = tool.execute({"target": "window", "action": "focus", "id": "0xfeedface"})
    assert result.success and "confirmed" in result.output
    assert manager.calls == []  # focus is the activator's, not the manager's


def test_a_desktop_without_a_manager_refuses_every_control_act() -> None:
    desktop, _ = make_desktop(windows=default_windows())
    tool = DesktopControlTool(desktop, which=lambda name: "/usr/bin/x")
    assert tool.validate_arguments(
        {"target": "app", "action": "launch", "name": "chromium"}
    )
    result = tool.execute({"target": "app", "action": "launch", "name": "chromium"})
    assert not result.success and "cannot manage windows" in result.output


def test_control_preview_names_the_literal_program_or_window() -> None:
    tool, _, _ = control_tool()
    preview = tool.preview(
        ApprovalRequest(
            "desktop_control",
            {"target": "app", "action": "launch", "name": "chromium", "workspace": 1},
        )
    )
    assert preview is not None
    joined = " ".join(preview.detail_lines)
    assert "chromium" in joined and "workspace 1" in joined and NOTE in joined
    preview = tool.preview(
        ApprovalRequest(
            "desktop_control",
            {"target": "window", "action": "close", "id": "0xfeedface"},
        )
    )
    assert preview is not None
    joined = " ".join(preview.detail_lines)
    assert "firefox" in joined and "0xfeedface" in joined and "not undoable" in joined


def test_control_preview_announces_a_refusal_for_an_unknown_window() -> None:
    tool, _, _ = control_tool()
    preview = tool.preview(
        ApprovalRequest(
            "desktop_control",
            {"target": "window", "action": "move", "id": "0x999999", "workspace": 2},
        )
    )
    assert preview is not None
    assert "refused at execution" in " ".join(preview.detail_lines)


def test_action_summary_names_the_desktop_control_target() -> None:
    assert "chromium" in action_summary(
        ApprovalRequest(
            "desktop_control",
            {"target": "app", "action": "launch", "name": "chromium", "workspace": 1},
        )
    )
    assert "0xfeedface" in action_summary(
        ApprovalRequest("desktop_control", {"target": "window", "action": "close", "id": "0xfeedface"})
    )
