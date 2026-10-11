"""Which microphone the always-open ear actually takes.

A capture that names no node takes PipeWire's *default source*, and on a
laptop with a Bluetooth headset connected that source is the headset's own
microphone — which pins the headset into its hands-free profile and downgrades
its playback to mono call-quality for as long as the capture holds it
(``docs/VOICE.md``). The laptop's built-in input has no such coupling, so the
ear should not be paying that price when another microphone exists.

Three rules keep this small enough to trust:

* A Bluetooth input is never the automatic choice while any other input
  exists. The reason is not that a headset is a poor microphone, but that
  the default source lies: measured on this machine, ``pactl info`` reported
  ``Default Source: bluez_input…`` for a headset that was connected, playing
  stereo, and holding no capture at all. The first unnamed capture is what
  makes PipeWire create that node, and BlueZ answers by switching the card's
  profile. The damage therefore comes from the command line that looked like
  the safe, do-nothing option, and no reading of "what are the candidates
  right now" would have seen it coming.
* The ordering inside that one exclusion is WirePlumber's, not ours.
  ``priority.session`` decides which non-Bluetooth input wins, so a
  deliberately plugged USB microphone can beat the built-in instead of losing
  to a hardcoded device ranking.
* A node that is not ALSA hardware — an echo-cancelled capture, any filter
  source — is listed for the owner to choose and never chosen for her. Such a
  node exists only while its module is loaded, and which playback it cancels is
  not visible from its properties, so a wrong automatic pick records the
  loudspeaker instead of the room.
* Ignorance is the answer to any doubt. No ``pw-dump``, a probe that stalls,
  unparsable output, no candidate at all, a machine whose only input *is* a
  headset — all of it means ``None``, which is what the callers already did
  with ``None``: name no target. A probe must never become a reason voice
  stops working, and an owner with no microphone but the headset keeps the
  headset rather than losing voice entirely.

The owner's own words outrank all three: a node named in Settings is used
exactly as it was written, headset included.

A node name is hardware identity, not a secret: it is safe in a command line
and in ``config.json``, and the Settings panel shows it to the owner.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from stella.childproc import guarded_popen

#: How long the one probe may take. It runs on the wake path at startup, and
#: a stalled PipeWire is not worth a slow launch — or a hung one.
PROBE_TIMEOUT_SECONDS = 1.0

#: ALSA capture nodes only. This prefix is what identifies real hardware;
#: a node outside it is a filter or virtual device (see ``VIRTUAL``), which
#: the panel may offer and the automatic pick never names.
INPUT_PREFIX = "alsa_input."

#: A monitor is the *playback* side of a device, never a microphone. On this
#: machine every monitor node already carries ``media.class=Audio/Sink`` and so
#: is filtered out before this leg; it stays because a node that answers with
#: the name of a monitor is not something to hand a recording.
MONITOR_SUFFIX = ".monitor"

BUILTIN = "builtin"
USB = "usb"
BLUETOOTH = "bluetooth"
OTHER = "other"
#: A source that is not a piece of hardware: the echo-cancelled capture a
#: filter module made, a virtual sink's input, anything else with no ALSA
#: device behind its name. Selectable, because an owner who set up that pair
#: has to be able to point Stella at it; never automatic, because a filter is
#: there only while its module is loaded.
VIRTUAL = "virtual"


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
    """Classify from the device segment of the node name.

    A name outside ``alsa_input.`` has no ALSA device segment to read at all:
    an echo-cancelled capture is named whatever its module was told to call it
    (``ec_mic``, ``echo-cancel_capture``, anything), so the only honest answer
    about a name like that is that it is not hardware.
    """

    if not node_name.startswith(INPUT_PREFIX):
        return VIRTUAL
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
    """Every capture node in a ``pw-dump`` document, hardware or filter.

    Pure text in, data out, and ``()`` for anything that is not understood —
    this is the part tested without a running PipeWire.

    ``media.class=Audio/Source`` is what does the excluding: it leaves out
    playback, MIDI and video, and on this machine it also leaves out every
    monitor, because PipeWire reports a monitor as an ``Audio/Sink``. Names
    outside ``alsa_input.`` are kept rather than dropped, and marked
    :data:`VIRTUAL`: an echo-cancelled capture is named whatever the owner told
    the module to call it, so no name pattern can recognise it, and a source
    she set up by hand is a source she must be able to choose. Nothing is
    *selected* from that group automatically — see
    :func:`pick_preferred_source` — so widening what is listed costs an owner
    who never opens the row nothing at all.
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
        if not isinstance(name, str) or not name:
            continue
        if name.endswith(MONITOR_SUFFIX):
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
    """The input to aim at, or ``None`` when there is nothing to aim at.

    Any non-Bluetooth *hardware* input is worth naming by hand, because the
    alternative is a capture that follows PipeWire's default source — and a
    default left over from a headset's hands-free profile creates that profile
    the moment it is read, which is the thing the owner can neither see nor hear
    coming. A machine whose only inputs are Bluetooth keeps them: refusing there
    would take voice away rather than protect it.

    A :data:`VIRTUAL` source is never the automatic answer, however high its
    priority. A filter node exists only while the module that made it is
    loaded, and it is not self-evident which playback it was paired with: an
    echo-cancelled capture bound to a sink nobody is playing through records the
    loudspeaker — the measured failure this module exists to avoid, with 23% of
    playback frames reading as speech. So the cancelled pair is offered to the
    owner and chosen on purpose, or not used at all.
    """

    for source in ranked(sources):
        if source.kind in (BLUETOOTH, VIRTUAL):
            continue
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


def automatic_capture_source() -> str | None:
    """The node the ear should take when the owner named none.

    Probed on every call rather than remembered for the life of the process:
    a headset that connects after Stella started is precisely the event the
    answer has to notice, and the cache was how the first version of this rule
    still let the ear take the headset — the pick was made at startup, when no
    headset existed, and the unnamed capture it produced then followed a
    default that did change. One ``pw-dump`` is ~13 ms on this machine
    (measured 2026-10-11) and callers ask once per capture open.
    """

    picked = pick_preferred_source(discover_capture_sources())
    return picked.name if picked is not None else None


def resolve_capture_source(explicit: str | None) -> str | None:
    """What to hand ``capture_command``: the owner's node, or the automatic pick."""

    if explicit:
        return explicit
    return automatic_capture_source()


def live_capture_target(explicit: str | None) -> Callable[[], str | None]:
    """A microphone choice re-probed at the moment a capture opens.

    The raw-stream listeners are handed :func:`barge_in.live_capture_command`
    because they build a whole command line; a capture that writes its audio to
    a file needs only the node name to append to a command line it already
    owns, and this is the same decision without the argv. A recorder built with
    a name frozen at startup records the microphone that existed when Stella
    launched, which is the bug this shape exists to prevent.
    """

    def resolve() -> str | None:
        return resolve_capture_source(explicit)

    return resolve
