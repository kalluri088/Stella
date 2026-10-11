"""Which microphone the ear takes: parsing, the rule, and the probe's silence.

Nothing here needs PipeWire. The dump is text written out in these tests in
the shape ``pw-dump`` really produces (verified against this machine), the
rule is a pure function over ``CaptureSource`` values, and the probe is
driven by a fake ``guarded_popen``. What the suite is really guarding is the
outcome the owner hears: a wired input is named, so the capture never follows
a default source that happens to be a headset's microphone.
"""

import json
import subprocess

import pytest

from stella.capture_target import (
    BLUETOOTH,
    BUILTIN,
    USB,
    CaptureSource,
    automatic_capture_source,
    capture_sources,
    discover_capture_sources,
    kind_of,
    pick_preferred_source,
    ranked,
    resolve_capture_source,
)

BUILT_IN = "alsa_input.pci-0000_00_1f.3.analog-stereo"
HEADSET = "alsa_input.bluez_1C_71_29_0A_7B_D8.headset-head-unit"
USB_MIC = "alsa_input.usb-Samsung_Samson_Q9U-00.analog-stereo"


def _node(
    name: str,
    priority: int = 2000,
    media_class: str = "Audio/Source",
    description: str | None = None,
) -> dict:
    """One object in the shape ``pw-dump`` emits for a node."""

    props: dict[str, object] = {
        "media.class": media_class,
        "node.name": name,
        "priority.session": priority,
    }
    if description is not None:
        props["node.description"] = description
    return {"type": "PipeWire:Interface:Node", "info": {"props": props}}


def _dump(*nodes: dict) -> str:
    return json.dumps(list(nodes))


def _source(name: str, priority: int) -> CaptureSource:
    return capture_sources(_dump(_node(name, priority)))[0]


# ------------------------------------------------------------------ parsing


def test_capture_sources_reads_audio_source_nodes_only() -> None:
    client = {
        "type": "PipeWire:Interface:Client",
        "info": {"props": {"node.name": BUILT_IN}},
    }
    dump = _dump(
        _node("alsa_output.pci-0000_00_1f.3.analog-stereo", 1009, "Audio/Sink"),
        # The monitor of the built-in input: an alsa_input name, but a sink.
        _node(f"{BUILT_IN}.monitor", 2000, "Audio/Sink"),
        _node("v4l2_input.pci-0000_00_14_0-usb-0_6_1_0", 1000, "Video/Source"),
        _node("echo-cancel_capture", 2000, "Audio/Source"),
        client,
        _node(BUILT_IN, 2009, "Audio/Source", "Built-in Audio Analog Stereo"),
    )
    sources = capture_sources(dump)
    assert [source.name for source in sources] == [BUILT_IN]
    assert sources[0].label == "Built-in Audio Analog Stereo"
    assert sources[0].priority == 2009


def test_capture_sources_classifies_builtin_usb_bluetooth() -> None:
    sources = capture_sources(
        _dump(
            _node(BUILT_IN),
            _node(USB_MIC),
            _node(HEADSET),
            _node("alsa_input.virtual-mic.stereo-fallback"),
        )
    )
    assert [source.kind for source in sources] == [BUILTIN, USB, BLUETOOTH, "other"]
    assert kind_of(HEADSET) == BLUETOOTH


def test_a_node_is_labelled_by_nick_then_name_when_it_has_no_description() -> None:
    node = _node(BUILT_IN)
    node["info"]["props"]["node.description"] = ""
    node["info"]["props"]["node.nick"] = "Internal mic"
    assert capture_sources(_dump(node))[0].label == "Internal mic"
    node["info"]["props"]["node.nick"] = None
    assert capture_sources(_dump(node))[0].label == BUILT_IN


def test_capture_sources_returns_nothing_for_unparsable_text() -> None:
    for dump in ("", "not json at all", "{}", '{"info": 3}', "[]"):
        assert capture_sources(dump) == ()
    # A node whose properties are the wrong type is ignored, not raised on.
    assert capture_sources(_dump(_node(BUILT_IN))) != ()
    broken = _node(BUILT_IN)
    broken["info"]["props"]["priority.session"] = "2009"
    assert capture_sources(_dump(broken))[0].priority == 0


# ---------------------------------------------------------------- the rule


def test_pick_returns_none_when_there_is_nothing_to_name() -> None:
    assert pick_preferred_source(()) is None
    # A headset-only machine keeps the ear armed on the headset (with the
    # warning in the panel): refusing here would take voice away entirely.
    sources = capture_sources(
        _dump(_node(HEADSET, 2009), _node("alsa_output.fake", 100, "Audio/Sink"))
    )
    assert pick_preferred_source(sources) is None


