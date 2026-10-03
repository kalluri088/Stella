"""The privacy control on captured screen text.

OCR text lands straight in the LLM context (report 11), so it is blanked
of obvious credentials before anything else sees it. This is a
mitigation, and the tool output says so, rather than a promise no regex
can keep.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}|\d{16,}")
_ASTERISK_RUN_RE = re.compile(r"\*{4,}")
_MASK = "[redacted]"


def mask_secrets(text: str) -> str:
    """Blank obvious credentials in captured screen text.

    Deliberately conservative (long opaque runs and password glyphs
    only): anything shorter stays visible, because a mask that ate real
    text would make the tool useless and tempt someone to switch it off.
    """

    text = _TOKEN_RE.sub(_MASK, text)
    return _ASTERISK_RUN_RE.sub(_MASK, text)


__all__ = ["mask_secrets"]
