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
def test_quote_band_is_visible(theme_name: str) -> None:
    # The transcript separates roles as a terminal does: the user's
    # blockquote band must read against the transcript background (the
    # retired light band sat under ~20 from white and washed out).
    # Stella's reply carries no band, so the marker/head colors below
    # do the rest of the separation.
    theme = stella_ui.THEMES[theme_name]
    assert _distance(theme.user_quote, theme.window) > 20


@pytest.mark.parametrize("theme_name", sorted(stella_ui.THEMES))
def test_speaker_labels_use_distinct_colors(theme_name: str) -> None:
    # accent == ok in every palette, so the labels must NOT derive from
    # brand fields: identical teal ">" and "Stella:" erased who spoke.
    # user_head and stella_head are dedicated fields for exactly this.
    theme = stella_ui.THEMES[theme_name]
    assert theme.user_head != theme.stella_head
    assert _distance(theme.user_head, theme.stella_head) > 40