def test_pick_names_the_built_in_even_when_no_headset_is_in_the_way() -> None:
    # The pin that changed on 2026-10-11, and the reason is measured rather
    # than theoretical: on this laptop ``pactl info`` reported ``Default
    # Source: bluez_input…`` while the headset was connected, playing stereo
    # and holding no capture. That node was not a source the ear could see
    # being chosen, so an unnamed capture was the command line that quietly
    # dragged the card into its hands-free profile. Naming any wired input
    # is what makes the remembered default irrelevant.
    sources = (_source(BUILT_IN, 2009), _source(USB_MIC, 2000))
    picked = pick_preferred_source(sources)
    assert picked is not None
    assert picked.name == BUILT_IN


def test_pick_leaves_a_headset_for_the_leading_non_bluetooth_input() -> None:
    sources = (
        _source(HEADSET, 2009),
        _source(USB_MIC, 2008),
        _source(BUILT_IN, 2007),
    )
    picked = pick_preferred_source(sources)
    assert picked is not None
    assert picked.name == USB_MIC
    assert [source.name for source in ranked(sources)] == [
        HEADSET,
        USB_MIC,
        BUILT_IN,
    ]


def test_ranking_breaks_a_priority_tie_by_name() -> None:
    # The tie-break is only there so two identical machines agree; nothing
    # about the pick depends on it.
    ordered = ranked((_source(USB_MIC, 5), _source(BUILT_IN, 5)))
    assert [source.name for source in ordered] == [BUILT_IN, USB_MIC]


# ------------------------------------------------------------------ probe


class _HangingDump:
    """A ``pw-dump`` child that never answers until it is killed."""

    def __init__(self) -> None:
        self.killed = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if timeout is not None:
            raise subprocess.TimeoutExpired(["pw-dump"], timeout)
        return "", ""

    def kill(self) -> None:
        self.killed = True


def test_probe_is_empty_when_pw_dump_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stella.capture_target.shutil.which", lambda name: None)

    def _never_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("the probe must not start a child it cannot find")

    monkeypatch.setattr("stella.capture_target.guarded_popen", _never_called)
    assert discover_capture_sources() == ()


def test_probe_kills_a_timed_out_dump_and_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _HangingDump()
    monkeypatch.setattr(
        "stella.capture_target.shutil.which", lambda name: "/usr/bin/pw-dump"
    )
    monkeypatch.setattr("stella.capture_target.guarded_popen", lambda *a, **k: child)
    assert discover_capture_sources() == ()
    assert child.killed is True


def test_probe_is_empty_when_the_child_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("no pipe for you")

    monkeypatch.setattr("stella.capture_target.shutil.which", lambda name: "pw-dump")
    monkeypatch.setattr("stella.capture_target.guarded_popen", _boom)
    assert discover_capture_sources() == ()


# ----------------------------------------------------------------- callers


def test_an_explicit_source_beats_the_probe_and_skips_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        "stella.capture_target.discover_capture_sources",
        lambda: calls.append(1) or (_source(HEADSET, 2009), _source(BUILT_IN, 2007)),
    )
    assert resolve_capture_source("some-node") == "some-node"
    assert calls == []


def test_the_automatic_pick_is_probed_afresh_on_every_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The resident server lives for hours and the microphones do not. The
    # first version of this rule remembered its answer for the life of the
    # process, so a headset that connected after the server started was never
    # seen by the rule that was supposed to step around it.
    asks: list[tuple[CaptureSource, ...]] = [
        (_source(BUILT_IN, 2007),),
        (_source(HEADSET, 2010), _source(BUILT_IN, 2007)),
    ]
    calls: list[int] = []

    def _discover() -> tuple[CaptureSource, ...]:
        found = asks[min(len(calls), len(asks) - 1)]
        calls.append(1)
        return found

    monkeypatch.setattr("stella.capture_target.discover_capture_sources", _discover)
    assert automatic_capture_source() == BUILT_IN
    assert automatic_capture_source() == BUILT_IN
    assert len(calls) == 2
    assert resolve_capture_source(None) == BUILT_IN
    assert len(calls) == 3


def test_the_automatic_answer_when_there_is_nothing_to_choose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stella.capture_target.discover_capture_sources", lambda: ())
    assert resolve_capture_source(None) is None
