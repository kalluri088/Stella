"""The sway adapter, against the contract rather than a measured machine.

The machine these tests were written on runs Hyprland, so nothing here
claims to be the truth about sway — it is the truth about the *decisions*
the adapter makes: the proof gate (a sway reply, not an i3 one), the
None-versus-empty split, the id validation before any command line, the
act-then-re-query, and the exact argv shapes. The fake compositor answers
the way the reports say compositors do (garbage with rc 0, a bare i3
reply on a leftover socket), and every assertion is at argv level so the
choices cannot be "simplified" away. The facts marked UNVERIFIED in the
module docstring still need the real-machine pass; these tests are what
makes that pass safe to run at all.
"""

import json

import pytest

from stella.desktop.backends.sway import WINDOW_ID_RE, Sway, probe
from stella.desktop.capabilities import (
    CaptureUnavailable,
    Region,
    SendUnavailable,
)
from stella.desktop.runner import Completed

SOCKET = "/run/user/1000/sway-ipc.1000.7.sock"
ENV = {
    "SWAYSOCK": SOCKET,
    "WAYLAND_DISPLAY": "wayland-1",
    "XDG_RUNTIME_DIR": "/run/user/1000",
}

SWAY_VERSION = {
    "major": 1,
    "minor": 8,
    "patch": 1,
    "human_readable": "1.8.1 (v1.8.1 - 9ab2d708)",
    "variant": "sway",
}
I3_VERSION = {
    "major": 4,
    "minor": 23,
    "patch": 1,
    "human_readable": "4.23 (2023-10-29)",
}

# The tree fixtures: windows are the nodes with app_id or window; the
# ids 1/3/5/6/9 belong to containers and 50 to a layer surface, none of
# which may ever appear as a Window.
FOOT = {
    "id": 42,
    "name": "Shell - ~",
    "type": "con",
    "app_id": "foot",
    "pid": 4001,
    "rect": [10, 20, 749, 402],
    "workspace_info": {"num": 2, "name": "2"},
    "nodes": [],
}
FIREFOX = {
    "id": 43,
    "name": "Error page - Mozilla Firefox",
    "type": "con",
    "window": 8388609,
    "window_properties": {"class": "firefox", "instance": "Firefox"},
    "pid": 55,
    "rect": [779, 20, 700, 500],
    "workspace_info": {"num": 1, "name": "1"},
    "nodes": [],
}
UNTITLED = {
    "id": 44,
    "name": None,
    "type": "con",
    "app_id": "zathura",
    "pid": None,
    "workspace_info": {"num": 3, "name": "3"},
    "nodes": [],
}
FLOATING = {
    "id": 45,
    "name": "Output Devices",
    "type": "con",
    "app_id": "pavucontrol",
    "pid": 60,
    "rect": [100, 100, 400, 300],
    "workspace_info": {"num": 2, "name": "2"},
    "nodes": [],
}
LAYERSURFACE = {
    "id": 50,
    "name": "swaybar",
    "type": "con",
    "app_id": "swaybar",
    "shell": "layer",
    "pid": 7,
    "rect": [0, 0, 1920, 24],
    "nodes": [],
}


def tree(focused_ids=()) -> list:
    """A sway-shaped get_tree answer around the fixture windows."""

    def mark(window: dict) -> dict:
        return dict(window, focused=window["id"] in focused_ids)

    leaf_container = {
        "id": 9,
        "type": "con",
        "layout": "splith",
        "focused": False,
        # a container's id is not the window's id — it must not surface
        "nodes": [mark(FOOT), mark(FIREFOX)],
        "floating_nodes": [],
    }
    workspace = {
        "id": 5,
        "type": "workspace",
        "num": 2,
        "name": "2",
        "focused": False,
        "nodes": [leaf_container, mark(UNTITLED)],
        "floating_nodes": [mark(FLOATING)],
    }
    dock = {
        "id": 6,
        "type": "dockarea",
        "focused": False,
        "nodes": [mark(LAYERSURFACE)],
        "floating_nodes": [],
    }
    output = {"id": 3, "type": "output", "focused": False, "nodes": [workspace, dock]}
    root = {"id": 1, "type": "root", "focused": False, "nodes": [output]}
    return [root]


