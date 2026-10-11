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

Wake word on (the ear replaces the finger, never the path):
  shared tap -> wake classifier (records nothing) -> the same Listen press
    -> Recorder -> ... the identical chain above ...

Barge-in armed (a named echo-cancelled source, during playback only):
  its own capture -> Silero VAD -> a confirmed utterance -> what Cancel does
    -> audio stops, the turn is cancelled; nothing is transcribed or sent
```

Neither branch adds a reasoning path: both end in something a button already
does, which is why **Mute mic** needs no branch of its own. It lowers both
ears and refuses the press, and the chain above simply never begins.

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
`stella/audio_output.py`: terminal punctuation, **every line break**, with
abbreviation, initial and decimal guards, tiny fragments absorbed into their
neighbour, and any chunk still longer than `MAX_SPEECH_CHUNK_CHARS` (600
characters) split once more between words). The worker thread synthesizes one
chunk at a time while a playback thread plays and removes each artifact as it
arrives — because local synthesis runs faster than real time, the first sentence
starts speaking after one sentence of rendering instead of after the whole reply.
A single-sentence reply takes the ordinary whole-file path unchanged.

The bound exists because of a measurement, not a preference. Rendered on this
machine's installed Kokoro worker (2026-10-10), synthesis costs about **0.03 s
per character** — 14 s for 423 characters, 41 s for 1410, 71 s for 2397, 88 s for
2961 — while the resident worker's budget for one request is a flat 60 s. Before
the bound, a 20-bullet reply (no punctuation, so no sentence boundary, and only
*blank* lines used to split) reached the worker as **one** 2329-character
request: it timed out at 60.1 s, and because the reply had been a single chunk,
all of its audio was lost while its text stayed on screen. The same reply now
arrives as 20 requests of ~115 characters; the slowest took 6.4 s of the 60 s
budget. Nothing about the timeout was widened — the job was made to fit it.

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
- A reply the model hard-wrapped in the middle of a clause now takes a breath
  at that line break instead of flowing through it. The price of the bound is
  paid in prosody, never in lost audio.

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

The two ways a request goes unanswered are named as the two different things
they are: **"the resident speech worker stopped (exit code N)"** means the
process is gone, and **"the resident speech worker did not answer within 60
s"** means it is alive and behind. They used to share one sentence — *"took
too long or stopped answering"* — and that conflation is exactly why a
session that lost its audio could not be diagnosed from the report: one
message covering both causes explains neither.

A cancel retires the worker rather than reusing it, because an interrupted
worker may be mid-write on an artifact. It no longer leaves a flag for the
next unrelated sentence to trip over: a waiting request recognizes itself by
the process it wrote to, so a "stop speaking" that arrives a beat late, or a
cancelled narration phrase whose synthesis had already returned, silences
only what it was aimed at. (The flag was owned by nothing, and a proven
result was a spoken reply that died for a cancel it never received.)

An `ok` reply is a promise about a file, not proof of audible sound. Before
the artifact reaches the player, Stella opens it as a WAV and rejects the
same three silent-failure classes the microphone path already rejects on
input: unreadable, shorter than 10 ms of audio, and full-length but
below-audibility. Each becomes a `VoiceError`, and the D2 degradation rule
already handles it — text reply continues, no false "speaking" state, no
silent turn the user has to explain. A valid-but-unusual shape (stereo,
24-bit, compressed) is validated and passed through: the check exists to
catch a broken worker, not to police every WAV a future worker might
produce.

For the exact PCM s16 mono shape Kokoro actually produces, the artifact
additionally gets its leading and trailing silence trimmed (with a 25 ms
pad before the first loud sample and a 15 ms pad after the last) and its
surviving edges linearly faded (15 ms in, 10 ms out), so the first
syllable lands without the click a hard boundary makes. Rewriting is
skipped when the trim would leave less than 50 ms of anything — the
clicks a fade can save are cheaper than the click produced by trimming a
"hello" down to two samples. Measured on a real 3 s tone this costs
about 3 ms per synthesized chunk, paid on the same producer thread that
already blocks on `synthesize`; the pipeline's overlap does not change.

**You do not have to name that worker.** With the default
`STELLA_VOICE_SPEECH=auto` and no `STELLA_SPEECH_COMMAND`, Stella checks
whether `~/tools/stella-speak-server` exists and may run, and prefers it over
the `espeak-ng` fallback. The check is one `os.access`, so it starts nothing
and startup never waits for a model to load. The first time spoken replies are
switched on, the worker is warmed on a background thread, so the several-second
model load is paid while the user is doing something else rather than inside
the first answer — and a machine with speech left off never loads a speech
model at all. A warm-up that fails says nothing; the first real sentence
reports it.

`STELLA_SPEECH_LOCAL_VOICE` and `STELLA_SPEECH_LOCAL_SPEED` add `"voice"` and
`"speed"` keys to the request line. They are deliberately **additive**: a
worker that knows nothing about them — including the installed Kokoro one,
which speaks its own built-in `af_heart` at 1.0× — ignores the extra keys and
answers exactly as before. Making the choice effective is an edit to that
script, which lives outside this repository.

`STELLA_SPEECH_RESIDENT` still only matters when `STELLA_SPEECH_COMMAND` is set
— there is nothing to make resident otherwise. It is now also a
Settings checkbox (*Keep the voice worker warm between sentences*) saved in
`config.json` like every other choice the panel holds, and the environment
variable outranks that saved answer for a single launch without rewriting it
(the worker Stella finds at `~/tools` is already resident, which is why the
saved default is off).

Honest latency: with the resident Kokoro worker the first audio of a reply
lands roughly half a second to a second after the reply text is finished,
because the first sentence is synthesized before it is played. Overlapping that
synthesis with the tokens still arriving would need a streaming turn, which is
a core change rather than a voice one (see `ROADMAP.md`).

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
  style note to the final-answer prompt: the answer will be heard once and
  cannot be skimmed, so the result comes first and everything else is
  compressed into an overview — one or two short spoken sentences, three only
  when the answer genuinely has three parts, and none of the detail that only
  works on a screen. No lists, code, markdown or unreadable symbols. Voice
  input with speech output switched off stays
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
running at all, so there is still no always-on listening. While an approval
dialog is open it does not arm either: cancelling that turn from the voice
would be the one thing the dialog exists to prevent.

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

Environment variables (voice configuration is read from the environment. Four
of these — `STELLA_VOICE_BARGE_IN`, `STELLA_SPEECH_RESIDENT`,
`STELLA_VOICE_CAPTURE_GATE` and `STELLA_VAD_MODEL` — have a saved half in
Settings and are written to `config.json`, so a launch from a desktop session,
where no shell profile ever ran, still behaves the way the owner last said. The
variable outranks the saved answer for the launch that carries it and rewrites
nothing. Everything else — the capture target the ear watches and every model
name and command line the recogniser and the synthesiser run — stays
environment-only and never reaches that file):

- `STELLA_VOICE_BARGE_IN` — `auto` (default: arms only when
  `STELLA_BARGE_SOURCE` names a capture), `on` (arm on a chosen capture — and
  where none is chosen, on whatever `capture_target` resolves, which is a
  wired input whenever this machine has one: see *Which microphone the ear
  takes*) or `off` (never). Anything else is refused at configuration, naming
  the variable, rather than defaulted: the runtime reads any fourth word as
  `on`, and `on` with no chosen capture is the setting that made Stella
  interrupt herself. Settings shows the same three as
  *Listen for you talking over her*.
- `STELLA_VAD_MODEL` — path to the Silero VAD v6 ONNX file, which Settings
  holds as *Speech detector* and this variable outranks for one launch. Blank
  in either place means `~/models/silero/silero_vad.onnx`. The barge-in ear,
  the wake ear and the capture gate all load this one file, and they do not
  treat its absence alike: the two ears say so at startup, the gate simply does
  nothing.
- `STELLA_BARGE_SOURCE` — capture target name, e.g. `ec_mic`. Under `auto`
  this doubles as the enable switch; unset, the ear never arms. Under `on`,
  unset means resolved-by-default rather than unnamed.
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
The Settings panel can also be pointed at it directly: a loaded filter source is
listed in the *Microphone* row as the node it is named, and choosing it applies
to the ear and to push-to-talk alike. Stella never loads the module herself —
that changes the machine's live audio graph rather than her own configuration,
so it is the owner's command to run, and the row keeps working with the pair
absent. Choosing it is not a free win either: whichever sink the module was
bound to is the only playback it cancels, so music played through a different
output arrives at the microphone unchanged, and a capture named after a filter
that has since been unloaded falls back to the default source.
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

## The capture gate

One finished recording passes a speech detector on its way to the recogniser.
`barge_in.SpeechGate` scores the clip frame by frame with the same Silero model
the ears already load, finds the span from the first run of five voiced frames
to the last, and writes that span — with ten frames (~320 ms) kept on either
side — into `capture.gated.wav` beside the original in the recorder's own
temporary directory. The recogniser is then handed the gated file. What is
recorded, and what is deleted when the turn is over, follows exactly the rules
already written for a capture: nothing new is kept and raw audio still enters
nothing but the one command it is sent to.

Why the switch ships on is a measurement rather than a preference, taken on this
machine on 2026-10-11 with Stella's own capture argv while music played through
the owner's headset. Seven point nine eight seconds of room: 249 frames of 32 ms,
every one of them above the energy floor the ear uses (RMS 0.13 to 0.28), and
**not one frame at or over** any probability threshold — the most speech-like
frame in the clip scored 0.046. Loud is not speech. That is also why this trims
on a learned detector and not on silence: a rule that measures volume would have
kept the entire clip and changed nothing, which is the failure the owner was
reporting — a whisper-family decoder handed several seconds of music with no
words in it invents words.

The gate is a filter and never a decision, which is the doctrine that separates
it from the two ears above and below it:

- It cannot refuse a turn. No speech found anywhere means the recording is
  handed over byte-identical, and whether a transcript is junk stays where it
  already lived (`audio.is_transcription_junk`).
- It never cuts inside the span, and never trims a clip that has speech at both
  edges — the pad keeps the breath before the first word and after the last.
- Anything going wrong costs the turn nothing: a file that is not a 16 kHz mono
  s16le capture, a detector that raises, a directory it cannot write into, all
  return the original path. A gate that fails is a launch that transcribes what
  it always transcribed, not a voice input that stopped working.
- A missing model is silent here and loud next door. Barge-in and the wake ear
  report one friendly error at startup because the owner armed them; this is a
  filter on a path that must keep working as it did, so `build_capture_gate`
  returns `None` and says nothing.

That last asymmetry, and the fail-open rules above it, are deliberate rather
than cautious: no real-speech clip exists on this machine, so the false-negative
side of the detector — a voice the gate would not find — is unmeasured here. The
unit tests prove the arithmetic against a detector that scores frames in a given
order; they do not prove that this microphone renders the owner's voice as
speech. If it ever stops doing so, the fix is the Settings row
*Cut the room's noise off the edges of what you say*, not a code change.

Environment: `STELLA_VOICE_CAPTURE_GATE` (`0` says not for this launch, and never
rewrites the saved answer). Settings: the same checkbox, plus *Speech detector*
for the model path the gate reads.

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

The shared tap exists only when something listens while Stella is idle, which
today means the wake word. **With the ear switched off there is no tap, and
push-to-talk opens its own `pw-record` — which takes the same microphone the
*Microphone* row names** (`voice.SubprocessRecorder` is handed
`capture_target.live_capture_target(settings.wake_source)` and asks it at every
press). Before that, the unnamed capture was the recording path's permanent
answer: the row chose the ear's device, push-to-talk followed PipeWire's
remembered default source, and on a machine with a headset connected the two
are not the same microphone at all. The name is re-probed per press for the
reason `capture_target.py` gives — a resident server lives for hours and the
microphones do not — and a `--target` is added to `pw-record` only, because
`arecord` has no switch that takes a PipeWire node name and a machine whose
probe cannot answer must keep the capture it always had.

## What the always-open ear costs

With wake on, the shared melspectrogram and embedding models plus one
classifier per chosen phrase run over every frame the shared capture
delivers, in addition to the one `pw-record` child that
push-to-talk starts anyway. The work is continuous rather than bursty, and
it is the price of the opt-in: openWakeWord's own guidance puts a
classifier of this size at a small fraction of one laptop core, which is
why Stella keeps the ear armed for as long as the box is ticked instead of
duty-cycling it. That figure is upstream's, not a measurement taken here —
Stella has never timed her own idle cost on this machine. Barge-in is the
louder neighbour: it is armed only while Stella is speaking, and its
voice-activity model runs on that window alone.

Setup (all of it optional; nothing is downloaded or trained for you):

```bash
uv sync --extra wake                # adds onnxruntime only
mkdir -p ~/models/openwakeword && cd ~/models/openwakeword
# The two shared pipeline models every classifier needs:
for f in melspectrogram.onnx embedding_model.onnx; do
  curl -LO "https://huggingface.co/dscripka/openWakeWord/resolve/main/models/$f"
