"""The speech boundary reads markup-free text without changing the words."""

import pytest

from stella.audio_output import SpeechOutput
from stella.spoken_form import speakable


def test_plain_prose_is_left_exactly_alone() -> None:
    text = "Your backup finished. Nothing else changed."

    assert speakable(text) == text


def test_screen_marks_never_reach_the_speaker() -> None:
    text = "## Today\n\nCall **Mira** about the `stella` release."

    spoken = speakable(text)

    assert spoken == "Today, Call Mira about the stella release."
    for mark in ("#", "**", "`"):
        assert mark not in spoken


def test_list_items_are_read_as_one_running_sentence() -> None:
    text = "- ship the release\n- call the bank\n- water the plants\n"

    assert speakable(text) == "ship the release, call the bank, water the plants"


def test_ordered_lists_lose_their_numbers_not_their_order() -> None:
    text = "1. first\n2. second\n"

    assert speakable(text) == "first, second"


def test_a_link_loses_its_address_and_keeps_its_label() -> None:
    text = "See [the release notes](https://example.com/notes) for details."

    assert speakable(text) == "See the release notes for details."


def test_a_bare_address_becomes_one_honest_word() -> None:
    text = "More at https://example.com/notes."

    assert speakable(text) == "More at a link."
    assert speakable("More at HTTPS://example.com/n, ok") == "More at a link, ok"


def test_emoji_are_silent_and_words_are_not() -> None:
    assert speakable("\u2705 Shipped \U0001F680 today") == "Shipped today"


def test_math_money_and_identifiers_survive() -> None:
    text = "3 + 4 = 7, $5 each, snake_case_name, 2 * 3 * 4."

    assert speakable(text) == text


def test_time_is_never_rewritten_into_a_guess() -> None:
    assert speakable("Call at 18:00.") == "Call at 18:00."


def test_tables_are_read_row_by_row() -> None:
    text = "| task | time |\n| --- | --- |\n| ship | 18:00 |\n"

    assert speakable(text) == "task, time, ship, 18:00"


def test_code_fences_silence_the_marker_not_the_line() -> None:
    text = "```python\nprint('hi')\n```\n"

    assert speakable(text) == "print('hi')"


def test_strikethrough_and_italics_lose_their_marks() -> None:
    text = "~~gone~~ and *italic* words."

    assert speakable(text) == "gone and italic words."


def test_rules_and_blockquotes_leave_no_noise() -> None:
    text = "> quoted\n\n---\n\nafter"

    assert speakable(text) == "quoted, after"


def test_normalization_is_idempotent() -> None:
    text = "## Plan\n- **ship** it\n- see [notes](https://example.com/n)\n"

    once = speakable(text)

    assert speakable(once) == once


def test_a_reply_that_is_only_decoration_becomes_empty() -> None:
    assert speakable("---\n\n**\u2705**\n") == ""
    assert speakable("") == ""


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("**Hello** aloud.", "Hello aloud."),
        ("Plain.", "Plain."),
    ],
)
def test_every_speech_output_arrives_prepared(raw: str, expected: str) -> None:
    assert SpeechOutput(raw).text == expected


def test_speech_output_never_speaks_an_empty_utterance() -> None:
    output = SpeechOutput("\u2705\u2705")

    assert output.text == "\u2705\u2705"
