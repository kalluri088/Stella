"""Provider-neutral one-shot speech-output boundary."""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from stella.spoken_form import speakable

MAX_SPEECH_TEXT_CHARS = 4000
MIN_SPEECH_CHUNK_CHARS = 12


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


_CHUNK_PARAGRAPH = re.compile(r"\n[ \t]*\n+")
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

    Deterministic and dependency-free: paragraph breaks always split;
    otherwise a split follows a run of ``.!?…`` (plus optional closing
    mark) that whitespace succeeds, unless it looks like an abbreviation,
    an initial or a decimal. Fragments below the minimum chunk length are
    absorbed into their neighbour, so a reply with no split point returns
    exactly one chunk.
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
    return merged


class StreamingSentenceSplitter:
    """Release whole sentences from text that arrives a piece at a time.

    A spoken reply is buffered today: the first sentence cannot be
    synthesized until the whole answer exists. This makes the same
    deterministic :func:`sentence_chunks` rule usable incrementally so a
    reply can start on its first finished sentence.

    On each :meth:`feed` the text accumulated so far is re-chunked and
    every chunk except the last is released — the last is always held
    back, because appending more text can still change it (a new fragment
    merges *forward* into the tail, and a terminator that saw end-of-
    buffer may not really have ended). It never changes an already-
    released chunk: ``sentence_chunks`` is pure, its only merge pulls a
    short fragment into the *previous* element, and a released element is
    provably not the tail and already at least the minimum length, so no
    later fold can reach it. :meth:`flush` releases whatever remains,
    including the held tail, so the concatenation of everything returned
    across the life of one reply equals ``sentence_chunks(whole_text)``.
    """

    def __init__(self) -> None:
        self._seen: list[str] = []
        self._released = 0

    def feed(self, delta: str) -> list[str]:
        """Hand back the sentences that are now complete, if any."""

        if not delta:
            return []
        self._seen.append(delta)
        ready = sentence_chunks("".join(self._seen))[:-1]
        fresh = ready[self._released :]
        self._released += len(fresh)
        return fresh

    def flush(self) -> list[str]:
        """Release every not-yet-handed chunk, the held tail included."""

        chunks = sentence_chunks("".join(self._seen))
        fresh = chunks[self._released :]
        self._released = len(chunks)
        return fresh
