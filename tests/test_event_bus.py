"""Tests for the locked two-tier event bus (reports 02 and 12).

The bus records matches and never acts, so every test asserts on the
:class:`DispatchResult`: which tier fired, what reason an auditor would
read, and — just as importantly — the honest status of a tier that did
not or could not run.
"""

import pytest

from stella.event_bus import (
    MAX_INTENTIONS,
    TIER_ONE_ABSENT,
    TIER_ONE_RAN,
    TIER_ONE_SKIPPED,
    TIER_ONE_UNAVAILABLE,
    Event,
    EventBus,
    FieldIs,
    FieldMatches,
    Intention,
    Match,
    TextMatches,
    TierOneUnavailable,
    intention,
    validate_question,
)


def make_event(
    source: str = "email",
    text: str = "hello there",
    **fields: str,
) -> Event:
    return Event(
        source=source, text=text, fields=tuple(sorted(fields.items()))
    )


class RecordingJudge:
    """Answers every question with a canned per-name reply mapping."""

    def __init__(self, answers=None, *, raises=False, exits=()):
        self.answers = answers or {}
        self.raises = raises
        self.exits = set(exits)
        self.requests: list[tuple[Event, dict]] = []

    def ask(self, event, questions):
        self.requests.append((event, dict(questions)))
        if self.raises:
            raise TierOneUnavailable("the judge is down")
        return {
            name: dict(self.answers[name])
            for name in questions
            if name in self.answers and name not in self.exits
        }


def choice_question(instructions: str = "Route this event.") -> dict:
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": {"yes": "it matters", "no": "it does not"},
    }


def noul_question(instructions: str = "Is this urgent?") -> dict:
    return {"type": "noul", "instructions": instructions}


# ------------------------------------------------------------------ events


def test_event_field_lookup_and_rendered_text():
    event = make_event(text="battery low", at="10%", device="laptop")
    assert event.field("at") == "10%"
    assert event.field("missing") is None
    assert event.as_text() == "email battery low at=10% device=laptop"


def test_as_text_omits_empty_pieces():
    assert Event(source="", text="plain").as_text() == "plain"


# -------------------------------------------------------------- tier zero


def test_text_matches_searches_the_event_text():
    rule = TextMatches(r"Error:\s+\d+")
    assert rule.matches(make_event(text="Error: 42 happened"))
    assert not rule.matches(make_event(text="no errors today"))
    assert rule.explain() == "text matches /Error:\\s+\\d+/"


def test_field_rules_read_named_facts():
    assert FieldIs("sender", "mom@example.com").matches(
        make_event(sender="mom@example.com")
    )
    assert not FieldIs("sender", "mom@example.com").matches(
        make_event(sender="boss@example.com")
    )
    assert FieldMatches("subject", r"Invoice").matches(
        make_event(subject="Invoice #12")
    )
    assert not FieldMatches("subject", r"invoice").matches(make_event())


@pytest.mark.parametrize("pattern", ["(unclosed", "[a-", "*oops"])
def test_invalid_patterns_are_rejected_at_registration(pattern):
    with pytest.raises(ValueError, match="invalid tier-0 pattern"):
        TextMatches(pattern)
    with pytest.raises(ValueError, match="invalid tier-0 pattern"):
        FieldMatches("field", pattern)


# ------------------------------------------------------ question shapes


def test_preset_shape_questions_are_accepted():
    validate_question(choice_question())
    validate_question(noul_question())
    validate_question(
        {
            "type": "score",
            "instructions": "How severe?",
            "criteria": ["none", "minor", "critical"],
        }
    )


@pytest.mark.parametrize(
    "question, message",
    [
        ({"instructions": "x", "criteria": {}}, "type must be one of"),
        ({"type": "choice", "instructions": "x"}, "non-empty criteria mapping"),
        ({"type": "vibe", "instructions": "x", "criteria": {}}, "type must be one of"),
        ({"type": "choice", "criteria": {}}, "non-empty instructions"),
        ({"type": "choice", "instructions": "  ", "criteria": {}}, "non-empty instructions"),
        ({"type": "choice", "instructions": "x", "criteria": []}, "criteria must be a mapping"),
        ({"type": "score", "instructions": "x", "criteria": "high"}, "list of strings"),
        ({"type": "noul", "instructions": "x", "criteria": 3}, "criteria must be a mapping"),
    ],
)
def test_off_preset_questions_are_rejected(question, message):
    with pytest.raises(ValueError, match=message):
        validate_question(question)


def test_a_non_mapping_question_is_a_type_error():
    with pytest.raises(TypeError):
        validate_question(["not", "a", "mapping"])


# -------------------------------------------------------------- intentions


def test_an_intention_needs_at_least_one_tier():
    with pytest.raises(ValueError, match="neither tier-0 rules nor a tier-1"):
        Intention(name="empty")


def test_intention_name_and_threshold_are_validated():
    with pytest.raises(ValueError, match="needs a name"):
        Intention(name="  ", rules=(FieldIs("a", "b"),))
    with pytest.raises(ValueError, match="probability in"):
        Intention(
            name="x", rules=(FieldIs("a", "b"),), threshold=1.5
        )
    with pytest.raises(ValueError, match="probability in"):
        Intention(
            name="x", rules=(FieldIs("a", "b"),), threshold=-0.1
        )


