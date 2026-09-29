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
    # full-width blockquote band must read against the transcript
    # background (the retired light band sat under ~20 from white and
    # washed out). Stella's reply carries no band and no label at all,
    # so this band and the marker on it are the whole separation.
    theme = stella_ui.THEMES[theme_name]
    assert _distance(theme.user_quote, theme.window) > 20


@pytest.mark.parametrize("theme_name", sorted(stella_ui.THEMES))
def test_quote_marker_reads_on_its_band(theme_name: str) -> None:
    # The ">" marker is painted in user_head on top of user_quote; a
    # marker that blends into its own band erases who speaks. user_head
    # is a dedicated field, never a brand derivation (accent == ok in
    # every palette would have made the marker just another teal).
    theme = stella_ui.THEMES[theme_name]
    assert _distance(theme.user_head, theme.user_quote) > 40
