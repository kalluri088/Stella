"""Provider-neutral one-shot speech-output boundary."""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from stella.spoken_form import speakable

MAX_SPEECH_TEXT_CHARS = 4000
MIN_SPEECH_CHUNK_CHARS = 12
#: The longest stretch of reply one synthesis request may be asked for.
#: Measured on the installed Kokoro worker (2026-10-10), rendering costs
#: about 0.03 s per character on an idle machine, so a 2300-character
#: "sentence" needs ~70 s — past the resident provider's 60 s budget, which
#: then kills a healthy worker mid-calculation and silences the whole reply.
#: The bound is what makes that impossible to ask in the first place.
MAX_SPEECH_CHUNK_CHARS = 600


@dataclass(frozen=True)
class SpeechOutput:
    """Bounded final response text prepared for speech rendering.

    Construction is the one place visible markup becomes spoken text: a
    provider receives what :func:`stella.spoken_form.speakable` returns,
    never the raw reply, so no engine is ever asked to read an asterisk
    aloud. The words are the reply's own. A reply made entirely of
    decoration keeps its original form rather than becoming an empty
    utterance, because silence would look like a broken provider.
    """

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("speech output requires non-empty text")
        if len(self.text) > MAX_SPEECH_TEXT_CHARS:
            raise ValueError("speech output exceeds the output bound")
        spoken = speakable(self.text) or self.text.strip()
        object.__setattr__(self, "text", spoken)


@dataclass(frozen=True)
class SpeechArtifact:
    """Provider-neutral reference to rendered speech."""

    reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.reference, str) or not self.reference.strip():
            raise ValueError("speech artifact requires a reference")


class SpeechProvider(ABC):
    """Interface for rendering one final response as speech."""

    @abstractmethod
    def speak(self, output: SpeechOutput) -> SpeechArtifact:
        """Render the supplied response text and return its artifact."""


_CHUNK_PARAGRAPH = re.compile(r"\n+")
_SENTENCE_END = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s|\Z)")
_INITIALISM = re.compile(r"^(?:[A-Za-z]\.)+[A-Za-z]?$")
_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "st",
        "sr",
        "jr",
        "vs",
        "etc",
        "ie",
        "eg",
        "approx",
        "inc",
        "ltd",
        "corp",
        "dept",
        "am",
        "pm",
    }
)


def _false_boundary(prefix: str, terminators: str) -> bool:
    """Reject a sentence-end candidate that most likely continues the text."""

    if set(terminators) - {'"', "'", "”", "’", ")", "]"} != {"."}:
        return False
    if not prefix or prefix[-1].isdigit():
        return True
    word = re.search(r"\S+$", prefix)
    if word is None:
        return False
    token = word.group()
    if len(token) == 1 and token.isalpha():
        return True
    return bool(
        _INITIALISM.match(token) or token.lower().rstrip(".") in _ABBREVIATIONS
    )


def sentence_chunks(text: str) -> list[str]:
    """Split one reply into speakable sentences, preserving punctuation.

    Deterministic and dependency-free: every line break splits, so a
    bullet, a numbered step or a table row is spoken on its own;
    otherwise a split follows a run of ``.!?…`` (plus optional closing
    mark) that whitespace succeeds, unless it looks like an abbreviation,
    an initial or a decimal. Fragments below the minimum chunk length are
    absorbed into their neighbour, and anything still longer than
    :data:`MAX_SPEECH_CHUNK_CHARS` is split once more between words, so no
    reply is ever handed to a worker as one request it cannot answer in
    time. A reply with no split point and no length returns exactly one
    chunk.
    """

    chunks: list[str] = []
    for paragraph in _CHUNK_PARAGRAPH.split(text):
        start = 0
        for match in _SENTENCE_END.finditer(paragraph):
            if _false_boundary(paragraph[: match.start()], match.group()):
                continue
            segment = paragraph[start : match.end()].strip()
            if segment:
                chunks.append(segment)
            start = match.end()
        remainder = paragraph[start:].strip()
        if remainder:
            chunks.append(remainder)
    merged: list[str] = []
    for chunk in chunks:
        if merged and len(chunk) < MIN_SPEECH_CHUNK_CHARS:
            merged[-1] = f"{merged[-1]} {chunk}"
        else:
            merged.append(chunk)
    if len(merged) > 1 and len(merged[0]) < MIN_SPEECH_CHUNK_CHARS:
        merged[1] = f"{merged[0]} {merged[1]}"
        del merged[0]
    bounded: list[str] = []
    for chunk in merged:
        for part in _bounded(chunk):
            # The absorption rule again: a tail too short to be a phrase of
            # its own rides on the part before it, even when that leaves the
            # pair a few characters over the bound. A slightly long chunk is
            # a worse breach of this module than a broken word.
            if bounded and len(part) < MIN_SPEECH_CHUNK_CHARS:
                bounded[-1] = f"{bounded[-1]} {part}"
            else:
                bounded.append(part)
    return bounded


def _bounded(chunk: str) -> list[str]:
    """Split one over-long chunk between words, never inside one.

    A run-on paragraph and a list whose items end without punctuation are
    both speakable units a sentence splitter cannot help with, and one of
    2300 characters is a job the resident worker is killed for. The break
    lands on the last whole word that fits; a single token longer than the
    bound (a URL, a base64 blob) goes whole rather than in halves, because
    half a word read aloud is worse than a long one.
    """

    if len(chunk) <= MAX_SPEECH_CHUNK_CHARS:
        return [chunk]
    parts: list[str] = []
    rest = chunk
    while len(rest) > MAX_SPEECH_CHUNK_CHARS:
        index = MAX_SPEECH_CHUNK_CHARS
        while index > 0 and not rest[index - 1].isspace():
            index -= 1
        if index == 0:
            index = len(rest.split(maxsplit=1)[0])
        parts.append(rest[:index].strip())
        rest = rest[index:].strip()
    if rest:
        parts.append(rest)
    return parts
