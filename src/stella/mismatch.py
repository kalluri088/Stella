"""Advisory mismatch warning for approval prompts (display-only).

When Stella asks for approval of a sensitive action, the user's own
turn is the strongest evidence that the action was actually requested.
A model error — or a decision shaped by untrusted tool or web content —
can ask for something the user never mentioned. This module turns that
gap into one advisory line shown beside the action preview.

It changes no authority: approval still binds solely to the exact
``ApprovalRequest``, and every judgement here is made by trusted
application code from the user's own text, never by the model. A None
return means "no objection detected", not "safe" — the warning is a
nudge, and its absence grants nothing.
"""

import re
from collections.abc import Sequence
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlparse

# One warning sentence per approval-requiring capability: what the
# user's turn would have had to mention. Capabilities absent here (all
# non-approval ones, plus anything unknown) never warn — a false alarm
# on every prompt would train the user to ignore the real one.
_ACTION_PHRASES: dict[str, str] = {
    "filesystem_write": "writing that file",
    "filesystem_edit": "editing that file",
    "filesystem_delete": "deleting that file",
    "filesystem_read": "reading that file",
    "persona_edit": "changing your persona, voice or style",
    "memory_write": "remembering something",
    "memory_update": "updating a memory",
    "memory_forget": "forgetting something",
    "network_read": "sending or fetching that over the network",
    "key_send": "typing those keystrokes somewhere",
    "web_search": "searching the web",
    "web_fetch": "opening that web page",
    "screen_read": "reading your screen",
}

# Word-prefix stems: a user token starting with any stem counts as the
# action being mentioned (writ → write/writes/writing/written).
_VERB_STEMS: dict[str, tuple[str, ...]] = {
    "filesystem_write": ("writ", "wrot", "save", "creat", "put", "add",
                         "dump", "record", "jot", "note", "file", "keep"),
    "filesystem_edit": ("edit", "chang", "updat", "fix", "modif", "append",
                        "replac", "renam", "amend", "tweak", "adjust",
                        "correct", "revise", "expand", "contract"),
    "filesystem_delete": ("delet", "remov", "eras", "clear", "wipe",
                          "trash", "unlink", "purge", "drop", "scrap",
                          "bin", "nuke"),
    "filesystem_read": ("read", "show", "open", "view", "display",
                        "print", "look", "see", "what", "cat"),
    "persona_edit": ("persona", "voice", "style", "tone", "personality",
                     "call me", "sound", "speak", "talk", "respond",
                     "phrase", "wording"),
    "memory_write": ("remember", "memor", "note", "store", "keep", "save",
                     "recall", "learn", "know that"),
    "memory_update": ("updat", "chang", "correct", "fix", "amend",
                      "revise", "edit"),
    "memory_forget": ("forget", "disregard", "ignore", "unremember"),
    "network_read": ("post", "send", "upload", "sync", "push",
                     "publish", "download", "fetch", "request", "api",
                     "endpoint", "curl"),
    "key_send": ("type", "send", "message", "press", "submit",
                 "keystroke", "keyboard", "chat", "text", "write him",
                 "write her", "whatsapp", "tell him", "tell her"),
    "web_search": ("search", "lookup", "find", "google", "research",
                   "who", "news", "check"),
    "web_fetch": ("open", "visit", "fetch", "load", "read", "browse",
                  "see", "pull", "check", "url", "link", "website",
                  "site", "page"),
    "screen_read": ("screen", "screenshot", "desktop", "window", "see",
                    "look", "read", "show", "what"),
}

# Broad delegation: the user handed over a task without enumerating the
# individual actions. Multi-word phrases and clearly delegating stems —
# deliberately narrow, since the wider these get, the quieter the
# warning becomes.
_GENERIC_STEMS = ("organiz", "reorganiz", "manage", "handl", "deal",
                  "proceed", "continue", "cleanup")
_GENERIC_PHRASES = ("take care", "sort out", "set up", "do it",
                    "go ahead", "clean up", "as you see fit",
                    "whatever you", "yourself", "wrap up", "tidy")

