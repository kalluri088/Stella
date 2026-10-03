"""The spoken round trip: local synthesis out, local transcription back.

Gated on ``STELLA_VOICE_ROUNDTRIP=on`` because this is the only file in the
suite that runs two real speech engines. It is skipped, never silently
passed, when the switch is off, so an ordinary ``pytest`` stays as cheap as
it always was.

Being explicit about engines is not the same as being noisy: the microphone
is never opened here. The audio is synthesized by the resident worker Stella
would itself pick, and handed to the transcriber as the file a recorder
"happened to stop on" — the same ``InputPart`` the live path produces. Two
engines and one claim: *every word survives*. That claim is what an engine
comparison has to answer, and one lost word is an answer.
"""

from __future__ import annotations

import os
import re

import pytest

from stella.app import StellaSettings, _build_speech_provider, _build_transcriber
from stella.audio_output import SpeechOutput
from stella.context import InputModality, InputPart, InputProvenance

#: Plain sentences on purpose. A transcriber that renders "12:40" as
#: "twelve forty" would be compared against a synthesizer that says the
#: digits, and that difference belongs to spelling, not to comprehension —
#: which is the only thing this harness claims to measure.
PHRASES = (
    "The kitchen timer has three minutes left.",
    "Bring the blue folder to the meeting at noon.",
    "Stella keeps every word on this machine.",
)

_ON = {"1", "true", "on", "yes"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.casefold())


@pytest.fixture(autouse=True)
def _require_the_switch() -> None:
    if os.environ.get("STELLA_VOICE_ROUNDTRIP", "").strip().casefold() not in _ON:
        pytest.skip(
            "the round trip runs real speech engines; set "
            "STELLA_VOICE_ROUNDTRIP=on to ask for it"
        )


def test_every_word_survives_local_speech_and_local_transcription() -> None:
    # Both sides go through the same builders a launched Stella uses, so
    # what is measured here is the pair this machine actually has — not a
    # test's idea of what voice sounds like.
    settings = StellaSettings(model="test")
    speech = _build_speech_provider(settings)
    transcriber = _build_transcriber(settings)
    if speech is None or transcriber is None:
        pytest.skip(
            "this machine has no local speech and local transcription pair "
            "to compare"
        )
    for phrase in PHRASES:
        artifact = speech.speak(SpeechOutput(phrase))
        try:
            heard = transcriber.transcribe(
                InputPart(
                    modality=InputModality.AUDIO,
                    provenance=InputProvenance.USER,
                    reference=artifact.reference,
                )
            )
        finally:
            try:
                os.remove(artifact.reference)
            except OSError:
                pass
        engine = (
            f"[{type(speech).__name__} -> "
            f"{getattr(transcriber, 'name', type(transcriber).__name__)}]"
        )
        wanted = _words(phrase)
        got = _words(heard or "")
        # One greedy pass answers both claims at once — that every word
        # survived and that they came back in order — because each word is
        # looked for only after the previous one was found. A membership
        # test alone could not make the second claim, and index lookups
        # would trip over a sentence that says "the" twice.
        cursor = 0
        for word in wanted:
            for index in range(cursor, len(got)):
                if got[index] == word:
                    cursor = index + 1
                    break
            else:
                pytest.fail(
                    f"{engine} lost {word!r} or heard it out of order: "
                    f"{phrase!r} came back as {heard!r}"
                )
