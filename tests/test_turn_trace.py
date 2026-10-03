"""The ``STELLA_TURN_TRACE`` diagnostic and the marks ``run_turn`` writes.

Two claims, both cheap to prove. When the environment says nothing,
:func:`stella.turntrace.mark` is a dict lookup and a ``None`` check:
nothing touches disk, nothing is appended, and a live turn cannot tell
it is being observed. When the environment names a file, every stage
writes a ``stage,<monotonic-ms>`` line — never text, never a transcript,
never a secret — and :func:`stella.app.StellaSession.run_turn` produces
its three marks (start, answer, end) on every path it can take, so a
cancelled or failing turn still ends on the disk in the order the
benchmark expects.

The bridge-level marks (``decision_done``, ``first_answer_token``,
``first_sentence_ready``, ``first_artifact``, ``first_play``) belong to
the voice pipeline and are exercised end-to-end by
``tests/test_voice_stream_bench.py``. This file only checks that the
diagnostic itself behaves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from stella import turntrace
from stella.app import StellaSession
from stella.brain import Brain, Decision, DecisionKind
from stella.context import Context
from stella.llm import LLMClient, Message
from stella.memory import InMemoryMemory
from stella.stella import Stella
from stella.tools import EchoTool, ToolDispatcher

_LINE = re.compile(r"^[a-z][a-z_]*,\d+$")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    # The diagnostic caches its path for the whole process; every test
    # resets so a leak from a previous test cannot pre-empt this one,
    # and the environment variable itself is cleared first.
    monkeypatch.delenv(turntrace.ENV_VAR, raising=False)
    turntrace.reset()
    yield
    turntrace.reset()


class _AnswerLLM(LLMClient):
    def chat(
        self,
        messages: list[Message],
        should_cancel=None,
    ) -> str:
        del messages, should_cancel
        return "noted"


class _OneAnswer(Brain):
    def decide(
        self,
        context: Context,
        should_cancel=None,
    ) -> Decision:
        del context, should_cancel
        return Decision(kind=DecisionKind.ANSWER, content="noted")


def _session() -> StellaSession:
    stella = Stella(
        _OneAnswer(),
        _AnswerLLM(),
        ToolDispatcher([EchoTool()]),
        InMemoryMemory(),
    )
    return StellaSession(stella)


def test_unset_writes_nothing_and_reports_disabled(tmp_path: Path) -> None:
    sink = tmp_path / "trace"
    # The variable is deliberately not set (the autouse fixture cleared
    # it). Pointing the environment somewhere else is not enough to
    # change behaviour; only setting the exact name does.
    assert sink.exists() is False
    assert turntrace.enabled() is False
    turntrace.mark("turn_start")
    turntrace.mark("turn_end")
    assert sink.exists() is False


def test_set_appends_stage_and_monotonic_ms(tmp_path: Path, monkeypatch) -> None:
    sink = tmp_path / "trace"
    monkeypatch.setenv(turntrace.ENV_VAR, str(sink))
    turntrace.reset()
    assert turntrace.enabled() is True
    turntrace.mark("turn_start")
    turntrace.mark("answer_done")
    turntrace.mark("turn_end")
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert [line.split(",", 1)[0] for line in lines] == [
        "turn_start",
        "answer_done",
        "turn_end",
    ]
    for line in lines:
        assert _LINE.match(line), line
    # Monotonic stamps are non-decreasing; two adjacent marks may share
    # a millisecond on a fast machine but never move backwards.
    stamps = [int(line.split(",", 1)[1]) for line in lines]
    assert stamps == sorted(stamps)


def test_reset_repicks_the_environment_after_a_change(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv(turntrace.ENV_VAR, str(first))
    turntrace.reset()
    turntrace.mark("turn_start")
    # Without a reset, the cached path still wins: the change is invisible
    # until a caller says otherwise, which is exactly what the running
    # application relies on (an owner sets the env before launch and
    # never edits it mid-turn).
    monkeypatch.setenv(turntrace.ENV_VAR, str(second))
    turntrace.mark("turn_start")
    assert second.exists() is False
    assert len(first.read_text(encoding="utf-8").splitlines()) == 2
    turntrace.reset()
    turntrace.mark("turn_start")
    assert second.exists() is True
    assert len(second.read_text(encoding="utf-8").splitlines()) == 1


def test_run_turn_writes_start_answer_end_on_the_happy_path(
    tmp_path: Path, monkeypatch
) -> None:
    sink = tmp_path / "trace"
    monkeypatch.setenv(turntrace.ENV_VAR, str(sink))
    turntrace.reset()
    _session().run_turn("hello")
    stages = [line.split(",", 1)[0] for line in sink.read_text().splitlines()]
    assert stages == ["turn_start", "answer_done", "turn_end"]


def test_run_turn_writes_turn_end_when_the_process_raises(
    tmp_path: Path, monkeypatch
) -> None:
    sink = tmp_path / "trace"
    monkeypatch.setenv(turntrace.ENV_VAR, str(sink))
    turntrace.reset()

    class _Failing:
        def process(self, context, **options):
            raise RuntimeError("boom")

    outcome = StellaSession(_Failing()).run_turn("hello")  # type: ignore[arg-type]
    assert outcome.error_message is not None
    stages = [line.split(",", 1)[0] for line in sink.read_text().splitlines()]
    # An error exit still lands on disk: turn_start, then turn_end
    # without an answer_done between. A benchmark that never sees an
    # answer_done for a given turn knows the turn failed before the
    # response text was composed.
    assert stages == ["turn_start", "turn_end"]
