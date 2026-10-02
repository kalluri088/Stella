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

What reaches a speaker is the reply's **words**, not its screen formatting.
`SpeechOutput` — the one boundary every provider is called through — passes
the text through `stella.spoken_form.speakable()`, which strips markdown a
synthesizer would otherwise read aloud as noise: heading hashes, `**bold**`
and `*italic*` markers, backticks, bullet and number markers, table pipes,
horizontal rules, blockquote chevrons, emoji and zero-width marks. A list is
joined into one running sentence with commas, which is also what gives the
voice its pauses. Two conversions say something instead of nothing: a written
link keeps its label and drops its address, and a bare URL becomes "a link" —
a listener cannot open an address. Nothing else is rewritten: no paraphrase,
no reordering, no number or time expansion (reading "18:00" as "six p.m." is
a claim about the user's clock that Stella does not own), and no summarising.
The visible reply is always the model's own text, markup and all.

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

The whole voice periphery is cancellable. **Cancel** during "Transcribing..."
abandons the capture and a running local transcription command within about a
second, reporting that nothing was sent. **Cancel** during a turn also
silences spoken audio, and speech synthesized around a cancel is discarded
rather than played. Honest residue: an abandoned cloud transcription or
speech request is given up on without closing its socket (the SDK exposes no
per-request cancel), and its result is never used.

## Chunked speech

A multi-sentence reply is spoken sentence by sentence (A9). The final response
is split deterministically with the standard library (`sentence_chunks` in
`stella/audio_output.py`: terminal punctuation and blank-line paragraph breaks,
with abbreviation, initial and decimal guards, and tiny fragments absorbed into
their neighbour). The worker thread synthesizes one chunk at a time while a
playback thread plays and removes each artifact as it arrives — because local
synthesis runs faster than real time, the first sentence starts speaking after
one sentence of rendering instead of after the whole reply. A single-sentence
reply takes the ordinary whole-file path unchanged.

The consequences users should know:

- **Stop speaking** ends the whole spoken reply — the sentence playing now and
  the sentences only queued — never just the current one; the underlying
  decision is still untouched.
- **Cancel** silences a chunked reply the same way: the in-flight synthesis is
  abandoned, queued audio is discarded, and nothing further reaches a speaker.
- If synthesis fails mid-reply, the sentences already rendered are still
  spoken, one honest error line reports the rest, and the text response stays
  fully available.
- Chunk order is reply order; every artifact is removed once heard, and a
  newer reply retires the previous reply's unplayed queue.

## Resident synthesis worker

Measuring the Kokoro command path sentence by sentence (research report 24)
showed the per-sentence cost is almost entirely fixed start-up — interpreter
launch plus model load, over 3 s every call — while the synthesis itself runs
at roughly 0.4× real time. Chunking cannot remove that floor because each
chunk pays it again. `STELLA_SPEECH_RESIDENT=on` takes it out by keeping **one
long-lived worker process** instead of one process per sentence: the first
sentence pays the start-up, every later sentence pays only synthesis.

The worker speaks a tiny line-JSON protocol (the same shape the laya judge
uses): it prints `{"ready": true}` once its model is loaded, then answers each
request line `{"id": int, "text": str, "output": str}` with `{"id": int,
"ok": bool, "error": str}` after writing a playable file to `output`. Stella
chooses the artifact paths in its own temporary directory and never trusts a
path from a worker reply. A worker that dies, times out or answers badly is
retired on the spot and a fresh one starts for the next sentence; a broken
worker degrades to no speech with the text reply still fully available, and
the plain `STELLA_SPEECH_COMMAND` path is one env var away. (On this
machine `~/tools/stella-speak-server` is such a worker for the local Kokoro
install; it lives outside the repository like all bench/tooling scripts.)

The flag only matters when `STELLA_SPEECH_COMMAND` is set — there is nothing
to make resident otherwise — and it is environment-only like all voice
configuration.

```bash
STELLA_SPEECH_COMMAND="$HOME/tools/stella-speak-server" \
STELLA_SPEECH_RESIDENT=on uv run stella-ui
```

## Spoken conversation turns

A voice turn counts as a *spoken conversation* only when the transcript
arrives from the microphone **and** "Speak replies" is on. The bridge makes
that call at the trusted application edge, and a spoken turn changes exactly
two things — both presentation-only:

- **Brevity.** The turn reaches the core with an audio-modality input
  envelope, and the runtime then attaches one fixed, application-authored
  style note to the final-answer prompt: the answer will be heard, so it
  should be one or two short spoken sentences, with no lists, code, markdown
  or unreadable symbols. Voice input with speech output switched off stays
  the ordinary text path — the microphone alone does not make a turn spoken;
  being *heard* does.
- **Work narration (D3).** The runtime reports the phase a turn has just
  entered — deciding, or dispatching a selected tool — and the application
  may speak a short filler phrase **it wrote itself**, chosen from the fixed
  set in `NARRATION_PHRASES` ("Let me think about that." / "Working on that
  now."). This answers the "quieter and worse" problem: a listening user can
  tell Stella is working from Stella being stuck. The model never authors
  narration, narration decides nothing, and no approval, risk or audit
  behavior changes — it is presentation of an event the application already
  knows.

The narration rules the tests enforce:

- One phrase at a time. The bridge has a single narration slot; a busy slot
  drops the new phrase — narration never queues up behind itself.
- It can never slow a turn. The phase observer only takes the slot;
  synthesis and playback happen on a daemon thread while the worker keeps
  working.
- The answer *is* the narration. When reply speech starts, a phrase that has
  not begun playing is discarded unheard; so do **Stop speaking** and
  **Cancel**.
- A failed narration is silent — no error line, no retry — because the real
  reply is what the user asked for, and it reports its own problems.
- The "answering" phase has no phrases by design: its speech is the reply.
- Approval prompts are never narrated and never described by a phrase;
  narration is phase-level only, so nothing spoken pressures the answer to
  an approval question.

## Spoken alerts

An Outline reminder this process claims is delivered as one amber chat line
(`REMINDERS.md`). When speech output is on, the same line is also spoken — a
background alert reaching the ear as well as the screen, which is what makes
the desktop window useful in a voice session rather than only glanceable.

Three rules keep it a notification and not a second conversation:

- It speaks **only** when the user switched speech on. A silent setup never
  hears it, and turning speech off is a complete mute.
- One announcement at a time, in its own slot: an alert that arrives while
  Stella is already speaking is dropped unheard. The visible line already
  said everything, so nothing is lost and announcements never pile up.
- The answer is still the priority. A reply, a cancel, or **Stop speaking**
  retires an alert that has not started; unheard audio never follows the
  reply onto the speakers.

The spoken text is the runtime's own line ("Outline reminder (task): …"), so
the model authors nothing that is heard here. The reminder title itself is
untrusted Outline content, exactly as it is on screen: it is rendered as
speech and nothing else — it cannot approve a tool, start a turn, or reach
the Brain, and it passes through the same markup-stripping boundary as a
reply (`stella/spoken_form.py`).

## Barge-in (optional interrupt-by-voice)

By default Stella's microphone is only live between a **Listen** press and its
stop. Barge-in adds one controlled exception: while Stella is
speaking, a small listener ("the ear") watches the microphone for the user's
voice, and a confirmed utterance does exactly what the **Cancel** button
already does — stop the audio and cancel the turn. The detector has no other
authority: it cannot approve tools, cannot inject text, and produces one
interruption per speaking episode. Outside of speech playback the ear is not
running at all, so there is still no always-on listening.

The default mode is `auto`: the ear arms only once you have named a capture
source with `STELLA_BARGE_SOURCE` — which is also the documented way to point
it at an echo-cancelled microphone. Declaring the source *is* enabling the
feature; no second variable is needed.

> **Resolved and accepted (live testing, 2026-09-26 → 2026-09-28):** an
> earlier round of live testing found that enabling the ear appeared to break
> Stella's *playback* — the first one or two sentences of each reply seemed
> stretched and glitched (research report 19; leading hypothesis: PipeWire
> real-time starvation on the busy machine). Instrumented sessions with
> per-play ratio measurement (research reports 28–29) did **not** reproduce
> it: full plays up to 7 s ran at 1.01–1.02× real time with the ear armed,
> `pw-play` stderr stayed empty, and the probe logged no xruns. The human
> double-talk acceptance passed: three interrupted replies at 383 / 128 /
> 127 ms from voice onset to cancel (bar ≤500 ms), and two silent controls
> with zero false fires. Barge-in is therefore accepted for live use, but
> only on the echo-cancelled path the measurement was made on — see
> the caveat below for why the default still refuses to arm an ear pointed
> at a raw mic. Text UI, push-to-talk voice, and chunked speech are
> unaffected.

Detection is fully local: raw 16 kHz mono capture (`pw-record`, falling back to
`arecord`) is fed frame by frame through Silero VAD running as an ONNX model;
five consecutive voiced frames (about 160 ms) above both a probability and an
energy threshold count as the user talking. Nothing is recorded, buffered for
transcription, or persisted — the only output is the interrupt itself.

Setup (all of it optional; the default needs nothing and arms no ear):

```bash
uv sync --extra barge-in            # adds onnxruntime only
# Put the Silero VAD ONNX model somewhere readable, e.g.
#   ~/models/silero/silero_vad.onnx   (or point STELLA_VAD_MODEL elsewhere)
STELLA_BARGE_SOURCE=ec_mic uv run stella-ui   # auto-arms the ear
```

Environment variables (voice config is environment-only and is never written
to `config.json`):

- `STELLA_VOICE_BARGE_IN` — `auto` (default: arms only when
  `STELLA_BARGE_SOURCE` names a capture), `on` (arm on any source, including
  the system default) or `off` (never).
- `STELLA_VAD_MODEL` — path to the Silero VAD v6 ONNX file.
- `STELLA_BARGE_SOURCE` — capture target name, e.g. `ec_mic`. Under `auto`
  this doubles as the enable switch; unset, the ear never arms.
- `STELLA_BARGE_THRESHOLD` — VAD probability in (0, 1), default `0.5`.

Echo cancellation is the make-or-break prerequisite: without it Stella's own
voice through the speakers registers as speech (measured on this machine —
23% of playback frames looked voiced; with cancellation that fell to 0%). The
recommended setup is PipeWire's WebRTC echo-cancel module, which replaces the
raw devices with a stereo pair (`ec_out` / `ec_mic`) at the Pulse-server layer
and needs no changes in Stella at all:

```bash
# IMPORTANT: the built-in speakers/mic must be the defaults *before* loading,
# or the module binds to the wrong devices.
pactl set-default-sink  alsa_output.pci-0000_00_1f.3.analog-stereo
pactl set-default-source alsa_input.pci-0000_00_1f.3.analog-stereo
pactl load-module module-echo-cancel aec_method=webrtc \
    source_name=ec_mic sink_name=ec_out
pactl set-default-sink ec_out
pactl set-default-source ec_mic
```

Stella's playback and recordings then follow the cancelled pair automatically.
Under `auto` you should still pass `STELLA_BARGE_SOURCE=ec_mic`: naming the
cancelled source is what arms the ear, because Stella cannot tell a
cancelled pair from a raw mic on its own — and the measured cost of getting
that wrong is the ear interrupting Stella's own voice every reply.
With a Bluetooth headset the echo path is ~40 dB down by itself, so the raw
devices are fine — route Bluetooth around the `ec_*` pair, because pulling SCO
through echo-cancel triggers codec switches (measured, research report 08).

When barge-in is enabled but cannot work, only the ear is disabled and nothing
else changes: a missing extra or model reports one friendly error at startup,
an internal fault retires the ear silently for the rest of the session, and
text, Listen and speech all keep working.

## One microphone

Push-to-talk, the wake ear and the utterance watcher that endpoints a
wake-initiated recording share a single capture process. One `pw-record`
child reads the device and hands whole frames to whoever is subscribed, so
the microphone is never opened three times at once — the state that makes
every "device busy" voice failure hard to explain. A subscriber that falls
behind loses its oldest frame rather than stalling the reader; if the capture
dies, each consumer is told once and retires quietly, and Stella's text path
carries on. Barge-in deliberately keeps its own process: it is only ever armed
while Stella speaks, when the wake ear is suspended, and it may point at a
different echo-cancelled source.

## UI states

The status line distinguishes "Listening...", "Transcribing...",
the elapsed "Stella is working · N s" turn state, and "Speaking...", and
returns to idle when the utterance is finished.
"Listening..." appears only while the recorder is
actually running, and speech is only claimed after a transcript was produced.

## Controls

- **Listen / Stop** — start or stop one explicit recording. Recording only
  happens inside an explicit press, or inside one wake-initiated capture
  when `STELLA_WAKE_WORD=on`; there is no background recording either way.
- **Wake word (opt-in)** — with `STELLA_WAKE_WORD=on` a local detector holds
  the microphone open while Stella is idle, and one confirmed phrase is
  exactly a **Listen** press: it starts the same capture, records nothing
  until it fires, and gains no authority of its own. Off by default, and
  suspended whenever Stella is speaking, narrating or already capturing.
- **Cancel** — while listening, abort the current recording without
  transcribing it; while transcribing, abandon the transcription in flight.
- **Stop speaking** — end the current spoken reply (playing sentence and
  queued ones); the turn is unaffected.
- **Speak replies** — toggle speech output; it defaults to off.

The microphone and speech buttons are disabled when the corresponding
capability is not available in this configuration, so the UI never pretends
voice works when it cannot.

## Providers and configuration

All voice providers are optional and selected through the environment; nothing
is mandatory and nothing fails at startup when audio tools are absent.

When a cloud voice provider needs an OpenAI key, it is resolved exactly like
the brain's: `OPENAI_API_KEY` for this launch, else the stored key for the
**OpenAI slot only** (`api_keys.json`; see `SECRETS.md`). Keys stored for
other presets (Claude, Grok, …) are never offered to OpenAI-compatible
transcription or speech endpoints.

- `STELLA_VOICE_TRANSCRIPTION` — `auto` (default), `openai`, or `off`.
  `auto` prefers a local command when `STELLA_TRANSCRIPTION_COMMAND` is set and
  otherwise uses OpenAI Whisper when an OpenAI key is available; otherwise
  voice input stays unavailable.
- `STELLA_VOICE_SPEECH` — `auto` (default), `openai`, or `off`. `auto` prefers
  `STELLA_SPEECH_COMMAND`, then local `espeak-ng`/`espeak`, then OpenAI TTS
  with an API key; otherwise speech output stays unavailable.
- `STELLA_TRANSCRIPTION_COMMAND` — a local command template that must contain
  `{input}` and prints the transcript on stdout (for example a `whisper.cpp`
  wrapper). Executed without a shell.
- `STELLA_SPEECH_COMMAND` — a local command template containing `{text}` and
  `{output}` that writes one audio file (for example a `piper` wrapper).
- `STELLA_SPEECH_RESIDENT` — `off` (default) or `on`; only meaningful with
  `STELLA_SPEECH_COMMAND` (see "Resident synthesis worker").
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
  is no always-on capture and no autonomous voice-triggered action. The one
  exception is opt-in barge-in, and even then the microphone feeds only a
  local voiced/not-voiced decision that is never recorded or stored.

## Security posture

A transcript has exactly the authority of typed user input — which is to say
none of its own. It cannot approve a tool: `DANGEROUS` actions still raise the
same approval request through `ApprovalBroker`, and spoken words such as
"approve it now" are ordinary untrusted content that can never construct a
`ToolApproval`. Filesystem and network verification receipts and memory
tool boundaries behave identically for spoken and typed requests.
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
- Playback is one subprocess per chunk and strictly sequential; a chunked
  reply keeps at most a few synthesized sentences ahead of the speakers.
  Synthesis is one process per sentence unless the resident worker is enabled.
  There is still no mixing, ducking, or overlap between artifacts.
- Voice mode is desktop-UI only; the CLI remains text-only.