class FakeRunner:
    """Answers by argv prefix; a list responder pops in script order.

    Every scripted answer is a real ``Completed``: the adapter shape-
    checks even the seam's reply, and test_even_a_broken_seam reads as
    unusable below proves what happens when it does not get one.
    """

    def __init__(self, **responders) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.responders = dict(responders)

    def __call__(self, argv, *, timeout, stdin=None, env=None) -> Completed:
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

    def swaymsg(self) -> list[tuple]:
        return [argv for argv, _ in self.calls if argv[0] == "swaymsg"]

    def first(self, binary: str) -> tuple:
        return next(argv for argv, _ in self.calls if argv[0] == binary)

    def env_of(self, binary: str) -> dict:
        return next(
            kwargs["env"] for argv, kwargs in self.calls if argv[0] == binary
        )


def done(returncode=0, stdout=b"", stderr=b"") -> Completed:
    return Completed(returncode, stdout, stderr)


def json_done(payload) -> Completed:
    return done(0, json.dumps(payload).encode("utf-8"))


PNG = b"\x89PNG fake"


def all_binaries(_name: str) -> str:
    return "/usr/bin/fake"


def make_sway(responders, *, env: dict | None = ENV) -> tuple[Sway, FakeRunner]:
    runner = FakeRunner(**responders)
    sway = Sway.from_environment(env or {})
    return (
        Sway(
            sway.socket,
            wayland_display=sway.wayland_display,
            xdg_runtime_dir=sway.xdg_runtime_dir,
            runner=runner,
        ),
        runner,
    )


def find(windows, window_id: str):
    return next(window for window in windows if window.id == window_id)


# ------------------------------------------------------------------- probe


def test_probe_without_a_swaysock_hint_never_spawns() -> None:
    # A desktop with no marker in the environment is not claimed, and
    # the unscripted FakeRunner proves nothing was ever asked.
    runner = FakeRunner()
    assert (
        probe({"WAYLAND_DISPLAY": "wayland-1"}, runner, which=all_binaries) is None
    )
    assert probe({}, runner, which=all_binaries) is None
    assert runner.calls == []


@pytest.mark.parametrize(
    "answer",
    [
        json_done(I3_VERSION),  # the leftover-socket trap: bare i3 is not sway
        json_done(dict(I3_VERSION, variant=None)),
        done(0, b"Invalid socket path\n"),
        done(0, b""),
        json_done([SWAY_VERSION]),  # right answer, wrong top-level shape
    ],
)
def test_probe_demands_an_answer_that_is_sway_specifically(answer) -> None:
    sway, runner = make_sway({"swaymsg": answer})
    assert sway.version() is None
    desktop = probe(ENV, FakeRunner(swaymsg=answer), which=all_binaries)
    assert desktop is None
    # the proof question is asked once; nothing after it spawns
    assert len(runner.calls) == 1


def test_probe_returns_a_desktop_naming_the_socket_and_the_reply() -> None:
    desktop = probe(
        ENV, FakeRunner(swaymsg=json_done(SWAY_VERSION)), which=all_binaries
    )
    assert desktop is not None
    assert desktop.name == "sway"
    assert SOCKET in desktop.session_note  # rule 6: which session is touched
    assert "1.8.1" in desktop.session_note  # and the answer that proved it
    # one complete object: every capability is this same session client
    assert desktop.reader is desktop.activator
    assert desktop.capture is desktop.keys is desktop.reader


def test_probe_spawns_exactly_the_one_proving_question() -> None:
    runner = FakeRunner(swaymsg=json_done(SWAY_VERSION))
    assert probe(ENV, runner, which=all_binaries) is not None
    assert runner.argvs() == [("swaymsg", "-t", "get_version")]
    assert runner.env_of("swaymsg")["SWAYSOCK"] == SOCKET  # explicit session


@pytest.mark.parametrize("missing", ["swaymsg", "grim", "wtype", "tesseract"])
def test_probe_gates_on_each_binary_without_spawning(missing: str) -> None:
    runner = FakeRunner()

    def which(name: str) -> str | None:
        return None if name == missing else "/usr/bin/x"

    assert probe(ENV, runner, which=which) is None
    assert runner.calls == []


# ------------------------------------------------------------------ reads


