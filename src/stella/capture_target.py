"""Which microphone the always-open ear actually takes.

A capture that names no node takes PipeWire's *default source*, and on a
laptop with a Bluetooth headset connected that source is the headset's own
microphone — which pins the headset into its hands-free profile and downgrades
its playback to mono call-quality for as long as the capture holds it
(``docs/VOICE.md``). The laptop's built-in input has no such coupling, so the
ear should not be paying that price when another microphone exists.

Three rules keep this small enough to trust:

* The deviation is narrow on purpose. Nothing is chosen here unless the
  highest-priority input **is** a Bluetooth mic and some non-Bluetooth input
  exists; every other machine gets exactly the argv it got yesterday.
* The ordering is WirePlumber's, not ours. ``priority.session`` decides which
  non-Bluetooth input wins, so a deliberately plugged USB microphone can beat
  the built-in instead of losing to a hardcoded device ranking.
* Ignorance is the answer to any doubt. No ``pw-dump``, a probe that stalls,
  unparsable output, one candidate — all of it means ``None``, which is what
  the callers already did with ``None``: name no target. A probe must never
  become a reason voice stops working.

A node name is hardware identity, not a secret: it is safe in a command line
and in ``config.json``, and the Settings panel shows it to the owner.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from stella.childproc import guarded_popen

#: How long the one probe may take. It runs on the wake path at startup, and
#: a stalled PipeWire is not worth a slow launch — or a hung one.
PROBE_TIMEOUT_SECONDS = 1.0

#: ALSA capture nodes only. This prefix is what excludes monitor streams, the
#: echo-cancelled ``ec_mic`` filter, MIDI and video devices from the list.
INPUT_PREFIX = "alsa_input."

BUILTIN = "builtin"
USB = "usb"
BLUETOOTH = "bluetooth"
OTHER = "other"


@dataclass(frozen=True)
class CaptureSource:
    """One input Stella could aim the ear at."""

    #: The PipeWire node name, which is what ``pw-record --target`` accepts.
    name: str
    #: What the owner reads in the Settings panel.
    label: str
    #: ``builtin`` / ``usb`` / ``bluetooth`` / ``other``.
    kind: str
    #: ``priority.session`` — WirePlumber's own preference, higher wins.
    priority: int


def kind_of(node_name: str) -> str:
    """Classify from the device segment of the node name."""

    tail = node_name[len(INPUT_PREFIX) :]
    if tail.startswith("pci-"):
        return BUILTIN
    if tail.startswith("usb-"):
        return USB
    if tail.startswith("bluez_"):
        return BLUETOOTH
    return OTHER


def _props_of(obj: object) -> dict[str, object] | None:
    if not isinstance(obj, dict):
        return None
    info = obj.get("info")
    if not isinstance(info, dict):
        return None
    props = info.get("props")
    return props if isinstance(props, dict) else None


def capture_sources(dump: str) -> tuple[CaptureSource, ...]:
    """Every ALSA capture node in a ``pw-dump`` document.

    Pure text in, data out, and ``()`` for anything that is not understood —
    this is the part tested without a running PipeWire.
    """

    try:
        objects = json.loads(dump)
    except (TypeError, ValueError):
        return ()
    if not isinstance(objects, list):
        return ()

    found: list[CaptureSource] = []
    for obj in objects:
        props = _props_of(obj)
        if props is None or props.get("media.class") != "Audio/Source":
            continue
        name = props.get("node.name")
        if not isinstance(name, str) or not name.startswith(INPUT_PREFIX):
            continue
        label = props.get("node.description") or props.get("node.nick") or name
        raw_priority = props.get("priority.session")
        priority = (
            raw_priority
            if isinstance(raw_priority, int) and not isinstance(raw_priority, bool)
            else 0
        )
        found.append(
            CaptureSource(
                name=name,
                label=str(label),
                kind=kind_of(name),
                priority=priority,
            )
        )
    return tuple(found)


def ranked(sources: tuple[CaptureSource, ...]) -> tuple[CaptureSource, ...]:
    """WirePlumber's preference first; the name breaks a tie deterministically."""

    return tuple(sorted(sources, key=lambda source: (-source.priority, source.name)))


def pick_preferred_source(
    sources: tuple[CaptureSource, ...]
) -> CaptureSource | None:
    """The input to aim at, or ``None`` when nothing needs aiming.

    Returns a node only in the one case worth the deviation: the input
    PipeWire would have taken is a Bluetooth headset's microphone, and there
    is another input that is not.
    """

    ordered = ranked(sources)
    if len(ordered) < 2 or ordered[0].kind != BLUETOOTH:
        return None
    for source in ordered:
        if source.kind != BLUETOOTH:
            return source
    return None


def discover_capture_sources() -> tuple[CaptureSource, ...]:
    """Ask PipeWire what inputs exist; ``()`` whenever it will not say."""

    if shutil.which("pw-dump") is None:
        return ()
    process: subprocess.Popen[str] | None = None
    try:
        process = guarded_popen(
            ["pw-dump"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            stdout, _stderr = process.communicate(timeout=PROBE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, _stderr = process.communicate()
    except Exception:  # noqa: BLE001 - a probe is never a user-facing failure
        return ()
    return capture_sources(stdout or "")


_PROBE_RESOLVED = False
_PROBE_PICK: str | None = None


def automatic_capture_source() -> str | None:
    """The node the ear should take when the owner named none, probed once."""

    global _PROBE_RESOLVED, _PROBE_PICK
    if not _PROBE_RESOLVED:
        picked = pick_preferred_source(discover_capture_sources())
        _PROBE_PICK = picked.name if picked is not None else None
        _PROBE_RESOLVED = True
    return _PROBE_PICK


def reset_capture_source_probe() -> None:
    """Forget the probe, for the tests that fake it."""

    global _PROBE_RESOLVED, _PROBE_PICK
    _PROBE_RESOLVED = False
    _PROBE_PICK = None


def resolve_capture_source(explicit: str | None) -> str | None:
    """What to hand ``capture_command``: the owner's node, or the automatic pick."""

    if explicit:
        return explicit
    return automatic_capture_source()