def test_intention_validates_its_question_at_creation():
    with pytest.raises(ValueError, match="type must be one of"):
        Intention(name="bad", question={"type": "vibe", "instructions": "x"})


# ----------------------------------------------------------- registration


def test_registration_rejects_duplicates_and_caps_the_live_set():
    bus = EventBus()
    first = intention("one", rules=[FieldIs("a", "b")])
    bus.register(first)
    with pytest.raises(ValueError, match="already registered"):
        bus.register(first)
    for index in range(MAX_INTENTIONS - 1):
        bus.register(
            intention(f"extra-{index}", rules=[FieldIs("a", "b")])
        )
    assert len(bus.intentions) == MAX_INTENTIONS
    with pytest.raises(ValueError, match="at most 10"):
        bus.register(intention("overflow", rules=[FieldIs("a", "b")]))


def test_unregister_removes_only_the_named_intention():
    bus = EventBus()
    bus.register(intention("keep", rules=[FieldIs("a", "b")]))
    bus.register(intention("drop", rules=[FieldIs("a", "b")]))
    bus.unregister("drop")
    bus.unregister("never-existed")
    assert [item.name for item in bus.intentions] == ["keep"]


# ----------------------------------------------------------- tier zero


def test_a_rule_match_is_recorded_with_its_audit_reason():
    bus = EventBus()
    bus.register(
        intention(
            "error-watch",
            rules=[TextMatches(r"Error:"), FieldIs("source", "build")],
        )
    )
    result = bus.dispatch(make_event(source="ci", text="Error: boom"))
    assert result.matches == (
        Match(
            intention="error-watch",
            tier=0,
            reason="text matches /Error:/",
        ),
    )
    assert result.fired == ("error-watch",)


def test_first_matching_rule_wins_and_later_rules_are_not_explained():
    bus = EventBus()
    bus.register(
        intention(
            "two-rules",
            rules=[FieldIs("a", "1"), FieldIs("b", "2")],
        )
    )
    result = bus.dispatch(make_event(a="1", b="2"))
    assert [match.reason for match in result.matches] == ["a == '1'"]


# ------------------------------------------------- tier-0 / tier-1 gating


def test_a_rule_hit_does_not_ask_the_judge_about_that_intention():
    judge = RecordingJudge(answers={"mail": {"choice": "yes"}})
    bus = EventBus(judge)
    bus.register(
        intention(
            "mail",
            rules=[FieldIs("sender", "mom@example.com")],
            question=choice_question(),
        )
    )
    result = bus.dispatch(make_event(sender="mom@example.com"))
    assert result.fired == ("mail",)
    assert result.tier1_status == TIER_ONE_SKIPPED
    assert judge.requests == []
    assert result.matches[0].tier == 0


def test_the_judge_is_only_asked_for_unmatched_questions_in_one_batch():
    judge = RecordingJudge(
        answers={"alert": {"choice": "yes"}, "digest": {"choice": "no"}}
    )
    bus = EventBus(judge)
    bus.register(
        intention(
            "anchor",
            rules=[FieldIs("channel", "calendar")],
            question=choice_question(),
        )
    )
    bus.register(
        intention("alert", question=choice_question("Is it an alert?"))
    )
    bus.register(
        intention("digest", question=choice_question("Is it a digest?"))
    )
    result = bus.dispatch(make_event(source="mail", text="ping", channel="calendar"))
    assert [request.source for request, _ in judge.requests] == ["mail"]
    asked = judge.requests[0][1]
    # The anchor matched its rule on the calendar channel, so only the
    # two unmatched intentions' questions go into the one batched
    # request — and the anchor still fires from tier 0.
    assert set(asked) == {"alert", "digest"}
    assert result.tier1_status == TIER_ONE_RAN
    assert result.fired == ("anchor", "alert")


# ---------------------------------------------------------- tier statuses


def test_no_judge_means_absent_not_zero_match():
    bus = EventBus()
    bus.register(intention("fuzzy", question=choice_question()))
    result = bus.dispatch(make_event(text="anything"))
    assert result.tier1_status == TIER_ONE_ABSENT
    assert result.matches == ()


def test_rules_only_bus_reports_absent_even_when_everything_matched():
    bus = EventBus()
    bus.register(intention("anchor", rules=[FieldIs("a", "b")]))
    result = bus.dispatch(make_event(a="b"))
    assert result.tier1_status == TIER_ONE_ABSENT
    assert result.fired == ("anchor",)


def test_a_judge_with_nothing_to_ask_is_skipped_not_absent():
    judge = RecordingJudge()
    bus = EventBus(judge)
    bus.register(
        intention(
            "anchor",
            rules=[FieldIs("a", "b")],
            question=choice_question(),
        )
    )
    result = bus.dispatch(make_event(a="b"))
    assert result.tier1_status == TIER_ONE_SKIPPED
    assert judge.requests == []