# Argument keys that carry a human-visible target, in probe order.
_TARGET_KEYS = ("path", "url", "title", "name", "query", "address",
                "summary", "content")

_STOP_TOKENS = frozenset({
    "the", "a", "an", "this", "that", "for", "and", "with", "from",
    "into", "onto", "com", "www", "http", "https", "org", "net",
    "file", "files", "folder", "dir", "txt", "md", "tmp", "home",
    "user", "you", "your", "my", "stella",
})

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(_normalize(text)))


def _hits(words: set[str], text: str, entries: tuple[str, ...]) -> bool:
    """True when any entry is mentioned: word-prefix for single words,
    substring for multi-word phrases (checked against normalized text)."""
    for entry in entries:
        if " " in entry:
            if entry in text:
                return True
        elif any(word.startswith(entry) for word in words):
            return True
    return False


def _target_tokens(arguments: dict[str, object]) -> set[str]:
    tokens: set[str] = set()
    for key in _TARGET_KEYS:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if key == "path":
            # Probe both separator dialects: a Windows-style path on
            # Linux still names its file.
            for pure in (PurePosixPath(value), PureWindowsPath(value)):
                tokens |= {
                    word for word in _tokens(pure.name)
                    if len(word) >= 3 and word not in _STOP_TOKENS
                }
        elif key == "url":
            parsed = urlparse(value)
            tokens |= {
                part for part in parsed.netloc.replace("-", ".").split(".")
                if len(part) >= 4 and part not in _STOP_TOKENS
            }
            tokens |= {
                part for part in re.split(r"[/\-_.]", parsed.path)
                if len(part) >= 3 and part not in _STOP_TOKENS
            }
        elif key == "content":
            # Prose target: only distinctive words count, so a memory
            # about "quarterly revenue" matches "what did I note about
            # revenue", but generic filler never creates a false hit.
            tokens |= {
                word for word in _tokens(value)
                if len(word) >= 5 and word not in _STOP_TOKENS
            }
        else:
            tokens |= {
                word for word in _tokens(value)
                if len(word) >= 3 and word not in _STOP_TOKENS
            }
    return tokens


def approval_mismatch_warning(
    user_text: str | None,
    capability: str | None,
    arguments: dict[str, object],
    history: Sequence[str] | None = None,
) -> str | None:
    """Return one advisory line, or None when nothing looks off.

    Warns only when the user's turn mentions neither the action (verb
    stems for the capability, or any delegation phrase) nor the target
    (any distinctive token from the request's nameable arguments).
    ``history`` is the earlier user messages of the conversation: a
    request that spans turns ("save this to a file" … "the meeting
    notes") is still the user's request, so a prior message that
    mentions both the action and the target also silences the warning.
    One-sided support does not — a stray earlier verb or path token is
    topic noise, not evidence this exact action was asked for.
    """

    if capability not in _ACTION_PHRASES:
        return None
    if not user_text or not user_text.strip():
        # No current-turn text (batch, idle proposal drain): claiming
        # a mismatch would be a lie, so say nothing.
        return None
    text = _normalize(user_text)
    words = _tokens(text)
    if _hits(words, text, _VERB_STEMS[capability]):
        return None
    if _hits(words, text, _GENERIC_STEMS + _GENERIC_PHRASES):
        return None
    target_words = _target_tokens(arguments)
    if target_words & words:
        return None
    verb_stems = _VERB_STEMS[capability] + _GENERIC_STEMS + _GENERIC_PHRASES
    for prior in history or ():
        if not prior or not prior.strip():
            continue
        prior_text = _normalize(prior)
        prior_words = _tokens(prior_text)
        if target_words & prior_words and _hits(prior_words, prior_text, verb_stems):
            return None
    return (
        f"Heads up: your request didn't mention {_ACTION_PHRASES[capability]} "
        "or what it targets — approve only if you expected this."
    )