def test_windows_walks_the_tree_and_reports_only_window_ids() -> None:
    sway, runner = make_sway({"swaymsg": json_done(tree(focused_ids=(42,)))})
    windows = sway.windows()
    assert windows is not None
    assert sorted(window.id for window in windows) == ["42", "43", "44", "45"]
    foot = find(windows, "42")
    assert (foot.class_name, foot.title, foot.pid) == ("foot", "Shell - ~", 4001)
    assert foot.workspace == "2"
    assert foot.region == Region(10, 20, 749, 402)
    firefox = find(windows, "43")  # X11 window: class from window_properties
    assert firefox.class_name == "firefox"
    assert firefox.workspace == "1"
    assert runner.first("swaymsg") == ("swaymsg", "-t", "get_tree")
    assert runner.env_of("swaymsg")["SWAYSOCK"] == SOCKET


def test_floating_nodes_are_windows_and_layer_surfaces_are_not() -> None:
    sway, _ = make_sway({"swaymsg": json_done(tree())})
    windows = sway.windows()
    assert windows is not None
    assert find(windows, "45").class_name == "pavucontrol"  # floating_nodes
    assert all(window.id != "50" for window in windows)  # the swaybar surface
    assert all(window.id not in {"1", "3", "5", "6", "9"} for window in windows)


def test_an_untitled_window_is_still_a_window() -> None:
    sway, _ = make_sway({"swaymsg": json_done(tree())})
    windows = sway.windows()
    assert windows is not None
    zathura = find(windows, "44")
    assert (zathura.title, zathura.pid) == ("", 0)  # nulls read honestly
    assert zathura.region is None  # no rect is no geometry, not no window


@pytest.mark.parametrize(
    "garbage",
    [
        b"",
        b"socket path does not exist\n",
        b"[not json",
        b'{"id": 1}',  # right JSON, wrong top-level shape
        b'[{"id": 1}, 7]',  # a node that is not an object
        b'[{"id": 1, "nodes": 7}]',  # children that are not a list
    ],
)
def test_reads_refuse_unshaped_trees_even_at_rc_zero(garbage: bytes) -> None:
    # The rc=0-with-garbage trap has no proof of being sway-exclusive,
    # and rule 1 says an unusable answer is never an empty list.
    sway, _ = make_sway({"swaymsg": done(0, garbage, b"")})
    assert sway.windows() is None
    assert sway.active_window() is None


def test_an_empty_tree_is_a_real_empty_list_not_an_unusable_answer() -> None:
    sway, _ = make_sway({"swaymsg": json_done([])})
    assert sway.windows() == []
    root_only = [{"id": 1, "type": "root", "nodes": []}]
    sway, _ = make_sway({"swaymsg": json_done(root_only)})
    assert sway.windows() == []


def test_a_window_with_an_unaddressable_id_refuses_the_whole_answer() -> None:
    # One odd member is sway answering in a shape this adapter does not
    # know, which is the unusable branch, never a silently dropped row.
    odd = dict(tree()[0], nodes=[{"id": "42", "app_id": "foot", "name": "n", "pid": 1}])
    sway, _ = make_sway({"swaymsg": json_done([odd])})
    assert sway.windows() is None


def test_active_window_is_the_focused_leaf() -> None:
    sway, _ = make_sway({"swaymsg": json_done(tree(focused_ids=(43,)))})
    window = sway.active_window()
    assert window is not None
    assert (window.id, window.class_name) == ("43", "firefox")


def test_a_usable_tree_with_nothing_focused_is_honest_about_its_none() -> None:
    # The contract gives None for "did not answer in a trusted form" and
    # has no third value for "answered, nothing is focused"; the module
    # docstring records the ambiguity instead of hiding it.
    sway, _ = make_sway({"swaymsg": json_done(tree())})
    assert sway.windows() is not None
    assert sway.active_window() is None


# ------------------------------------------------------------------ acts


def test_activate_sends_the_con_id_selector_and_re_queries() -> None:
    sway, runner = make_sway(
        {
            "swaymsg": [
                done(0, b'{"success": true}\n', b""),  # the claim, ignored
                json_done(tree(focused_ids=(43,))),
            ]
        }
    )
    assert sway.activate("43") is True
    assert runner.swaymsg()[0] == ("swaymsg", "[con_id=43] focus")
    assert runner.swaymsg()[1] == ("swaymsg", "-t", "get_tree")
    assert runner.env_of("swaymsg")["SWAYSOCK"] == SOCKET