done
# Then at least one phrase classifier from the same release —
# e.g. hey_jarvis_v0.1.onnx or alexa_v0.1.onnx.
```

Every `.onnx` file placed there that is not one of the shared two is a
wake word Stella can hear: the Settings *Wake word* row lists them, or
chooses "everything detected on this machine" to arm all of them at once
(any one crossing the threshold is exactly one Listen press). A custom
phrase like "hey stella" is not a setting that downloads anything —
openWakeWord's own training step produces a classifier for it, and the
file it writes at `~/models/openwakeword/hey_stella.onnx` is what the
phrase field in Settings looks for. Until such a file exists, choosing a
custom phrase reports one friendly line at startup and every other voice
path keeps working. Environment: `STELLA_WAKE_MODELS` (comma-separated
list of classifier files; unset means detect everything in the
directory), `STELLA_WAKE_PHRASE` (display label),
`STELLA_WAKE_MODEL_DIR`, `STELLA_WAKE_SOURCE` and
`STELLA_WAKE_THRESHOLD`.

### Which microphone the ear takes

A capture that names no node takes PipeWire's *default source*, and on a
laptop with a Bluetooth headset that source is the headset's own microphone —
which costs the headset its audio quality for as long as the capture holds it
(see *Known limitations*). `stella/capture_target.py` decides what to name, in
three rules:

- **A Bluetooth input is never automatic while another input exists.** The
  first version of this rule stepped aside unless a Bluetooth microphone was
  already the highest-priority candidate, and that was the defect: on this
  laptop, with the headset connected and playing music in stereo and nothing
  recording, `pactl info` reported `Default Source: bluez_input…`. The node a
  capture would take was a headset microphone that no candidate list had been
  asked about, and the first unnamed capture is what makes PipeWire create it
  — BlueZ then drops the card to its hands-free profile, so the owner's music
  becomes mono call audio and Stella's replies become inaudible. Naming any
  wired input makes the remembered default irrelevant, which is the whole
  point: the failure was invisible at the moment the decision was made.
- **The ordering is WirePlumber's.** `priority.session` decides which of the
  non-Bluetooth inputs wins, so a microphone the owner plugs in deliberately
  can outrank the built-in instead of losing to a ranking hardcoded here.
- **Doubt means silence.** No `pw-dump` on PATH, a probe that stalls past one
  second, output that does not parse, no candidate at all, or a machine whose
  only input *is* a headset — all of it means "name no target", which is what
  the voice path always did with that answer. A probe must never become a
  reason voice stops working, so nothing in the module raises into a user's
  path, and an owner with no microphone but the headset keeps voice rather
  than getting a rule applied to her.

Discovery reads `Audio/Source` nodes and nothing else, which is what leaves out
playback streams, MIDI and video; a node whose name ends in `.monitor` is
dropped too, because a monitor is the loudspeaker's side of a device however it
is classed. Each survivor is classified from the device segment of its name —
`pci-` built-in, `usb-`, `bluez_` Bluetooth, anything else under `alsa_input.`
other — and a name outside `alsa_input.` is **`virtual`**: a filter's capture
rather than a piece of hardware. That is the shape an echo-cancelled source
arrives in, and it is listed on purpose: the name of that source is whatever the
owner told the module to call it (`ec_mic`, `echo-cancel_capture`, anything), so
no name pattern could recognise it, and a pair she set up by hand is a pair she
must be able to point Stella at. Being listed is not being chosen — see
*Which microphone the ear takes* and `pick_preferred_source`, which skips
`virtual` however high WirePlumber ranks it. A PipeWire node name is hardware
identity, not a secret: it is safe in a command line and in `config.json`.

The answer is **recomputed each time a capture opens**, not once per process.
A resident voice server lives for hours; the microphones do not. The tap, the
wake ear, its utterance watcher and the barge-in listener are all handed a
function that builds the command line (`barge_in.live_capture_command`) rather
than the command line itself, so the device that exists when the microphone is
picked up is the device that gets picked up. One probe is ~13 ms on this
machine, and it runs at an open — a session or an armed ear — never per frame.
If the named node has vanished in between, `pw-record` does not fail: measured
here, a target PipeWire cannot find falls back to the default source, which is
the old behaviour for exactly one capture rather than a dead ear.

The owner sees the whole decision. Settings has a **Microphone** row listing
*let Stella choose* plus every input found when the panel opens (so a
hot-plugged device appears on reopen even though the running ear already
decided), and under it one line saying what the choice costs: naming the
headset's own microphone, whether by hand or because it is the only input
here, says so; automatic says which microphone Stella took, and adds that it
left a Bluetooth headset alone only when a headset is actually here. A
deliberate choice is saved in `config.json` as `wake_source` and survives the
next launch. A node that was saved but is not connected now is still listed,
marked as such — an unplugged microphone must not silently rewrite what the
owner picked. `STELLA_WAKE_SOURCE` still outranks the saved value, and only for
the launch that carries it.

Both the always-open taps and the barge-in watcher resolve through the same
function, because VOICE.md's own observation was two live captures pinning the
device, and either one alone would do it.

## UI states

The status line distinguishes "Listening...", "Transcribing...",
the elapsed "Stella is working · N s" turn state, and "Speaking...", and
returns to idle when the utterance is finished.
"Listening..." appears only while the recorder is
actually running, and speech is only claimed after a transcript was produced.

The dot beside the voice controls answers a question the status line cannot:
*is the microphone open right now?* It is red while Stella holds the
microphone — an armed wake ear, a capture in flight, an interruption
listener waiting on her own voice — and dim when nothing does. The answer
comes from the parties that can know it: the shared capture says whether its
process is alive, and the wake and interruption sessions say when they hold
the device — so it goes dark the moment the microphone is released.

## Controls

- **Listen / Stop** — start or stop one explicit recording. Recording only
  happens inside an explicit press, or inside one wake-initiated capture
  when wake is on; there is no background recording either way.
- **Wake word (opt-in)** — with the *Wake word* box ticked in Settings (or
  `STELLA_WAKE_WORD=on`) a local detector holds the microphone open while
  Stella is idle, and one confirmed phrase is exactly a **Listen** press:
  it starts the same capture, records nothing
  until it fires, and gains no authority of its own. Off by default, and
  the tick is saved for later launches; the environment variable decides
  one launch in either direction without rewriting the saved answer.
  The *Wake word* row under that tick chooses which classifiers answer:
  the dropdown lists what was detected in `~/models/openwakeword` (see
  setup above), and a typed phrase arms the classifier file it names —
  after the owner has placed or trained it there, never by a download
  or a training run of Stella's own.
  Left on **"everything detected on this machine"**, the line under the row
  spells out every word that will wake her, because that default arms every
  classifier in the folder and a folder copied from openWakeWord ships
  `alexa_v0.1.onnx` and `hey_jarvis_v0.1.onnx`. A video or an advertisement
  saying one of those words is then a legitimate wake, and it looked like a
  malfunction until the row named the words it had been listening for. The
  default is still detect-all — it is what makes a folder holding the owner's
  own trained classifier work the moment the box is ticked — so the fix is that
  the choice is legible, never that a word list was added to the runtime.
  Apply arms or stops the ear in the running session, so there is no
  restart to wait for and never a second capture for the same microphone.
  The ear is suspended whenever Stella is speaking, narrating, already
  capturing or waiting on an on-screen approval — a dangerous action is
  answered by a press on that dialog and nothing else, so the ears stay off
  the microphone until it closes. A wake that fires over silence is
  discarded:
  the filler a transcriber invents for a quiet room ("Thank you.",
  `[Music]`) is recognised and reported
  (*Stella woke up but heard no words. Nothing was sent.*) instead of sent
  to the model. A **Listen** press is never second-guessed this way,
  because the transcript stays visible.
- **Cancel** — while listening, abort the current recording without
  transcribing it; while transcribing, abandon the transcription in flight.
- **Stop speaking** — end the current spoken reply (playing sentence and
  queued ones); the turn is unaffected.
- **Speak replies** — toggle speech output; it defaults to off.
- **Mute mic** — put every ear down for this session. Nothing is saved:
  the switch is there to be reached in one click the moment an open
  microphone feels wrong, and the next launch starts unmuted. Muting stops
  the wake ear, so its subscription leaves and the shared capture ends with
  it; a wake phrase that races the switch is refused out loud rather than
  recorded, and a **Listen** press while muted says so instead of going
  silent. A recording already started by hand is left alone — the switch
  answers for the ears that open themselves, not for a press already on
  screen. Speaking and work narration are untouched: this is a microphone
  switch, not a volume one.

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
  `auto` prefers a local command when `STELLA_TRANSCRIPTION_COMMAND` is set,
  then the `voxtype` CLI when it is installed, and otherwise uses OpenAI
  Whisper when an OpenAI key is available; otherwise voice input stays
  unavailable. Whichever transcriber is chosen is named on screen (`Voice
  input uses voxtype (whisper).`) once per session, never on every turn.
- `STELLA_VOICE_SPEECH` — `auto` (default), `openai`, or `off`. `auto` prefers
  `STELLA_SPEECH_COMMAND`, then a resident worker at
  `~/tools/stella-speak-server` when it may run, then local
  `espeak-ng`/`espeak`, then OpenAI TTS with an API key; otherwise speech
  output stays unavailable.
- `STELLA_TRANSCRIPTION_COMMAND` — a local command template that must contain
  `{input}` and prints the transcript on stdout (for example a `whisper.cpp`
  wrapper). Executed without a shell.
- `STELLA_TRANSCRIPTION_ENGINE` (default `whisper`) — the engine handed to the
  detected `voxtype` (`voxtype -q transcribe --engine <name> {input}`). It
  names an engine, not a model size; the model an engine uses stays in
  voxtype's own configuration. The `-q` is a *global* flag and goes before
  the subcommand, and it is not the whole story: even quiet, voxtype prints a
  block about the file it loaded, its format and its resampling on stdout,
  then a blank line, then the words. Stella reads the transcript out of that
  stdout instead of taking it whole, because the block is the tool
  describing its own work — heard verbatim, it would enter every spoken turn
  as things the user said. This applies only to the built-in tool: a command
  the owner writes is documented as printing the transcript on stdout, so its
  bytes are the transcript and nothing else, blank lines included.
- `STELLA_TRANSCRIPTION_TIMEOUT` (default `30` seconds, max `600`) — how long a
  cloud transcription request may take before Stella abandons it. A local
  command keeps its own 120-second ceiling.
- `STELLA_SPEECH_COMMAND` — a local command template containing `{text}` and
  `{output}` that writes one audio file (for example a `piper` wrapper).
- `STELLA_SPEECH_RESIDENT` — `off` (default) or `on`; only meaningful with
  `STELLA_SPEECH_COMMAND` (see "Resident synthesis worker").
- `STELLA_SPEECH_LOCAL_VOICE`, `STELLA_SPEECH_LOCAL_SPEED` — the voice name and
  rate asked of a resident worker. Sent as extra request keys a worker may
  ignore, and never chosen for you: unset means the worker speaks its own
  default. The speed is a rate between 0 and 4.
- `STELLA_TRANSCRIPTION_MODEL` (default `whisper-1`), `STELLA_SPEECH_MODEL`
  (default `tts-1`), `STELLA_SPEECH_VOICE` (default `alloy`) — OpenAI
  identifiers when the cloud path is selected.

Recording uses `pw-record` or `arecord` when installed, pinned to the 16 kHz
mono the local transcribers expect; playback uses `pw-play`, `paplay`, or
`aplay`. Synthesis prefers a resident worker, and falls back to
`espeak-ng`/`espeak`.

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
- A recording exists only between an explicit Listen press (or one
  wake-initiated capture) and its stop, and no voice path ever acts on its own.
  An open microphone is a different thing from a recording: the opt-in wake ear
  and barge-in each feed only local classifiers — a wake candidate and a
  voiced/not-voiced decision — that are never written to disk or stored, and
  audio reaches a recorder only after a phrase was confirmed. **Mute mic** takes
  even the open handle away for the rest of the session.

## Security posture

A transcript has exactly the authority of typed user input — which is to say
none of its own. It cannot approve a tool: `DANGEROUS` actions still raise the
same approval request through `ApprovalBroker`, and spoken words such as
"approve it now" are ordinary untrusted content that can never construct a
`ToolApproval`. Filesystem and network verification receipts and memory
tool boundaries behave identically for spoken and typed requests.
Playback output is an interface rendering; nothing it says feeds back into the
decision path.

## Validation

The normal suite never opens a microphone or a speaker: fakes stand in for
the recorder and player, and the command providers are exercised only against
this interpreter. One file crosses that line, deliberately and by request.

`tests/test_voice_roundtrip.py` is gated on `STELLA_VOICE_ROUNDTRIP=on` and
skipped, never silently passed, without it. It asks the synthesis engine this
machine would itself pick to speak fixed sentences and reads the audio back
through the detected transcriber — both sides built by the same functions a
launched Stella uses — and asserts every word returns, in order. It never
touches the microphone: the synthesized file is handed over as the recording
a recorder "happened to stop on".

That harness is also the answer to the engine question `MODELS.md` parks
(bigger whisper engines, parakeet): it measures whichever pair is installed,
and one lost word is a measurement. Measured 2026-10-03: three sentences
through the resident Kokoro worker and voxtype's whisper engine came back
whole and in order — and the run found a real defect no fake could see, the
voxtype progress block described under "Providers and configuration", which
was polluting every live transcript until it was read out of stdout.

The microphone choice was checked on this machine, read-only, before and after.
`pw-dump` here reports `alsa_input.pci-0000_00_1f.3.analog-stereo` at
`priority.session` 2009 and, when a WH-CH720N is connected,
`bluez_input.00:A4:1C:90:B9:B0` at 2010 — the headset outranks the built-in in
WirePlumber's own ranking, which is why it is the default source. With the
headset playing music in stereo and nothing recording, `pactl info` still
reported `Default Source: bluez_input…`, and that is the state the rule has to
survive: an unnamed capture here is a headset microphone request, not a
built-in one. The current build therefore names the built-in —
`pw-record -a --rate 16000 --channels 1 --target
alsa_input.pci-0000_00_1f.3.analog-stereo -` — and a one-second run of the real
tap proved both halves of that: frames arrived, the source-output's
`target.object` was the built-in node (`Source: 59`), and the card stayed on
`a2dp-sink` with stereo playback links intact for the whole time.
`pw-record --target` was then proven to accept a *node name* rather than only a
serial: one bounded second of capture against the named built-in input delivered
4000 bytes, and a deliberately unknown name delivered the same, falling back to
the default silently — the reason an absent microphone reads as silence rather
than as an error, and why the saved value is a name the panel showed rather than
anything hand-typed. The Bluetooth-only branch, which is the one this laptop
cannot offer on command, is proven against recorded `pw-dump` text in
`tests/test_capture_target.py`.

## Known limitations

- One utterance per Listen press — or per wake phrase, where that is on —
  with no streaming recognition, no speaker identification and no emotion
  detection. Stella hears *a* voice, never *whose* voice.
- Wake detection is a local classifier: it has no idea who is talking, so
  anyone in the room who says the phrase opens a capture, and any phrase it
  mistakes for the word can too. That is the whole reason the opt-in is a
  box the owner ticks and a dot on screen, and why **Mute mic** is one click
  away.
- Local transcription quality depends entirely on the configured command; Stella
  performs no speech modeling of its own.
- `espeak` output is a fixed robotic voice and follows the text's language as
  best it can; there is no voice selection beyond `STELLA_SPEECH_VOICE` on the
  OpenAI path.
- Playback is one subprocess per chunk and strictly sequential; a chunked
  reply keeps at most a few synthesized sentences ahead of the speakers.
  Synthesis is one process per sentence unless the resident worker is enabled.
  There is still no mixing, ducking, or overlap between artifacts.
- One unbroken token longer than the chunk bound — a base64 blob, one
  enormous URL — is still a single request, and can still overrun the 60 s
  budget. Stella will not halve a word to meet a deadline, so that reply
  loses its audio and now says *"did not answer within 60 s"* rather than
  implying the worker died. It is a rare shape in prose a person dictated.
- The installed worker answers any failure inside a request with a reply
  naming that request's id — except a failure while *reading* the line, where
  the id is unknown and its own handler raises again, ending the process.
  That script lives outside this repository and Stella does not edit it; what
  Stella does now is report the death with its exit code instead of hiding it
  behind a timeout's words.
- Voice mode is desktop-UI only; the CLI remains text-only.
- A Bluetooth headset's microphone is also a mode switch, and any capture that
  holds it flips it. While *any* capture holds that device, PipeWire must keep
  the headset in its hands-free profile, and playback through the same
  headphones follows: mono, low-bitrate, volume ceiling quietly dropped — a
  phone-call sound where music used to be. Observed directly on 2026-10-06:
  two live taps — a resident voice server and a session UI, both
  `pw-record -a --rate 16000 --channels 1 -` — held a WH-CH720N at
  `headset-head-unit`; ending them returned the device to `a2dp-sink`, stereo
  and full volume, by itself, with no profile touched by hand. The ear no
  longer asks for that microphone by accident (*Which microphone the ear
  takes* above), and the Settings row says out loud when the ear is going to
  hold one anyway. What remains is the machine with a headset and no other
  input: the ear arms on the headset's microphone, because the alternative is
  no voice at all, and the panel says what that costs.
- A microphone named deliberately, or cached by the automatic pick at startup,
  is not re-decided mid-session. Unplug it while Stella is running and the ear
  keeps listening on a node that is gone — silence, not an error — and plug one
  in and nothing changes until the next launch. Reopening Settings shows the
  new device; restarting Stella listens to it.
- The Bluetooth branch of the pick is proven against recorded `pw-dump` text,
  not against a headset connected to this laptop: the case that motivated the
  change has not been re-run on the owner's own WH-CH720N since. What was
  checked here is the other half — that with no headset connected the built
  input is the only candidate, the pick is empty, and the capture command is
  byte-for-byte the one Stella ran before.
- The capture commands are PipeWire and ALSA, so there is no recording path on
  Windows at all. What that platform does lose is the polite stop: it cannot
  interrupt a child process, so Stella asks it to exit (`terminate`) and the
  recorder therefore never gets to finalize its WAV. The capture is then
  reported as *no recording* rather than as a usable file — fail-closed, not
  silent.
