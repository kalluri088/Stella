"""Role-separation palette guarantees.

These assert the presentation invariants behind the user/model
separation in the transcript. They read the static theme palettes only,
so unlike the rest of the UI suite they run headless: a theme edit can
never quietly erase the separation.
"""

import pytest

from stella import ui as stella_ui


def _rgb(spec: str) -> tuple[int, int, int]:
    return (int(spec[1:3], 16), int(spec[3:5], 16), int(spec[5:7], 16))


def _distance(a: str, b: str) -> float:
    return sum((x - y) ** 2 for x, y in zip(_rgb(a), _rgb(b), strict=True)) ** 0.5


@pytest.mark.parametrize("theme_name", sorted(stella_ui.THEMES))
def test_role_bands_are_visible_and_apart(theme_name: str) -> None:
    # Bands must read against the transcript background AND against each
    # other. The retired light palette sat ~11 apart from band to band
    # and under ~20 against white: the two roles washed into one.
    theme = stella_ui.THEMES[theme_name]
    assert _distance(theme.user_bubble, theme.stella_bubble) > 25
    assert _distance(theme.user_bubble, theme.window) > 20
    assert _distance(theme.stella_bubble, theme.window) > 20


@pytest.mark.parametrize("theme_name", sorted(stella_ui.THEMES))
def test_speaker_labels_use_distinct_colors(theme_name: str) -> None:
    # accent == ok in every palette, so the labels must NOT derive from
    # brand fields: identical teal "You:"/"Stella:" erased who spoke.
    # user_head and stella_head are dedicated fields for exactly this.
    theme = stella_ui.THEMES[theme_name]
    assert theme.user_head != theme.stella_head
    assert _distance(theme.user_head, theme.stella_head) > 40
