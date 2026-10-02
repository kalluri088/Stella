"""Turn text written to be seen into text that reads aloud cleanly.

A reply is authored for a screen: markdown emphasis, headings, bullets,
links and emoji. A speech synthesizer has no way to render those marks,
so "your tasks are: * **ship it**" comes out of a speaker as noise. This
module is the single place that strips the presentation syntax, so every
:class:`stella.audio_output.SpeechProvider` receives the same words
whichever engine is configured.

It is deliberately conservative about *content*. Nothing is rephrased,
reordered, summarised or dropped — except a bare URL, which a listener
cannot act on and which is replaced by one honest word. Numbers, times
and abbreviations stay exactly as written: a wrong expansion (saying
"six p.m." for a 24-hour 18:00 the user never spoke) is worse than an
awkward one, and Stella has no reliable locale for hours.
"""

import re
import unicodedata

__all__ = ["speakable"]

_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_BARE_URL = re.compile(r"(?<!\w)(?i:https?|ftp)://\S*[^\s.,;:!?)\]]")
_FENCE = re.compile(r"^[ \t]*```[^\n]*$", re.MULTILINE)
_RULE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,}|={3,})[ \t]*$")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+")
_QUOTE = re.compile(r"^[ \t]*>+[ \t]?")
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+")
_NUMBER = re.compile(r"^[ \t]*\d{1,3}[.)][ \t]+")
_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_DIVIDER = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}[ \t:|-]*\|?[ \t]*$")
_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_ITALIC_STAR = re.compile(
    r"(?<![\w*])\*(?![ \t])([^*\n]+?)(?<!\s)\*(?![\w*])"
)
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_INNER_SPACE = re.compile(r"[ \t]+")
_ORPHAN_PUNCT = re.compile(r"\s+([,.;:!?])")
_SILENT_CODEPOINTS = frozenset(
    {
        0x00AD,  # soft hyphen
        0x200D,  # zero-width joiner (emoji sequences)
        0x2060,  # word joiner
        0xFE0E,  # variation selector: text presentation
        0xFE0F,  # variation selector: emoji presentation
    }
)


def _decorative(char: str) -> bool:
    """True for emoji, dingbats and invisible marks that carry no words.

    Unicode category is the test rather than a codepoint table:
    pictographs are ``So`` and zero-width format characters are ``Cf``.
    Combining marks are deliberately *not* stripped (an accented letter
    written as two codepoints is still that letter), and arithmetics,
    currency and letters are left alone, so "3 + 4 = 7" and "$5" survive.
    """

    return (
        ord(char) in _SILENT_CODEPOINTS
        or unicodedata.category(char) in {"So", "Sk", "Cf", "Co"}
    )


def _spoken_line(raw: str) -> tuple[str, bool]:
    """One source line as speech, and whether it was a list item."""

    if _TABLE_ROW.match(raw):
        cells = [cell.strip() for cell in raw.strip().strip("|").split("|")]
        return ", ".join(cell for cell in cells if cell), True
    line = _HEADING.sub("", raw)
    line = _QUOTE.sub("", line)
    listed = bool(_BULLET.match(line) or _NUMBER.match(line))
    line = _BULLET.sub("", line)
    line = _NUMBER.sub("", line)
    line = _BOLD.sub(lambda match: match.group(2), line)
    line = _ITALIC_STAR.sub(lambda match: match.group(1), line)
    line = _STRIKE.sub(lambda match: match.group(1), line)
    line = line.replace("`", "")
    line = "".join(char for char in line if not _decorative(char))
    return _INNER_SPACE.sub(" ", line).strip(), listed


def speakable(text: str) -> str:
    """Return ``text`` with screen-only markup removed, words intact.

    Deterministic, dependency-free and idempotent: normalizing an
    already-normalized reply changes nothing. A reply that is nothing but
    decoration comes back empty, and the caller decides what to say
    instead (see :class:`stella.audio_output.SpeechOutput`).
    """

    if not text:
        return ""
    prepared = _LINK.sub(lambda match: match.group(1), text)
    prepared = _BARE_URL.sub("a link", prepared)
    prepared = _FENCE.sub("", prepared)
    parts: list[str] = []
    previous_listed = False
    break_pending = False
    for raw in prepared.splitlines():
        if not raw.strip() or _RULE.match(raw) or _TABLE_DIVIDER.match(raw):
            break_pending = bool(parts)
            previous_listed = False
            continue
        line, listed = _spoken_line(raw)
        if not line:
            continue
        if (
            parts
            and (listed or previous_listed or break_pending)
            and parts[-1][-1] not in ".!?;:,"
        ):
            parts.append(",")
        parts.append(line)
        previous_listed = listed
        break_pending = False
    return _ORPHAN_PUNCT.sub(r"\1", " ".join(parts)).strip()
