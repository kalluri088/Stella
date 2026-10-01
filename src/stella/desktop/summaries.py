"""Plain-language approval lines for the desktop capabilities.

The wording lives with the tools, not in the dispatcher: an approval has
to say in one sentence what is about to happen to the user's screen, and
"the exact text is shown above" is only honest because the preview shows
it. The window id is echoed here, never a title, so a user reading one
line can match it against the preview detail below.
"""

from __future__ import annotations

import json

from stella.desktop.capabilities import (
    MAX_KEY_TEXT_CHARS,
    window_id_usable,
)


def desktop_tool_summaries(
    capability: str, arguments: dict[str, object]
) -> str | None:
    """The summary for one desktop request, or None if it isn't one."""

    if capability == "screen_read":
        scope = arguments.get("scope")
        if scope == "active_window":
            return (
                "take a local screenshot of the focused window and read "
                "its text with local OCR (nothing leaves this machine)"
            )
        if scope == "full_screen":
            return (
                "take a local screenshot of the whole screen and read "
                "its text with local OCR (nothing leaves this machine)"
            )
    elif capability == "window_focus":
        window_id = arguments.get("id")
        if window_id_usable(window_id):
            return (
                "focus the desktop window with id "
                f"{json.dumps(str(window_id))}"
            )
    elif capability == "key_send":
        window_id = arguments.get("id")
        text = arguments.get("text")
        if (
            window_id_usable(window_id)
            and isinstance(text, str)
            and text
            and len(text) <= MAX_KEY_TEXT_CHARS
        ):
            return (
                f"type {len(text)} characters into the window with "
                f"id {json.dumps(str(window_id))} after focusing it "
                "(the exact text is shown above; keystrokes can never "
                "be undone)"
            )
    return None


__all__ = ["desktop_tool_summaries"]
