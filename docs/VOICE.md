# Stella Voice Mode

## How it works

Voice is another input/output interface to the same Stella application the
text UI uses — not a second assistant. One press of **Listen** records exactly
one utterance; a second press stops and transcribes it. The transcript is then
sent through `StellaSession.run_turn`, the identical shared turn path as a
typed message: same conversation history, memory retrieval and writes, Brain
decisions, `ToolDispatcher` risk checks, approvals, and verification receipts.
There is no voice-specific reasoning path.

When speech output is enabled (the "Speak replies" checkbox), the final
response text — and only that text — is rendered through the existing
`SpeechProvider` abstraction and played. The text stays visible either way.
Internal trace/debug information is never spoken.

Flow:

```text
Listen button -> Recorder (one utterance) -> TranscriptionProvider
  -> transcript shown as "You (voice): ..."
  -> StellaSession.run_turn (same path as typed input)
  -> response shown as text
  -> optional SpeechProvider -> Player -> audio (text remains visible)
```

A failed turn still produces an honest reply line; a failed voice stage never
replaces it. If transcription fails or is empty, nothing is sent to Stella —
a failed transcript is never replaced with invented text. If synthesis or
playback fails, the text response remains fully available. Playback can be
stopped with **Stop speaking**; that cancels the audio only and never alters
the underlying Stella decision or its history entry.

## UI states

The status line distinguishes "Listening...", "Transcribing...",
the elapsed "Stella is working · N s" turn state, and "Speaking...", and
returns to idle when the utterance is finished.
"Listening..." appears only while the recorder is
actually running, and speech is only claimed after a transcript was produced.

## Controls

- **Listen / Stop** — start or stop one explicit recording. No continuous
  listening, no wake word, no background recording.
- **Cancel** — abort the current recording without transcribing it.
- **Stop speaking** — end the current playback; the turn is unaffected.
- **Speak replies** — toggle speech output; it defaults to off.

The microphone and speech buttons are disabled when the corresponding
capability is not available in this configuration, so the UI never pretends
voice works when it cannot.

## Providers and configuration

All voice providers are optional and selected through the environment; nothing
is mandatory and nothing fails at startup when audio tools are absent.

- `STELLA_VOICE_TRANSCRIPTION` — `auto` (default), `openai`, or `off`.
  `auto` prefers a local command when `STELLA_TRANSCRIPTION_COMMAND` is set and
  otherwise uses OpenAI Whisper when `OPENAI_API_KEY` is present; otherwise
  voice input stays unavailable.
- `STELLA_VOICE_SPEECH` — `auto` (default), `openai`, or `off`. `auto` prefers
  `STELLA_SPEECH_COMMAND`, then local `espeak-ng`/`espeak`, then OpenAI TTS
  with an API key; otherwise speech output stays unavailable.
- `STELLA_TRANSCRIPTION_COMMAND` — a local command template that must contain
  `{input}` and prints the transcript on stdout (for example a `whisper.cpp`
  wrapper). Executed without a shell.
- `STELLA_SPEECH_COMMAND` — a local command template containing `{text}` and
  `{output}` that writes one audio file (for example a `piper` wrapper).
- `STELLA_TRANSCRIPTION_MODEL` (default `whisper-1`), `STELLA_SPEECH_MODEL`
  (default `tts-1`), `STELLA_SPEECH_VOICE` (default `alloy`) — OpenAI
  identifiers when the cloud path is selected.

Recording uses `pw-record` or `arecord` when installed; playback uses
`pw-play`, `paplay`, or `aplay`. Synthesis fallback is `espeak-ng`/`espeak`.

## Privacy

- Local-first: local capture, local command-based transcription/synthesis, and
  local playback are preferred; no cloud speech service is required or used
  unless configured or explicitly selected.
- The recording lives in a short-lived temporary directory and is deleted
  immediately after transcription — on success and on failure alike.
- No audio is ever stored in conversation history or memory: history contains
  the resulting text message only.
- Speech artifacts are removed after playback, and provider temp directories
  are disposed of at shutdown.
- Recording happens only between an explicit Listen press and its stop; there
  is no background or always-on capture and no autonomous voice-triggered
  action.

## Security posture

A transcript has exactly the authority of typed user input — which is to say
none of its own. It cannot approve a tool: `DANGEROUS` actions still raise the
same approval request through `ApprovalBroker`, and spoken words such as
"approve it now" are ordinary untrusted content that can never construct a
`ToolApproval`. Filesystem and network verification receipts, memory tools,
and reminder boundaries behave identically for spoken and typed requests.
Playback output is an interface rendering; nothing it says feeds back into the
decision path.

## Known limitations

- One utterance per Listen press; no streaming recognition, continuous
  conversation, speaker identification, or emotion detection.
- Local transcription quality depends entirely on the configured command; Stella
  performs no speech modeling of its own.
- `espeak` output is a fixed robotic voice and follows the text's language as
  best it can; there is no voice selection beyond `STELLA_SPEECH_VOICE` on the
  OpenAI path.
- Playback is one subprocess per artifact and sequential; stopping is immediate
  but there is no queue management.
- Voice mode is desktop-UI only; the CLI remains text-only.