def test_an_unavailable_judge_never_reads_as_no_match():
    judge = RecordingJudge(raises=True)
    bus = EventBus(judge)
    bus.register(intention("fuzzy", question=choice_question()))
    result = bus.dispatch(make_event(text="anything"))
    assert result.tier1_status == TIER_ONE_UNAVAILABLE
    assert result.matches == ()
    assert result.answers == {}


# ------------------------------------------------------- tier-1 readings


def test_choice_questions_fire_on_the_positive_label():
    judge = RecordingJudge(
        answers={"urgent": {"choice": "yes", "confidence": 0.9}}
    )
    bus = EventBus(judge)
    bus.register(
        intention(
            "urgent",
            question=choice_question(),
            positive=("yes", "probably"),
        )
    )
    result = bus.dispatch(make_event(text="server on fire"))
    match = result.matches[0]
    assert match.tier == 1
    assert match.reason == "laya chose 'yes'"
    assert match.probability == 0.9
    assert result.answers == {
        "urgent": {"choice": "yes", "confidence": 0.9}
    }


def test_a_negative_choice_label_records_nothing_but_keeps_the_answer():
    judge = RecordingJudge(answers={"urgent": {"choice": "no"}})
    bus = EventBus(judge)
    bus.register(intention("urgent", question=choice_question()))
    result = bus.dispatch(make_event(text="a joke"))
    assert result.matches == ()
    assert result.tier1_status == TIER_ONE_RAN
    assert result.answers == {"urgent": {"choice": "no"}}


def test_an_answer_for_an_unregistered_name_is_ignored():
    judge = RecordingJudge(
        answers={"urgent": {"choice": "yes"}, "ghost": {"choice": "yes"}}
    )
    bus = EventBus(judge)
    bus.register(intention("urgent", question=choice_question()))
    result = bus.dispatch(make_event(text="x"))
    assert result.fired == ("urgent",)


def test_missing_answers_are_absent_not_denied():
    judge = RecordingJudge(
        answers={"urgent": {"choice": "yes"}}, exits=("other",)
    )
    bus = EventBus(judge)
    bus.register(intention("urgent", question=choice_question()))
    bus.register(intention("other", question=choice_question()))
    result = bus.dispatch(make_event(text="x"))
    assert result.fired == ("urgent",)
    assert "other" not in result.answers


def test_uncalibrated_noul_probabilities_are_advisory_only():
    judge = RecordingJudge(answers={"maybe": {"noul": 0.99}})
    bus = EventBus(judge)
    bus.register(intention("maybe", question=noul_question()))
    result = bus.dispatch(make_event(text="phone ringing"))
    assert result.matches == ()
    assert result.answers == {"maybe": {"noul": 0.99}}


def test_noul_fires_only_at_or_above_an_explicit_threshold():
    judge = RecordingJudge(answers={"pager": {"noul": 0.72}})
    bus = EventBus(judge)
    bus.register(
        intention("pager", question=noul_question(), threshold=0.9)
    )
    result = bus.dispatch(make_event(text="alert"))
    assert result.matches == ()
    judge2 = RecordingJudge(answers={"pager": {"noul": 0.9}})
    bus2 = EventBus(judge2)
    bus2.register(
        intention("pager", question=noul_question(), threshold=0.9)
    )
    fired = bus2.dispatch(make_event(text="alert"))
    match = fired.matches[0]
    assert match.tier == 1
    assert match.probability == 0.9
    assert match.reason == "laya P(true)=0.90 ≥ threshold 0.90"


@pytest.mark.parametrize("value", ["0.9", None, True, {}])
def test_a_malformed_noul_probability_never_fires(value):
    judge = RecordingJudge(answers={"pager": {"noul": value}})
    bus = EventBus(judge)
    bus.register(
        intention("pager", question=noul_question(), threshold=0.1)
    )
    result = bus.dispatch(make_event(text="alert"))
    assert result.matches == ()


def test_noul_probability_zero_is_real_and_can_fire():
    judge = RecordingJudge(answers={"pager": {"noul": 0.0}})
    bus = EventBus(judge)
    bus.register(
        intention("pager", question=noul_question(), threshold=0.0)
    )
    result = bus.dispatch(make_event(text="anything"))
    assert result.matches[0].probability == 0.0


def test_choice_confidence_that_is_not_a_number_stays_none():
    judge = RecordingJudge(
        answers={"urgent": {"choice": "yes", "confidence": "high"}}
    )
    bus = EventBus(judge)
    bus.register(intention("urgent", question=choice_question()))
    result = bus.dispatch(make_event(text="x"))
    assert result.matches[0].probability is None


# --------------------------------------------------------- tolerant ctor


def test_intention_helper_accepts_iterables():
    item = intention(
        "mixed",
        rules=[FieldIs("a", "b")],
        question=choice_question(),
        positive=["yes", "sure"],
    )
    assert isinstance(item.rules, tuple)
    assert item.positive == ("yes", "sure")
    bus = EventBus()
    bus.register(item)
    assert bus.intentions == (item,)
