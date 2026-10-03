"""Spoken-latency bench: streaming synthesis vs. the whole-reply path.

Gated on ``STELLA_VOICE_STREAM_BENCH=on`` for the same reason the round
trip is: this is the only file in the suite that spins up two real speech
engines on top of a real model. An ordinary ``pytest`` skips it, never
silently passes it, so the running suite stays cheap and honest.

Two arms, identical prompts, identical machine state, one number that
matters: how much earlier the first sentence reaches a rendered file.
Both arms run the same code except for the tiny switch that installs a
streaming callback — nothing else about generation changes, so any
difference in ``first_artifact`` is entirely attributable to streaming.

What this is **not**: a microphone test, a wake-word test, or a claim
about wall-clock "felt" latency. It measures the pipeline stages the
feature was added to overlap. A real conversation — opened to the owner's
ear and played to real speakers — is what turns these numbers into a
judgement about whether the feature was worth shipping.
"""

from __future__ import annotations

import os
import re
import statistics
import threading
import time
from pathlib import Path

import pytest

from stella import turntrace
from stella.app import (
    StellaApplication,
    StellaBridge,
    StellaSession,
    StellaSettings,
    VoicePanel,
    _build_speech_provider,
)
from stella.brain import LLMBrain
from stella.memory import InMemoryMemory
from stella.ollama_client import OllamaLLMClient
from stella.stella import Stella
from stella.tools import EchoTool, ToolDispatcher
from stella.voice import Player

_ON = {"1", "true", "on", "yes"}
_LINE = re.compile(r"^([a-z][a-z_]*),(\d+)$")

#: Every prompt is answered in three sentences by the local model. A
#: one-sentence answer hides the whole point of streaming, and a long
#: answer lets generation time dominate the number. Three sentences is
#: where the difference is real for an owner and small enough for one
#: bench pass to stay cheap.
PROMPTS = (
    "Give me three short reasons a person might take a walk at lunch.",
    "Describe what makes a good campfire site in three sentences.",
    "Tell me three things to check before a long drive.",
)

#: Stage order for the table; ``mark`` writes them in this order.
STAGES = (
    "turn_start",
    "decision_done",
    "first_answer_token",
    "first_sentence_ready",
    "first_artifact",
    "first_play",
    "answer_done",
    "turn_end",
)


@pytest.fixture(autouse=True)
def _require_the_switch() -> None:
    if (
        os.environ.get("STELLA_VOICE_STREAM_BENCH", "").strip().casefold()
        not in _ON
    ):
        pytest.skip(
            "the bench runs real speech engines and a real model; set "
            "STELLA_VOICE_STREAM_BENCH=on to ask for it"
        )