@pytest.mark.parametrize(
    "requery",
    [
        json_done(tree(focused_ids=(42,))),  # swaymsg claimed, sway disagrees
        json_done(tree()),  # nothing focused anymore
        done(0, b"garbage", b""),  # and an unusable re-query is no proof
    ],
)
def test_activate_is_false_unless_the_re_query_confirms(requery) -> None:
    sway, _ = make_sway({"swaymsg": [done(0, b'{"success": true}\n'), requery]})
    assert sway.activate("43") is False


@pytest.mark.parametrize(
    "window_id",
    [
        "42; exec touch /tmp/pwned",
        "[con_id=42] focus && rm -rf",
        "foot",
        "-1",
        "1234567890",  # ten digits is not the bounded shape
        "4 2",
        "",
    ],
)
def test_malformed_ids_never_reach_a_command_line(window_id: str) -> None:
    sway, runner = make_sway({"swaymsg": json_done(tree())})
    assert sway.activate(window_id) is False
    assert runner.calls == []  # rule 5: validated before any spawn


def test_ids_must_look_like_sway_window_ids() -> None:
    assert WINDOW_ID_RE.match("42")
    assert WINDOW_ID_RE.match("999999999")
    assert not WINDOW_ID_RE.match("1000000000")
    assert not WINDOW_ID_RE.match("42; exec touch /tmp/pwned")


# ------------------------------------------------------------ fail closed


def test_a_missing_socket_fails_closed_without_spawning() -> None:
    runner = FakeRunner()
    sway = Sway(None, runner=runner)
    assert sway.version() is None
    assert sway.active_window() is None
    assert sway.windows() is None
    assert sway.activate("1") is False
    with pytest.raises(CaptureUnavailable):
        sway.capture(None)
    with pytest.raises(SendUnavailable):
        sway.send_text("hi")
    assert runner.calls == []


def test_even_a_broken_seam_reads_as_unusable_not_as_a_crash() -> None:
    # The injected runner's reply is input too: a runner that answers
    # with something that is not a Completed must not take the registry
    # (or the stub contract in test_tools.py) down with it.
    sway = Sway(SOCKET, runner=lambda *args, **kwargs: None)
    assert sway.version() is None
    assert sway.windows() is None
    assert sway.activate("42") is False
    with pytest.raises(CaptureUnavailable):
        sway.capture(None)
    with pytest.raises(SendUnavailable):
        sway.send_text("hi")


# ---------------------------------------------------------- capture/typing


def test_capture_mirrors_the_measured_grim_invocation() -> None:
    sway, runner = make_sway({"grim": done(0, PNG, b"")})
    assert sway.capture(Region(10, 20, 749, 402)) == PNG
    # The geometry string is UNVERIFIED for sway; the *invocation shape*
    # (trailing "-" after options, explicit Wayland env) is what mirrors
    # the measured Hyprland grim call and what these asserts pin.
    assert runner.first("grim") == ("grim", "-g", "10,20 749x402", "-")
    env = runner.env_of("grim")
    assert env["WAYLAND_DISPLAY"] == "wayland-1"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"


def test_full_screen_capture_is_grim_with_only_the_stdout_target() -> None:
    sway, runner = make_sway({"grim": done(0, PNG, b"")})
    assert sway.capture(None) == PNG
    assert runner.first("grim") == ("grim", "-")


@pytest.mark.parametrize(
    "answer",
    [
        done(1, b"", b"failed to capture"),
        done(0, b"not a png", b""),  # rc 0 and garbage, again
    ],
)
def test_a_non_png_capture_raises_one_safe_message(answer) -> None:
    sway, runner = make_sway({"grim": answer})
    with pytest.raises(CaptureUnavailable) as failure:
        sway.capture(Region(0, 0, 10, 10))
    assert "grim" in str(failure.value)
    assert runner.first("grim")[-1] == "-"  # PNG to stdout, never a file


def test_send_text_types_only_into_the_focused_surface() -> None:
    sway, runner = make_sway({"wtype": done(0)})
    assert sway.send_text("echo hi") is None
    assert runner.first("wtype") == ("wtype", "echo hi")
    assert runner.env_of("wtype")["WAYLAND_DISPLAY"] == "wayland-1"
    # there is no window-id argument anywhere on this route (rule 3)
    assert not any("con_id" in part for part in runner.first("wtype"))


def test_a_failing_wtype_raises_instead_of_claiming_delivery() -> None:
    sway, _ = make_sway({"wtype": done(1, b"", b"not a keyboard")})
    with pytest.raises(SendUnavailable) as failure:
        sway.send_text("hi")
    assert "wtype" in str(failure.value)
