"""The locked two-tier event bus: lexical rules first, Laya only for fuzz.

Research reports 02 and 12 measured where each tier wins: regex/field rules
are free, deterministic, reviewable by a human auditor, and perfect on
intentions with a lexical anchor (sender address, ``Error:``, ``in N
minutes``); a typed Laya question adds ~40 ms and only earns its cost on
genuinely fuzzy semantics — and it cannot learn state facts no matter how
the criteria are worded (report 12: 4/8 zero-shot, 4/8 with hand-tuned
criteria, 8/8 regex). Tier 2 (the language-model Brain) acts on events
later; it is deliberately not a router and does not appear here.

The trust model matches the rest of Stella: this module produces
:class:`Match` records — reasons a human can read — and never takes an
action. Feeding matches into notifications or the bridge is
each consumer's own approved decision path.

Because the checkpoint ships uncalibrated confidences (report 02/12,
warned at every load), a ``noul`` probability only fires an intention
when that intention explicitly registered a threshold; otherwise it is
reported as advisory data inside the dispatch result. Choice-type
questions fire on the label, which is a deterministic reading of the
model's answer, not a threshold on a float.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

# Report 02: live intentions are capped around ten; quality, not just
# cost, degrades past the curated preset shapes, and the cap keeps the
# per-event Tier-1 batch bounded by design.
MAX_INTENTIONS = 10

QUESTION_TYPES = ("choice", "score", "noul")


@dataclass(frozen=True)
class Event:
    """One thing that happened, as the bus sees it.

    ``fields`` carries whatever structured facts the source could
    extract (sender, battery percentage, pane name). Sources should
    preprocess state before publishing: report 02's clearest finding was
    that a tmux watcher must hand the bus "job DONE" as a fact, not raw
    pane text for a model to interpret.
    """

    source: str
    text: str
    fields: tuple[tuple[str, str], ...] = ()

    def field(self, name: str) -> str | None:
        for key, value in self.fields:
            if key == name:
                return value
        return None

    def as_text(self) -> str:
        """The single string a Tier-1 judge reads for this event."""

        parts = [self.source, self.text]
        parts += [f"{key}={value}" for key, value in self.fields]
        return " ".join(part for part in parts if part)


# ------------------------------------------------------------ tier zero


class Tier0Rule(ABC):
    """A deterministic, readable match rule over one event."""

    @abstractmethod
    def matches(self, event: Event) -> bool:
        """Pure function of the event; never raises for odd input."""

    @abstractmethod
    def explain(self) -> str:
        """The audit line an interrupt will carry verbatim."""


def _compiled(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as error:  # registration-time, not dispatch-time
        raise ValueError(f"invalid tier-0 pattern {pattern!r}: {error}")


@dataclass(frozen=True)
class TextMatches(Tier0Rule):
    """The event text contains ``pattern`` (``re.search``)."""

    pattern: str

    def __post_init__(self) -> None:
        _compiled(self.pattern)

    def matches(self, event: Event) -> bool:
        return _compiled(self.pattern).search(event.text) is not None

    def explain(self) -> str:
        return f"text matches /{self.pattern}/"


@dataclass(frozen=True)
class FieldIs(Tier0Rule):
    """The named field equals ``value`` exactly."""

    name: str
    value: str

    def matches(self, event: Event) -> bool:
        return event.field(self.name) == self.value

    def explain(self) -> str:
        return f"{self.name} == {self.value!r}"


@dataclass(frozen=True)
class FieldMatches(Tier0Rule):
    """The named field exists and contains ``pattern``."""

    name: str
    pattern: str

    def __post_init__(self) -> None:
        _compiled(self.pattern)

    def matches(self, event: Event) -> bool:
        value = event.field(self.name)
        return value is not None and _compiled(self.pattern).search(value) is not None

    def explain(self) -> str:
        return f"{self.name} matches /{self.pattern}/"


# ------------------------------------------------------------ intentions


def validate_question(question: Mapping[str, object]) -> None:
    """Enforce the shipped preset shape a question must keep.

    Report 12: laya's curated ``laya.presets`` question sets encode what
    the model was actually trained to answer; hand-rolled criteria drift
    off that distribution. Stella therefore only accepts questions in
    exactly the preset shape — ``type`` from the three shipped kinds,
    real ``instructions``, and criteria of the kind-appropriate type.
    """

    if not isinstance(question, Mapping):
        raise TypeError("tier-1 question must be a mapping")
    kind = question.get("type")
    if not isinstance(kind, str) or kind not in QUESTION_TYPES:
        raise ValueError(
            "tier-1 question type must be one of " + ", ".join(QUESTION_TYPES)
        )
    instructions = question.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("tier-1 question needs non-empty instructions")
    criteria = question.get("criteria")
    if kind == "score":
        if not isinstance(criteria, list) or not all(
            isinstance(level, str) for level in criteria
        ):
            raise ValueError("score criteria must be a list of strings")
    elif criteria is not None and not isinstance(criteria, Mapping):
        raise ValueError("choice/noul criteria must be a mapping")
    elif kind == "choice" and (
        not isinstance(criteria, Mapping) or not criteria
    ):
        raise ValueError("choice questions need a non-empty criteria mapping")


@dataclass(frozen=True)
class Intention:
    """One standing wish of the user, in up to two tiers.

    ``rules`` are the Tier-0 anchors: any one matching is enough, and the
    match is deterministic. ``question`` is an optional single typed
    laya question (preset shape) for the fuzzy residue. ``positive``
    lists the choice-criteria keys that mean "this is it"; ``threshold``
    is the explicit, per-deployment calibration decision to let an
    uncalibrated ``noul`` probability fire — leave it ``None`` and the
    probability is reported but never acts.
    """

    name: str
    rules: tuple[Tier0Rule, ...] = ()
    question: Mapping[str, object] | None = None
    positive: tuple[str, ...] = ("yes",)
    threshold: float | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("intention needs a name")
        if not self.rules and self.question is None:
            raise ValueError(
                f"intention {self.name!r} has neither tier-0 rules nor a "
                "tier-1 question"
            )
        if self.question is not None:
            validate_question(self.question)
        if self.threshold is not None and not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be a probability in [0, 1]")


@dataclass(frozen=True)
class Match:
    """Why the bus believes an intention fired."""

    intention: str
    tier: int
    reason: str
    probability: float | None = None


TIER_ONE_RAN = "ran"
TIER_ONE_SKIPPED = "skipped"
TIER_ONE_ABSENT = "absent"
TIER_ONE_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class DispatchResult:
    """Everything one event produced: matches plus honest tier status.

    ``answers`` carries every raw Tier-1 reply (even for intentions that
    did not fire) so consumers and future calibration can see what the
    judge actually said. ``tier1_status`` says plainly whether tier 1
    ran, had nothing to ask, has no judge, or the judge was there and
    failed — "no match" and "could not ask" must never read the same.
    """

    matches: tuple[Match, ...]
    tier1_status: str
    answers: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    @property
    def fired(self) -> tuple[str, ...]:
        return tuple(match.intention for match in self.matches)


class TierOneUnavailable(RuntimeError):
    """The judge exists but could not answer this event."""


class TierOneJudge(Protocol):
    """A batched semantic judge over preset-shape questions.

    Implementations answer every question against ``event`` in one shot
    (the measured cost model only works batched) and return laya's raw
    per-question answer mappings unchanged.
    """

    def ask(
        self,
        event: Event,
        questions: Mapping[str, Mapping[str, object]],
    ) -> Mapping[str, Mapping[str, object]]:
        """Answer ``questions`` about ``event``; raise if it cannot."""


class EventBus:
    """Register intentions; dispatch events through both tiers."""

    def __init__(self, judge: TierOneJudge | None = None) -> None:
        self._judge = judge
        self._intentions: dict[str, Intention] = {}

    @property
    def intentions(self) -> tuple[Intention, ...]:
        return tuple(self._intentions.values())

    @property
    def has_judge(self) -> bool:
        return self._judge is not None

    def register(self, intention: Intention) -> None:
        if intention.name in self._intentions:
            raise ValueError(
                f"intention {intention.name!r} is already registered"
            )
        if len(self._intentions) >= MAX_INTENTIONS:
            raise ValueError(
                f"the bus holds at most {MAX_INTENTIONS} live intentions "
                "(report 02: quality past the curated set is unmeasured)"
            )
        self._intentions[intention.name] = intention

    def unregister(self, name: str) -> None:
        self._intentions.pop(name, None)

    def dispatch(self, event: Event) -> DispatchResult:
        matches: list[Match] = []
        asked: dict[str, Intention] = {}
        for intention in self._intentions.values():
            for rule in intention.rules:
                if rule.matches(event):
                    matches.append(
                        Match(
                            intention=intention.name,
                            tier=0,
                            reason=rule.explain(),
                        )
                    )
                    break
            else:
                if intention.question is not None:
                    asked[intention.name] = intention
        answers: Mapping[str, Mapping[str, object]] = {}
        if not asked:
            status = TIER_ONE_SKIPPED if self._judge is not None else TIER_ONE_ABSENT
        elif self._judge is None:
            status = TIER_ONE_ABSENT
        else:
            try:
                answers = self._judge.ask(
                    event,
                    {
                        name: intention.question
                        for name, intention in asked.items()
                    },
                )
            except TierOneUnavailable:
                answers = {}
                status = TIER_ONE_UNAVAILABLE
            else:
                status = TIER_ONE_RAN
                for name, intention in asked.items():
                    fired = self._tier_one_match(intention, answers.get(name))
                    if fired is not None:
                        matches.append(fired)
        return DispatchResult(
            matches=tuple(matches), tier1_status=status, answers=answers
        )

    @staticmethod
    def _tier_one_match(
        intention: Intention, answer: Mapping[str, object] | None
    ) -> Match | None:
        if not answer:
            return None
        question_type = intention.question.get("type") if intention.question else None
        if question_type == "noul":
            probability = _probability(answer.get("noul"))
            if probability is None:
                return None
            if intention.threshold is None:
                # Uncalibrated confidence is advisory data, never an act.
                return None
            if probability >= intention.threshold:
                return Match(
                    intention=intention.name,
                    tier=1,
                    reason=(
                        f"laya P(true)={probability:.2f} ≥ "
                        f"threshold {intention.threshold:.2f}"
                    ),
                    probability=probability,
                )
            return None
        label = answer.get("choice")
        if isinstance(label, str) and label in intention.positive:
            return Match(
                intention=intention.name,
                tier=1,
                reason=f"laya chose {label!r}",
                probability=_probability(answer.get("confidence")),
            )
        return None


def _probability(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def intention(
    name: str,
    *,
    rules: Iterable[Tier0Rule] = (),
    question: Mapping[str, object] | None = None,
    positive: Iterable[str] = ("yes",),
    threshold: float | None = None,
) -> Intention:
    """Tolerant constructor (iterables instead of tuples)."""

    return Intention(
        name=name,
        rules=tuple(rules),
        question=question,
        positive=tuple(positive),
        threshold=threshold,
    )