class _DiscardingPlayer(Player):
    """Report every artifact as played, then delete it on the spot.

    The bench's claim is about *when audio was ready*, not about what
    anybody heard, so the speaker never gets involved — and each
    artifact file leaves the disk the moment its ``play`` returns, so a
    run does not accumulate hundreds of wav files.
    """

    def available(self) -> bool:
        return True

    def play(self, path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def stop(self) -> None:
        return


def _real_stella(settings: StellaSettings) -> Stella:
    """The same core ``build_application`` builds, minus the tool belt.

    The Brain sees the prompts exactly as a real user would say them;
    only tools are stripped, and the answer-side prompt is untouched.
    That matters: streaming's claim is about answer synthesis, and any
    tool step on either arm would be noise in the comparison.
    """

    llm = OllamaLLMClient(
        model=settings.model or "qwen3:4b",
        base_url=settings.ollama_base_url,
        native=True,
        num_ctx=8192,
        decision_max_output_tokens=settings.decision_max_tokens,
        answer_max_output_tokens=settings.answer_max_tokens,
        think=settings.ollama_think,
    )
    tools = ToolDispatcher([EchoTool()])
    brain = LLMBrain(llm, tools=tools)
    return Stella(brain, llm, tools, InMemoryMemory())


def _panel(settings: StellaSettings) -> VoicePanel:
    speech = _build_speech_provider(settings)
    if speech is None:
        pytest.skip(
            "no local speech provider — set STELLA_SPEECH_COMMAND (and "
            "STELLA_SPEECH_RESIDENT=on) so the bench measures the "
            "resident worker a real launch would use"
        )
    panel = VoicePanel(
        recorder=None,
        player=_DiscardingPlayer(),
        transcriber=None,
        speech_provider=speech,
    )
    panel.speech_enabled = True
    panel.prewarm_speech()
    return panel


def _read_stamps(path: Path) -> dict[str, int]:
    marks: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        stage, stamp = match.group(1), int(match.group(2))
        # The one-shot flags already keep the file to one line per
        # stage; the guard is a safety net for a rerun that forgot to
        # reset the diagnostic between turns.
        marks.setdefault(stage, stamp)
    return marks


def _wait_idle(bridge: StellaBridge, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        for event in bridge.poll():
            seen.append(event.kind)
            if event.kind == "voice_state" and event.payload == "idle":
                return
        time.sleep(0.05)
    raise AssertionError(
        f"no voice_state=idle within {timeout}s; saw {seen[-20:]}"
    )


def _run_one_turn(bridge: StellaBridge, sink: Path, prompt: str) -> dict[str, float]:
    sink.unlink(missing_ok=True)
    os.environ[turntrace.ENV_VAR] = str(sink)
    turntrace.reset()
    try:
        bridge.post_turn(prompt, spoken=True)
        _wait_idle(bridge)
        marks = _read_stamps(sink)
    finally:
        turntrace.reset()
        os.environ.pop(turntrace.ENV_VAR, None)
    if "turn_start" not in marks:
        return {}
    base = marks["turn_start"]
    return {stage: (stamp - base) / 1000 for stage, stamp in marks.items()}


def _median_table(label: str, rows: list[dict[str, float]]) -> dict[str, float]:
    medians: dict[str, float] = {}
    print(f"\n[{label}] n={len(rows)}")
    for stage in STAGES:
        samples = [row[stage] for row in rows if stage in row]
        if not samples:
            continue
        medians[stage] = statistics.median(samples)
        print(f"  {stage:22s} {medians[stage]:7.3f}s")
    return medians


def test_first_artifact_arrives_earlier_when_the_callback_is_wired(
    tmp_path: Path,
) -> None:
    """Same machine, same Ollama, same prompts, one flipped switch.

    Arm ``stream`` runs the shipped code path. Arm ``whole`` disables
    only the streaming-callback installation — the terminal-tool fast
    path, the chunked consumer, the pipeline marks and the resident
    worker are identical between them — so a difference in
    ``first_artifact`` is the difference streaming actually buys.
    """

    settings = StellaSettings(
        provider="ollama",
        model=os.environ.get("STELLA_BENCH_MODEL", "qwen3:4b"),
        # The plan's read-only target: root's own Ollama on 11434,
        # never restarted or reloaded by this harness.
        ollama_base_url=os.environ.get(
            "STELLA_BENCH_OLLAMA_URL", "http://127.0.0.1:11434"
        ),
        # The bench inherits the owner's real voice settings so it
        # measures the resident worker a launch would use, not a
        # test-only espeak.
        voice_speech="auto",
        speech_resident=(
            os.environ.get("STELLA_SPEECH_RESIDENT", "").strip().casefold()
            in _ON
        ),
        speech_command=os.environ.get("STELLA_SPEECH_COMMAND") or None,
    )

    def make_bridge(*, with_streamer: bool) -> StellaBridge:
        application = StellaApplication(
            StellaSession(_real_stella(settings)),
            settings,
            _panel(settings),
        )
        bridge = StellaBridge(lambda: application)
        if not with_streamer:
            # The switch this bench is measuring: install nothing on
            # the pre-model hook, and every streamed reply falls
            # through to ``_speak_after`` exactly as it did before
            # streaming existed. Everything downstream — chunks,
            # pipeline, marks, the one-at-a-time player — still runs.
            bridge._begin_reply_stream_speech = (  # type: ignore[method-assign]
                lambda: None
            )
        return bridge

    sink = tmp_path / "trace"
    iterations = int(os.environ.get("STELLA_BENCH_N", "3"))

    stream_rows: list[dict[str, float]] = []
    whole_rows: list[dict[str, float]] = []
    for with_streamer, rows in ((True, stream_rows), (False, whole_rows)):
        bridge = make_bridge(with_streamer=with_streamer)
        try:
            for _ in range(iterations):
                for prompt in PROMPTS:
                    row = _run_one_turn(bridge, sink, prompt)
                    if row:
                        rows.append(row)
        finally:
            bridge.stop()
            # Give the worker thread a moment to join before we build
            # the next bridge; StellaBridge.stop() is the documented
            # way to release it.
            threading.Event().wait(0.25)

    stream = _median_table("streaming wired", stream_rows)
    whole = _median_table("callback off (whole-reply path)", whole_rows)
    if "first_artifact" not in stream or "first_artifact" not in whole:
        pytest.skip(
            "the bench never reached first_artifact — check that the "
            "resident speech worker is running and that prompts resolve "
            "to an ANSWER decision"
        )
    delta = whole["first_artifact"] - stream["first_artifact"]
    print(
        f"\nfirst_artifact earlier by {delta:6.3f}s "
        f"(whole {whole['first_artifact']:.3f}s → stream "
        f"{stream['first_artifact']:.3f}s)"
    )
    # The claim under test, honestly stated: a streamed reply is only
    # worth shipping if it produces audio earlier than the whole-reply
    # path did. A negative number here is the honest signal to remove
    # the wiring (AGENTS.md: no demonstrated weakness, no change); the
    # bench does not fail on that value, it reports it — the assertion
    # is what a green run means.
    assert delta > 0, (
        f"streaming did not move first_artifact earlier "
        f"(delta {delta:.3f}s); the plan says to remove the wiring"
    )
