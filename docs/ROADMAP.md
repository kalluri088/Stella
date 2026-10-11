# Stella Roadmap

This is the whole-project plan, organised around the six commitments of
`docs/STELLA_VISION.md` rather than around the stages the work happened to
land in. The question this file answers is **"how close is she to feeling
right"**, not "how many capabilities shipped": for each commitment it states
what enforces it in code today, which questions measurement has already
closed, and what is still not true. Design rationale lives in the dedicated
documents linked below; this file only orders the work and names what enforces
each rule in code.

Each commitment is written in three parts — **what enforces it now**,
**measured, and closed**, and **what is still not true**. A commitment with
nothing to say in one of those parts says so rather than padding it.

Item ids (`A9`, `B0`, `D4` …) are unchanged, because documents, tests and
commit messages cite them; only their address moved. **Where the stages went**
near the end keeps each stage's own framing verbatim and maps Stage A–E onto
this file, which is also what the release history needs: 1.1.0 is Stage A,
1.2.0 is Stages B and C.

## The gate on new work

Standing decision, recorded here at the owner's instruction (2026-10-11,
#239): **no new capability lands until the five sentences of the Vision's
*proof* are true of the installed Stella, on the owner's machine, in an
ordinary minute.** They are repeated below with the commitment each tests and
the best evidence this file holds, because a failing sentence is a defect to
fix and outranks every other item in this document.

1. *ask something with one command, and she starts answering quickly enough to
   feel like conversation* — **commitment 1. Not true yet.** A warm local
   model answers a trivial turn in 10–68 s (#244's measurement of the 16.8 s
   call, `docs/MODELS.md`); the part that does work is that the first
   sentence of speech now lands after ~1–2 s of rendering (A9, D2).
2. *ask her to do something that needs several steps, and she finishes it or
   says exactly where she stopped* — **commitment 3. Half true.** The budget
   keeps the loop working and the stop names what ran (#231); the stop cannot
   say what *remains*, because the runtime holds no task model.
3. *tell her something true about yourself once, and see her use it weeks
   later without being reminded* — **commitment 4. True once, under
   conditions** (B8): one restart in one sitting, needing the semantic-recall
   opt-in and a request that still fits its context.
4. *disagree with her, and watch her hold her ground when she is right* —
   **commitment 2. Untested.** The persona subsystem gives her something to
   hold (C1–C6) and #227/#244 stop her agreeing with a result she did not
   get; no measurement here asks her to hold a position against the owner.
5. *and have her ask, unprompted, whether you are okay on a day you do not
   sound like yourself* — **commitment 5. Not built.** The delivery path
   exists (A1, B5); the sense of how you usually write does not.

Until sentences 1, 4 and 5 have a mechanism that passes, work that adds a
capability instead of closing one of these five is out of order, whatever
else it delivers.

## Standing principles

Each principle is a property of the running system, enforced by a named
mechanism — not an aspiration.

1. **The model proposes; the app decides.** The LLM emits `Decision`s;
   only the application-owned `ToolDispatcher` executes anything, and
   only for capabilities registered by trusted code
   (`src/stella/tools.py`, `src/stella/brain.py`; see
   `docs/TOOL_EXECUTION.md`).
2. **Memory is selective and user-controlled.** Nothing is stored without
   an approval whose `ApprovalRequest` matches the exact content, and
   tool outputs report honestly (`src/stella/memory.py`;
   `docs/OUTCOME_MEMORY.md`).
3. **Awareness is not authority.** Proactivity runs on a notify-only
   runtime path that never consults the Brain, the LLM or the
   dispatcher, and anything an event or a scheduled item carries is
   treated as untrusted data
   (`src/stella/proactivity.py`; `docs/REMINDERS.md`).
4. **Trusted execution with verification.** The full chain is
   LLM → Decision → ToolDispatcher → risk classification → exact
   single-use approval → execute → independent verification →
   `ActionReceipt`; a success is reported only when re-reading the real
   world confirms it (`src/stella/tools.py`, `src/stella/stella.py`;
   `docs/APPROVAL_BOUNDARY.md`).
5. **Honesty.** No fabricated success: denials, failures and
   inconclusive outcomes reach the user and the model as such
   (`outcome_status` in `src/stella/app.py`), and previews say
   "too large to preview" rather than showing a misleading diff.
6. **Provider independence.** Ollama and any OpenAI-compatible endpoint
   sit behind one `LLMClient` interface; no feature depends on a
   particular model's quirks (`src/stella/ollama_client.py`,
   `src/stella/llm.py`).

## 1. Fast enough to feel like conversation

*The test:* one ordinary question, six times, from a cold start — if any
answer takes more than a few seconds to **start**, this commitment is broken,
whatever else works. The Vision states the price in the same breath: "close
to 0 latency" and a hosted model reached over the internet are mutually
exclusive, so a provider choice is a latency decision, not a preference. That
is why every brain, endpoint and speech decision in Stella's history is filed
here. The measured model choices themselves are registered in
`docs/MODELS.md` — that file, not this one, is the authority on what runs
where, and a swap there requires its stated evidence bar.

### What enforces it now

- **A10. Stella owns its llama.cpp brain — done.** The systemd-topology
  research found the real gap: the measured brain launch line had no
  supervisor. `stella/llama_server.py` now spawns `llama-server` as a
  Stella child — the report 09 Round C flags (with the one honest
  amendment that `-c` is shared across slots, so 16384 keeps every
  slot at the ~5.3 k tokens Stella's real prompt needs), waits on
  `/health`, refuses to bind a port someone else already answers on,
  and stops with the SIGINT → terminate → kill ladder; closing the
  application closes the brain (live smoke: ready in 4–19 s, 2.86 GB
  VRAM, correct turn, child gone after close). Provider `llama` is
  selectable everywhere (`STELLA_LLM_PROVIDER=llama`, Settings, saved
  config) and the model is the full GGUF path; the client reuses the
  proven Ollama compatibility path against `/v1`. Applying llama
  settings frees the shared port before the replacement binds it,
  while the build-first safety net (a bad config cannot destroy the
  working session) stands for every other change.
- **B4. Provider conformance suite — done.** One written contract
  (`CONFORMANCE_CASES` in `src/stella/conformance.py`: 16 named
  obligations) replayed twice. Offline: a loopback simulated provider
  answers in each dialect's real wire shapes (clean/fenced/prose
  decision JSON, stringified and malformed tool arguments, the
  reserved `tool_final` marker, invented capabilities, REQUIRED-guard
  overrides, empty replies, HTTP 500s) while the actual client classes
  and `LLMBrain` decide — 61 case×dialect checks across OpenAI-compat
  (Responses API), Ollama compat, Ollama native and llama-server
  compat, deterministic and GPU-free (`tests/test_conformance.py`),
  plus a prompt-fit tripwire asserting the decision prompt keeps
  fitting `num_ctx=8192`. The provider-keys work (2026-09-29) added a
  fifth dialect — `openai-chat-compat`, the same `OpenAILLMClient`
  asked to use Chat Completions function tools, which is how every
  hosted preset other than OpenAI itself talks — and replayed the
  whole matrix on it (76 case×dialect checks now). Live:
  `~/tools/bench_provider_conformance.py` re-asks the endpoint-only
  obligations against the shipping model — measured 2026-09-26 on
  `qwen3:4b`: honest HTTP 404 for an unknown model, the full decision
  prompt evaluated (`prompt_eval_count=3442` vs the size estimate,
  the report-15 question made checkable), and a clean native
  `datetime` tool call over compat. B0's routing-question calibration
  (gate 3) reuses this case-table shape on the live half.
- **A9. Chunked speech synthesis — done.** Whole-file speech made the
  user wait out every character of a reply before hearing any: a
  multi-sentence answer cost one full synthesis (measured ~10.7 s
  first-audio on local Kokoro). Now `sentence_chunks`
  (`stella/audio_output.py`, stdlib `re`, abbreviation/decimal guards)
  splits the final response, `_speak_chunks` synthesizes sentence by
  sentence on the worker while a consumer thread plays and disposes
  each artifact — first audio lands after one sentence of rendering
  (~1–2 s), because synthesis is faster than real time. **Stop
  speaking** ends the whole spoken reply (playing + queued), a cancel
  drains and discards the queue, mid-reply synthesis failures speak
  what they have plus one honest error line, and single-sentence
  replies take the byte-identical legacy path. Token-level streaming
  and barge-in stay out of scope.
  **A9b — every request fits its budget (2026-10-10).** A session lost a
  whole reply's audio to `Resident speech took too long or stopped
  answering`: a 20-bullet answer has no punctuation and used to split only
  on *blank* lines, so it reached the resident worker as one 2329-character
  request, synthesis costs ~0.03 s per character, and the 60 s budget killed
  a healthy worker at 60.1 s. `sentence_chunks` now breaks on every line and
  bounds any remaining chunk (`MAX_SPEECH_CHUNK_CHARS`, 600) at a word; the
  timeout did not move — the job was made to fit it. Death and slowness are
  reported separately ("stopped (exit code N)" vs "did not answer within
  60 s"), and a cancel is recognized by the request whose process it retired
  instead of by a flag with no owner that the next unrelated sentence used to
  trip over.
- **D2. TTS lookahead queue.** The walkthrough's biggest conversational
  win was synthesizing the *next* sentence while the current one
  plays (a queue of finished sentences, depth ~2), so the speaker
  never waits on synthesis mid-reply. Stella already starts speech
  while the reply is still being written; the open question is
  whether our per-sentence synthesis is sequential against playback.
  Measure first; if it is, add a bounded lookahead. The queue may
  hold final response text only — never decisions — and the existing
  Cancel path must flush it like it flushes playback today.
  *Measured first, as promised (research report 24): the queue
  already existed and synthesis already overlaps playback — the real
  per-sentence cost is a >3 s fixed process+model-load floor (idle
  A/B median 3.65 s vs 1.80 s steady inference).* D2 shipped as
  `ResidentSpeechProvider` (`STELLA_SPEECH_RESIDENT=on`,
  environment-only when it landed, and a saved Settings checkbox since): one
  line-JSON worker per session, retired and
  restarted on death or timeout, degrading to no speech with the
  text reply untouched. The Cancel path is unchanged — it kills the
  in-flight worker call and the queue drains as before.
- **#228. The clock stopped costing a second every minute — done.**
  `LLMBrain._system_prompt` no longer embeds the local date and time; that
  reference now travels in `_context_payload`, which already carries the
  user's words, the history and the retrieved memories. The rules-and-tools
  prefix is therefore byte-stable between turns and a provider can reuse its
  KV cache across it. A minute-granular timestamp sat at the end of a
  ~6,900-token system prompt with Ollama rendering the `tools` array after it,
  so each new minute invalidated the cached prefix from that point and forced
  ~2,300 tokens to be re-read: measured 1.03 s for a turn whose only
  difference from the last was the minute, against 0.11 s after the move (cold
  full prompt 3.05 s, warm 0.07 s). What the model is told did not change —
  the same sentence, the same ISO-8601 minute precision, still reference data
  and never authority, and `datetime` is still the only way she answers "what
  time is it".
- **A6. Per-turn duration UX — done.** Every finished turn is timed by
  the session (`TurnOutcome.duration_seconds`) and the transcript
  renders it ("Stella: … (took 47 s)"), so slow answers read as
  "local model", not "broken". The duration is display-only; the
  stored conversation never carries it.
- **A5. Working feedback and cancel — done.** The desktop status line
  counts elapsed seconds while a turn runs, and a Cancel button stops
  the turn at its next safe point: cancellation is cooperative and
  checked between steps (before a Brain decision, immediately
  after one returns, and right after a dispatched step lands), so an
  approval prompt or an executing action is never interrupted
  mid-way. (As shipped, A5 could not reach an in-flight provider
  request either; A7 below closes that gap.) A cancelled turn is
  discarded whole from the conversation; a cancel with an approval
  still open denies it fail-closed (`Stella.process(should_cancel=...)`
  and `StellaBridge.cancel_current_turn` in `src/stella/app.py`,
  `docs/UI.md`). The CLI keeps Ctrl+C as its immediate-stop
  equivalent by design.
- **A7. Cancel interrupts in-flight provider requests — done.** The
  safe-point model of A5 meant cancelling during a blocking model
  call waited out the whole request (minutes on a slow local turn).
  Now the desktop Cancel also aborts the request itself: the native
  Ollama path runs its HTTP wait on the worker thread under a short
  socket-timeout poll (`_await_native_response` in
  `src/stella/ollama_client.py`), so the wait is abandoned within
  about a second and the connection closed; the OpenAI-compatible
  paths wrap the blocking SDK call in `run_cancellable`
  (`src/stella/llm.py`) and give up on it, discarding any late
  reply. `should_cancel` is offered to `Brain.decide` and the
  synthesis `chat` calls — conditionally, so 1-argument clients are
  unaffected — and a turn without a cancel behaves exactly as
  before (the CLI never passes one). Cancelled turns still surface
  through the existing checkpoint shapes: an abandoned decision
  records nothing, an abandoned synthesis keeps whatever effects
  already landed, and the whole turn is discarded from the
  conversation. Honest limits: server-side generation stops on
  disconnect best-effort only (`stream: True` would be the
  guaranteed fix, deliberately not taken on), an executing action
  still lands first, and voice transcription remains a
  non-cancellable blocking segment (resolved by A8 below).
- **A8. Voice turns are cancellable end-to-end — done.** A7 stopped
  at the conversation; the voice periphery around it is now covered
  too. The pre-turn window ("Transcribing...") is abandonable:
  `VoicePanel.stop_and_transcribe` and `synthesize` wrap their work
  in `run_cancellable`, and the command providers expose a
  `cancel()` handle (SIGINT → terminate → kill on the tracked
  `Popen`, like the player's) that the abort path uses, so a local
  whisper/piper command dies within about a second instead of
  running to its 120 s timeout. `cancel_current_turn` also stops
  playback, so Cancel means silence in a poll tick; speech that
  finishes around a cancel is discarded, never played. The
  lost-cancel race is fixed by clearing the flag at submission
  instead of at turn start, so a cancel raised during transcription
  survives into the turn and ends it cancelled. The mic **Cancel**
  button carries the new affordance. Honest limits kept: tools and
  approvals stay atomic-by-design; an abandoned cloud
  transcription/speech request is given up on without a socket
  close (no per-request cancel exists); Tier-2 barge-in (continuous
  mic + VAD) remains out of scope.
- **D3. Voice narration of tool work, application-owned.** His
  "quieter and worse" problem: while the agent reads a tool result,
  the user hears nothing and can't tell working from stuck. Stella's
  version: the runtime picks a phrase from a fixed pre-authored set
  per trace-activity kind the moment that activity starts, and a
  voice-context style note asks for brief replies. No model-authored
  narration text, no effect on approvals, risk or the audit record —
  spoken filler is presentation of events the app already knows.
  *Shipped 2026-09-27:* `Stella.process` gained a presentation-only
  `on_activity` observer (`"thinking"` / `"working"` / `"answering"`,
  exceptions swallowed, consulted by nothing that decides anything) and
  a fixed voice-style note that joins synthesis only when the input
  envelope carries audio modality. The bridge narrates from
  `NARRATION_PHRASES` on a single non-stacking slot, off the worker
  thread, only for true spoken turns (voice input *and* speech output
  on); reply speech, **Cancel** and **Stop speaking** all retire an
  unheard phrase. See `docs/VOICE.md`, "Spoken conversation turns".
- **#233. The window says what she is doing, not only for how long —
  done.** The status label renders the activity phases the runtime already
  emits: `thinking` becomes "Stella is thinking about that", `working` becomes
  "Stella is working on that", `answering` becomes "Stella is composing the
  answer"; `calling:<capability>` keeps its older, more specific rendering and
  a kind with no phrase leaves the previous line standing. The CLI had this,
  voice had `NARRATION_PHRASES`, and the desktop window — the surface the
  owner actually uses — showed a stopwatch, because "Stella is working · 14 s"
  is a progress bar wearing her name. The window map is deliberately new
  rather than shared with the voice one: that map has no `answering` entry on
  purpose, since the reply itself is that phase's speech. Neither map ever
  renders model output.
- **D4. Barge-in unblock with external endpointing data (extends
  report 19, which B7's live blocker defers to).** Two observations
  from the walkthrough map onto the parked ear's tuning: a deliberate
  ~1 s trailing-silence wait before answering is kept on purpose
  (shorter interrupts the user's thinking pauses), and hesitations
  ("um") count as speech — speech-state hysteresis, not a raw
  per-frame floor. Resume order stays as report 19 records it: first
  the playback-stretch blocker itself (xrun/pw-top instrumentation
  during a live reply, possibly delayed ear arming, then a proper
  re-measure), only then endpointing tuning. Echo cancellation as a
  prerequisite is independently re-confirmed by his setup.
  *Measured 2026-09-27→28 (research reports 28–29, four instrumented
  live sessions, `~/tools/d4_analyze.py` over per-play ratio +
  pw-top + journal evidence): the stretch did NOT reproduce — full
  plays up to 7.0 s ran at 1.01–1.02× with the ear armed, zero xruns,
  empty `pw-play` stderr — so delayed arming and the resident-worker
  control were never needed, and report 19's blocker closes as
  cannot-reproduce-under-instrumentation. Human acceptance PASSED:
  Round A 3/3 ≤500 ms (383/128/127 ms onset→cancel), Round B zero
  false fires with echo residual measured at `prob` 0.60 peak.
  Endpointing tuning: a hesitant "um" human sample plus TTS dip
  analysis showed the 5-frame streak never breaks on real speech
  (in-speech dips ≤2 frames; hard-reset/leaky/hysteresis fire
  identically) — no judge change, per the no-demonstrated-weakness
  rule. Consequence: `STELLA_VOICE_BARGE_IN` default flipped from
  `off` to `auto` — the ear arms when `STELLA_BARGE_SOURCE` names
  the (echo-cancelled) capture, never on an undeclared raw mic,
  because uncancelled playback was measured to register as speech
  (23% voiced frames). The raw-mic trap is why plain `on` is not
  the default.*
- **B7. Voice barge-in — built; the live double-talk acceptance remains.**
  Compendium item 7 (report 08) measured the two prerequisites on this
  box and they pass: PipeWire's WebRTC `module-echo-cancel` cancels
  Stella's own playback to the noise floor (−29.6 dB, 0 % false-VAD
  frames during playback vs 22.8 % uncancelled), and silero-vad costs
  0.30 ms per 32 ms frame over ONNX (RTF 0.01), so a continuous
  listener is ~1 thread-percent. The Tier-1 half of ship order (a
  voice-reachable Cancel that flushes playback) already landed as A8.
  With all three gates explicitly approved, the ear itself shipped:
  `src/stella/barge_in.py` runs a raw 16 kHz `pw-record`/`arecord`
  capture through the Silero v6 ONNX model and fires only after five
  consecutive frames clear both a probability and an energy floor —
  and its entire authority is one call to the existing
  `cancel_current_turn()`, so an interrupt is exactly a Cancel-button
  press. It is opt-in via `STELLA_VOICE_BARGE_IN` (off by default;
  voice settings never enter `config.json`), uses the new `barge-in`
  extra (`onnxruntime` only), and degrades like every other voice
  part: a broken ear disables the ear and nothing else. Offline tests
  cover the judge, the subprocess listener, settings and the bridge
  lifecycle (20 cases, no microphone needed). **Live testing found a
  real blocker:** with the ear on, the first sentences of each spoken
  reply play stretched and glitched (a 7.5 s file measured 45.8 s and
  76.6 s in the UI trace) even though the detector never fired, and
  the identical UI with the ear off is perfect — the live capture
  stream interferes with playback on this loaded machine. The stretch
  does not reproduce in any isolated harness (five controlled
  reproductions, including the full producer/consumer/synthesis/
  per-episode-ear shape), so the mechanism is open (leading
  hypothesis: PipeWire real-time starvation with brain + TTS +
  capture all live at once). The human double-talk acceptance is
  therefore NOT taken; barge-in stays off by default and parked
  by user decision — full evidence and next steps in research
  report 19. External endpointing data that may inform the tuning
  half of that resume order arrived 2026-09-27 (Stage D, D4).
  *Unparked and accepted 2026-09-28: see D4's measured outcome
  below — the stretch did not reproduce under instrumentation and
  the double-talk acceptance passed.*

### Measured, and closed

Each of these is a verdict with numbers attached, not a deferred idea.
Re-open one only with the evidence its own text names — that is what keeps
someone from paying for the same measurement twice.

- **D1. Prefill bench for the llama.cpp brain line.** Reports 15/20
  showed decision turns are prefill-dominated, and our ngram-mod
  draft acceptance measures only ~0.44–0.45 on real decisions
  (report 09's synthetic +20 % did not transfer). The external
  walkthrough claimed a micro-batch raise (128 → 1024) took page-read
  prefill from ~200 to ~800 tok/s, and that MTP speculation made tool
  calls markedly faster — both knobs `BRAIN_LAUNCH_ARGS` has never
  tuned. Bench outside the repo (`~/tools/`, temp ports, never the
  live 8080 or 8093): shipped line vs `-ub 1024` vs partial expert
  offload (replacing all-or-nothing `-cmoe`) vs an MTP-capable
  variant, measuring prompt-eval t/s, VRAM and real-corpus decision
  wall time. Adopt only on measured prefill gain with no VRAM
  regression and no strict-pass regression under the standing
  ≥6-pass A/B rule.
  *Measured 2026-09-27 (research report 25,
  `~/tools/bench_prefill_flags.py`, six arms): the shipped line
  stays.* `-ub` genuinely doubles-to-triples *fresh* prefill
  (316 → 523 → 754 t/s on the 5.3k-token decision prompt) but the
  server's prompt cache means a live session re-prefills ~60–800
  tokens per turn, so the gain pays once per brain start and the
  real-corpus medians moved only within noise; `-ub 2048` also
  costs +264 MiB VRAM. `-ncmoe 44` recovered no VRAM and no speed,
  and `--spec-type draft-mtp` cannot load gpt-oss weights at all.
  Every modified arm drew more of the known peg-500 flake than
  base (0/12), which alone fails the quality gate's premise.
- **D5. First-response thinking-off experiment (gated).** His trick:
  reasoning is off for the first spoken reply — nobody should wait
  on a thinking phase to hear "hi" — and stays on for tool work.
  Our own bake-off says quality comes *from* the thinking channel
  (report 20), so this is an experiment, not a plan: `think=false`
  scoped to first response turns, measured on the full decision
  corpus with ≥6 passes per arm; adopt only if strict passes hold.
  *Measured 2026-09-27 (research report 26,
  `~/tools/bench_think_off.py`, global arms because scoping to
  first-response turns was the hypothesis under test): REJECTED.
  think-off scored 23/30 strict vs think-on's 22/30 — inside the
  flake band, not a win — and the claimed latency benefit inverted:
  arm median 24.8 s vs 21.6 s, with the plain fast-path cases the
  trick targets 2–4× *slower* off (`route-read` 11.3 → 47.7 s). A
  scoped variant has nothing to capture; the shipped line is
  untouched and this parked idea is definitively answered.*
- **B0. System-1 decision router (Laya) — resolved by measurement as
  a non-build (report 18).** The sketch stays written down because a
  future act classifier could reopen it. A small non-autoregressive
  classifier (Laya 0.3.20, 421M) consulted *before* the language-model
  Brain, so cheap
  always-on awareness informs (never replaces) the expensive
  decision.
  - *Slot.* One advisory call at a new safe point in
    `Stella.process`, immediately before `self.brain.decide`:
    `SystemOneRouter.route(state) -> RoutingHint | None`. A hint
    (act probability, per-tool relevance scores) may only
    pre-shape what the Brain sees — context emphasis, tool-list
    ordering — and can never dispatch a tool, approve anything,
    or substitute for the `Decision`. Any error, timeout, absent
    router or untrustworthy calibration falls back to the exact
    current path (fail-closed); router process death disables
    routing only, following the voice-degradation precedent. The
    model-proposes/app-decides trust model is untouched.
  - *Runtime facts (benchmarked on the real rig;
    `~/STELLA-BENCHMARK-REPORT.md` Part 3).* 31 ms per event on
    GPU, ~1.6 s on CPU and 4–5 s under load, because the RL-agent
    config runs the ModernBERT-large encoder up to 6 prefix
    forwards per event — so the router is only viable as a
    GPU-resident sidecar (~2.5 GB VRAM of a 6 GB card) and
    worthless off-GPU. It cannot co-reside with a full-GPU
    FreeToken server (that one's floor is ~2.9 GB fixed); the
    validated coexistence stack is llama.cpp `-cmoe` for the
    brain (2.8 GB GPU, experts in RAM) with Laya on GPU, holding
    ~41 ms sustained under full-stack load. No custom serving
    layer: a `pip install laya` process holds the model.
  - *Interface.* `agent.predict(state, {question: {type:
    choice|score|noul, instructions, criteria}})` returns
    per-question answers with calibrated confidence and
    `act_probability` — Stella authors its own routing questions
    rather than inheriting fixed act/route heads.
  - *Gated behind prerequisite experiments (measure before
    building).* (1) Brain-swap baseline: gpt-oss-20b with
    `reasoning_effort: low` — part of the "hello takes 300 s"
    pain is a qwen3:4b thinking-mode artifact, and A7 already
    bounds the wait; see how much shrinks before a router earns
    its VRAM.
    *Measured 2026-09-25 (research report 15,
    `~/tools/bench_brain_decisions.py`): the LFM2.5-8B swap is
    rejected on quality — 12/30 strict vs gpt-oss 28/30 on
    Stella's real decision path, with 12/60 silences and
    `hinglish 0/6` — and the real-turn speed gap is ~3×
    (median 10.1 s vs 29.4 s), not the 17× decode ratio, because
    decisions are prefill-dominated. The same bench caught a
    live incumbent bug: at `num_ctx=4096` Ollama evaluated only
    2050 of the ~5300-token decision prompt for every turn,
    silently truncating the head of the system prompt; fixed to
    8192 with a wiring test, and the post-fix incumbent arm scores
    29/30 strict (was 21/30) at a 10.0 s median — matching or
    beating gpt-oss on this corpus at ~3× the decision speed.*
  (2) Re-validate VRAM coexistence with whatever
    ships as the brain config. (3) Routing-question conformance
    and calibration on Stella-shaped inputs — ties directly into
    the B4 provider conformance suite.
    *Measured 2026-09-26 (research report 18): gate (2) PASSED —
    the shipping brain (warm `qwen3:4b` at `num_ctx=8192`, 3804 MiB)
    plus resident Laya (1448 MiB) coexist at 5745/6141 MiB with
    every call answered, no mutual eviction, and a working cold
    brain reload under Laya (Laya's latencies in that run were
    contaminated by an unrelated CPU-bound experiment; timing never
    enters the gate-2 question). Gate (3) FAILED — over the 30
    report-15 decision cases, "does this turn need a tool" scored
    AUROC 0.550 with Stella-authored noul questions and 0.567 with
    the verbatim shipped `laya.presets.router_questions`
    `needs_tools` control: coin-flip, and biased to silence 16–17
    of the 18 tool-requiring turns at threshold 0.5; the capability
    bucket choice lands 14/30. **Verdict: `SystemOneRouter` is not
    built** — a measured non-build decision, reopened only by an
    act classifier beating AUROC ~0.75 on this corpus
    (`~/tools/bench_laya_routing_calibration.py` re-runnable) or a
    different model with a routing-shaped signal. Laya's B5
    Tier-1 event-bus role is a different, still-valid measurement
    and stands.*
- **E2. Tier-0 pre-router (Cactus Needle 3 + LoRA).** Experimental,
  NOT shipped, and never wired into `src/` before its decision card
  exists. Open blockers per report 23: false-call rate (corpus v2
  rebalance), a usable confidence gate (the current head is
  policy-suppressed and uninformative), and an idle-machine latency
  re-measurement. Registry-size assumption restated by report 31: the
  report-19 verdict was measured at a fixed 13-tool registry, and the
  re-measure at the shipped 18-tool registry (20 with E1) shows the
  blockers are contract-level, not size-level — abstention stays
  0/49, the memory/reminder boundary stays 6/6 wrong, and extending
  the trigger prefix to the new capabilities changes zero routing
  decisions. No prefix rebuild is warranted; only the domain-split
  precursor (≤5 tools per instance) reopens this. The old 0.18 s
  envelope is idle-machine-only: today's control measured 0.93 s at
  13 tools and 1.02 s at 18 under normal load. If it ever lands it
  must obey the fast-path router constraints below — it may
  pre-select among existing SAFE-checked capabilities only, never
  bypass dispatcher, risk, approval, audit or step limits.
- **B8b. Stream the answer path (#230) — measured, declined, no code.** The
  last latency idea that #243's measurements left standing was that the wait
  has no beginning the user can see. Ten real decision prompts streamed from
  the resident brain say otherwise: reasoning occupies a median 95% of the
  turn, and the reply's own decode — everything a stream could have delivered
  early — is a median 0.62 s, under a quarter of a second on the short replies
  a spoken conversation makes. Chunk-0 synthesis of a first sentence costs more
  than the decode it would overlap, so the voice gain is negative. The
  two-pass design that the deferred-designs note makes a precondition of
  re-opening streaming was measured in the same run and is worse on every
  turn. Table and reasoning in `docs/MODELS.md`; the constraint list above
  still stands for any future attempt, and so does the finding that the
  remaining lever is a faster brain for turns that need no reasoning (#237).

**The closing verdict of Stage D, kept whole** — a set of declined
borrowings, not a backlog:

Explicitly **not** adopted from this input: the always-listening
hands-free loop as a background recorder, KV-cache save/restore between
sessions (no demonstrated
Stella weakness; we run one active conversation), per-persona tool
behavior (C5's standing non-goal), and his single-card VRAM plan
(it is a 12 GB plan for a different stack — only the *method*,
piece-by-piece measured budgeting, transfers).

### What is still not true

- **The test has never been run as written.** Every latency number in
  this file is a bench replay or a two-turn smoke through the dev tree, not
  six ordinary questions from a cold start. What the cold path costs is known
  in pieces, and the pieces already break it: a brain Stella supervises itself
  is ready in 4–19 s (A10), and one warm trivial turn costs 10–68 s.
- **The wait is one call, and its length is not ours to set.** Measurement
  says a trivial turn is exactly one provider request whose whole cost is
  decoding the thinking channel (~44 tok/s), and that the channel is
  stochastic: the same factual question cost 31.4 s once and 67.6 s later
  (`docs/MODELS.md`). #230 removed streaming as an answer to this, #228
  removed the per-minute cache invalidation, and the cap sweep showed that
  cutting thinking off mid-thought loses the reply instead (#244). What is
  left is #237 — A/B the local brain against the router, then set a routing
  policy — and as of 2026-10-11 it cannot run: nothing answers on
  `localhost:3001`, which is the dead endpoint #240 now makes `stella doctor`
  say out loud.
- **Tokens are not a budget axis.** `decision_max_tokens` and
  `answer_max_tokens` cap one call, not a task, so the Vision's three axes
  are two-thirds built (#231).
- **Two of the voice switches are unreachable from the interface** — see
  commitment 6, whose first rule they violate.

## 2. Her own objectivity

*The test:* tell her something confidently incorrect — she should disagree, or
ask what the basis is, and never build on it — then ask something this machine
cannot answer, and she should say she cannot rather than produce something
plausible. The Vision's claim is that this is the one place Stella can be
genuinely better than a commercial assistant, and that it is available only
because she is local: a hosted assistant's politeness is trained in and cannot
be turned off, while Stella's is a file.

### What enforces it now

- **#227. A refusal no longer ends with a tick — done.** Live on
  2026-10-06: a turn that spent 154 seconds and then refused the task — "open
  the outline and add the task manually" — printed `Action outcome: ✓
  succeeded`, because `succeeded` was defined as "a success that needs no
  state verification", so anything that never bothered to verify itself won a
  free tick. `outcome_status` now gives a receipt-less success the kind
  `nothing_changed` with a neutral `·`, and the window drops the outcome line
  for it: only a verified receipt can render as a success. An audit of every
  `ToolResult(success=True)` in `tools.py`, `outline_tools.py` and
  `web_tools.py` found exactly 16 receipt-less successes and every one belongs
  to a read, an echo or the Outline health probe, so nothing that alters state
  lost its line. Risk level was deliberately *not* the gate — `filesystem_read`
  is sensitive and `network_read` is dangerous while changing nothing, so
  gating on risk would have kept this exact bug alive.
- **#244. A turn that could not be read no longer ends in silence —
  done.** Sweeping the decision token cap down looking for the 16.8 s a
  trivial turn costs produced two turns whose token count landed *exactly* on
  the cap — truncated mid-thought — and both came back as no words at all, in
  a window that had just shown a 12-to-23-second spinner and reported the turn
  as finished. `Decision` now carries one flag, `unreadable`, set only by the
  parse fallback in `LLMBrain._safe_decision`; when a turn ends on a decision
  that failed to parse and nothing else is available to show, the runtime ends
  it with its own sentence — "I couldn't make sense of that answer. Try asking
  again." — and that sentence goes into the conversation, so the next turn
  knows the last one failed. An explicit `{"kind":"do_nothing"}` still says
  nothing (rule 9 is untouched), a garbled summary after a successful tool
  step still shows the tool's output, and the fallback grants exactly what it
  always granted: no capability, no arguments, no memory write, no approval.
- **C1. Persona as data — done.** `persona.md` (yours) plus a learned
  `persona.addons.md` compose into the system prompt under a fixed,
  app-owned invariant block that persona text can never override; no
  persona file means the byte-identical default prompt. Addon lines pass
  a forbidden-content filter with visible discard counts and hard caps
  (20 bullets / ~1 KB).
- **C2. `persona_edit` — done.** Chat-initiated style changes execute
  only as a `DANGEROUS` capability: realpath-locked to the two persona
  files, unified-diff preview, exact-match approval, verified-byte
  receipts — the same boundary as every file write.
- **C3. CLI persona tooling — done.** `stella persona` (opens
  `$EDITOR`), `stella persona preset snark|warm|terse`, and a
  first-run onboarding that drafts a persona from three questions and
  writes it only on an explicit yes.
- **C6. Persona snapshot history and revert — done.** Every write path
  (approved `persona_edit`, editor session, preset, onboarding draft,
  revert) copies the previous bytes into a `history/` directory the
  user owns before replacing the file: full snapshots, newest 10 per
  file, `manifest.jsonl` labels recording what replaced them, identical
  content deduped. `stella persona revert` lists them newest-first;
  `revert <number>` shows a diff, confirms, restores the exact bytes,
  verifies the read-back — and snapshots the state it displaces, so a
  revert is itself undoable. Recovery tooling, not a boundary: a failed
  snapshot never blocks an approved write (the result says so honestly),
  approval matching is untouched, and reflection still has no write
  path of its own. The editor copy predates the `$EDITOR` session;
  mid-edit states are not versioned.
- **C4. Bounded self-learning — done.** Opt-in transcripts (off by
  default, bounded, local, never read back into chat) feed
  `stella reflect`, which derives only observable friction signals,
  proposes at most two style edits, re-checks every candidate
  app-side (anti-sycophancy, authority lines, cap consolidation) and
  **writes nothing**: proposals are queued and surfaced as real
  approvals at the next session.
- **C5. Open questions for later.** Deliberately not built here:
  per-conversation voice switching, persona-specific tool behavior (a
  persona must never change what Stella *can* do), and any form of
  automatic write.

### Measured, and closed

- **The brain-swap that would have changed her judgement was refused on
  quality, not speed.** Report 15 measured an LFM2.5-8B candidate at 12/30
  strict against the incumbent's 28/30 on Stella's real decision path, with
  12/60 silences and `hinglish 0/6` — the same corpus that caught the
  `num_ctx=4096` truncation bug and, after the fix, scored the incumbent
  29/30 (B0). Objectivity is a property of the decision path, so this is its
  only measurement, and it says: change the brain only against that bar.

### What is still not true

- **Nobody has measured sentence 4.** The decision corpus in this file
  scores *which action she chooses*, not whether she holds a position against
  the owner, and no case asks her to disagree with a confident human. What
  exists is the negative half: she may not report a result she did not get
  (#227), may not end a turn with nothing to say (#244), and may not be
  flattered into storing a style note she cannot defend (C4's anti-sycophancy
  re-check).
- **Her spine is currently a refusal, not a stance.** The persona subsystem
  can change style and never authority (C5), which is the Vision's own rule —
  but it means "her own objectivity" today is the runtime declining to lie,
  not Stella choosing to disagree. Whether any persona text produces the
  second reliably is a question this file has no measurement for, and it
  belongs to whoever tests sentence 4.
- **Rule 9 still has two readings.** `DO_NOTHING` is legitimate, and #231 put
  a prompt line forbidding its use to abandon a task halfway. That line is an
  instruction to the model, not an enforced property: nothing in the runtime
  can tell a real decision from an early quit.

## 3. She finishes the thing

*The test:* give her a task that needs four or five real steps. She either
finishes it, or reports exactly where and why she stopped. "I can't, do it
yourself" accompanied by *succeeded* is a defect, not an outcome — the
Vision's sentence, and the reason #227 exists. Persistence is bounded by a
**budget** (time, tool calls, tokens) rather than by one decision per turn;
two of those three axes are in place and the third is named as missing below.

### What enforces it now

- **#231. The turn keeps working until its budget runs out — done.** A
  turn now carries two bounds and stops at whichever comes first: 8 tool
  calls and 90 seconds, both owner-settable numeric entries in Settings and
  persisted in `config.json` — neither a code edit nor an environment
  variable. `Stella` gained `turn_time_limit_seconds` and an injectable
  `clock`, and reads the clock at exactly the two points where new work could
  start: the top of the loop, and after a `tool` decision but before its
  dispatch. An exhausted budget ends the turn with one sentence built only
  from facts the runtime knows — the limit in force and the capabilities that
  actually got a tool result — and `StellaResult.budget` (`"steps"` /
  `"time"`) names which bound fired. The semantics are the Vision's: the clock
  stops the loop from *starting* work and never abandons a request already in
  flight, because measurement says one call can itself spend 68 of the 90
  seconds (`docs/MODELS.md`), so the panel says that in those words instead of
  promising a deadline the code does not keep. The stop is a normal result
  composed of runtime literals — no model prose, no second LLM call, no new
  authority — and the `do_nothing` prompt line now forbids using rule 9 to
  abandon a task halfway. Known consequence, stated plainly: raising the step
  bound from 2 to 8 means a model that keeps trying can spend ~80 s where it
  used to spend ~20 s.
- **B6. Desktop awareness tools — done.** Compendium item 6, built on
  reports 03/11/13 but corrected where the live 0.56.2/Omarchy machine
  disagreed with them (`src/stella/desktop/`). Three capabilities,
  default-on *and* environment-gated (a real session adapter — Hyprland is
  measured; sway/X11 written-but-unverified; KDE/GNOME/Wayland-screencopy are
  still stubs — plus
  `hyprctl`/`grim`/`tesseract`/`wtype` on `PATH`), so the model never
  sees a tool that could only fail:
  `screen_read` (grim → `tesseract --psm 6`, bounded 6 000-char OCR
  text with obvious credentials masked; pixels never persist),
  `window_focus` and `key_send` (typed only into an explicitly
  addressed, focus-verified window). Every read trusts the JSON shape,
  never the return code (the rc-0-"unknown request" trap), and every
  act is followed by a compositor re-query, so a receipt says
  *verified* only when the session proves it; `key_send` is honestly
  `inconclusive` because the application's reaction is unknowable.
  Two of the reports' measured specifics were wrong on this box and
  are corrected in code, not papered over: the instance flag is `-i`
  (not `-r`, which is *refresh* and made hyprctl answer "unknown
  request"), focus goes through the compositor's Lua dispatch API
  (`hl.dispatch(hl.dsp.focus{window=…})`; the `dispatch focuswindow
  "(address:0x…)"` string form is rejected here), grim takes
  `X,Y WxH` written to stdout via a trailing `-` (not `WxH+X+Y`), and
  it reads the window geometry hyprctl reports, which is already in
  grim's logical space — no scaling. Live on the real rig: reads 6–9 ms,
  window focus 25–29 ms (verified), key send 94–106 ms (the target
  foot process wrote the typed probe to disk, then screen_read OCR'd it
  back with tesseract mis-reading `O` as `0`), active-window OCR
  ~0.42 s, full-screen OCR 6.90 s.
  **Follow-up (owner request).** A fourth tool, `desktop_control`, grew
  the app/window surface inside its arguments:
  `target=app action=launch` (optional `workspace`), and
  `target=window action=close|move|focus`. Every use is `DANGEROUS` and
  the approval names the literal launcher command or concrete window;
  the launcher must pass a strict charset *and* resolve on `PATH`
  before anything spawns. On Hyprland each act is confirmed by a
  compositor re-query — launch verified by a genuinely *new* window,
  close by the id's absence, move by the reported workspace — so
  "open chromium in workspace 1" now lands it there and proves it. The
  tool is registered only when the probed adapter vouches for window
  management, so sway/X11 simply never offer it (rule 5).
- **E1. Web capability (TinyFish Search+Fetch).** Decided and
  live-verified (report 22 is the ticket): key in the environment only
  (`TINYFISH_API_KEY`), keyless ddgs+stdlib fallback stays the
  demonstrated secondary path. The provider budget (1000 fetch-urls/day,
  500 searches/hour) is enforced by the *runtime*, never by the model;
  `fetch` builds on the existing `NetworkReadTool` machinery (pinned
  DNS, peer-validated), not the spike's regex guard; results remain
  untrusted input inside `<<<UNTRUSTED_WEB_CONTENT>>>` with per-page
  caps. *Implemented 2026-09-28 as `stella.web_tools` (`web_search`,
  `web_fetch`) behind `STELLA_WEB`, with `ddgs` shipped as the optional
  `web` extra and a runtime `WebBudget`; see `docs/WEB.md`. The live
  key-path run from `src/` happened 2026-09-29 with a fresh key —
  search, recency and fetch all behaved through TinyFish — so every
  Stage-E claim is now live-exercised.*

### Measured, and closed

- **The agentic techniques reviewed and rejected here** — batched
  plan-ahead calls, plan-level approval, critic passes and sub-agents — are
  recorded with their reasoning under *Deferred designs and their
  non-negotiable constraints*, which also names the four real ceilings the
  loop was measured against. The two-step bound was the one of those four
  #231 closed.

### What is still not true

- **The stop cannot say what remains.** An exhausted budget names what
  ran and warns that the request may not be finished; the runtime holds no
  task model, so it does not invent one. The Vision's middle clause — "this
  is what's left, this is what blocked me" — is the unbuilt half of the honest
  stop, and it is the reason sentence 2 is only half true.
- **A turn that works is not yet a task that crosses sessions.** The
  doctrine's answer for cross-turn work is an Outline item the reminder tick
  already notices (A1), and #241 — a real Outline turn with the integration
  switched on — is blocked because nothing answers on `localhost:8741` as of
  2026-10-11.
- **How much she may do without asking is the owner's open decision.** #232
  is the tier mapping; until it is agreed, the reach recorded in B6 and E1
  runs under today's capability-by-capability risk levels.

## 4. She remembers you from the first day

*The test:* tell her one small true fact about yourself on Monday; ask
something related to it on Friday, in a fresh session, without repeating it.
Memory is the point rather than a feature — what she knew in month one and
what she learned thirty seconds ago should reach the same answer, retrieved on
her own when relevant and written deliberately rather than transcribed.

### What enforces it now

- **B8. The day-one memory test, run for real — passes, with three
  conditions.** Commitment 4 of `docs/STELLA_VISION.md` had only ever been
  unit-tested: state one small true fact, restart, ask something related
  without repeating it. Run through `bin/stella-dev chat` against the
  tree's own state on 2026-10-10, the mechanism held end to end — the
  Brain volunteered a write for a plain statement of fact, the runtime
  stored it only after a `yes`, the row survived into a fresh process, and
  a later turn retrieved it and answered *from* it ("Yes, you are allergic
  to penicillin"). Three things stood in the way, and none is the store:
  a fact needs one approval (rule 10, working as designed); retrieval at
  shipped settings is lexical and needs two shared normalized terms, so
  the paraphrased Friday question found nothing until the existing
  `STELLA_SEMANTIC_MEMORY` opt-in was switched on — then it worked; and
  with the desktop capability families enabled the decision prompt measures
  **8,151 tokens against `num_ctx=8192`**, so the **58 tokens one recalled
  memory adds overflow the request and the recall turn is the only one
  that dies** (measured: 8,209 > 8,192, `exceed_context_size_error`).
  Full write-up in `docs/OUTCOME_MEMORY.md`; the backlog's two open
  latency and setup items — A/B-ing the local brain against the router,
  and loading capability descriptions lazily instead of every turn — now
  carry a demonstrated problem rather than a suspected one.
- **B2. Semantic memory retrieval — done.** The
  `LocalHashEmbeddingProvider` is wired into memory recall
  (`src/stella/semantic_memory.py`, `src/stella/stella.py`) with the two
  decisions taken up front: it is **opt-in** (Settings checkbox or
  `STELLA_SEMANTIC_MEMORY`, because the index duplicates memory content
  into a second local file, and disabled installs never create it), and
  recall is **fused, keyword-dominant** — keyword matches keep their
  order, at most two labeled semantic supplements fill the slack, and
  the two score scales are never cross-compared. Provenance is reported
  per memory (`RetrievalSource`, surfaced in the Brain payload), the
  index is healed by full reconciliation (startup plus after every
  in-turn memory mutation) with failures reported honestly, and no
  silent ranking magic entered anywhere.
- **B2.1. Real embedding recall — done.** The provider is now a choice:
  `local-hash` stays the zero-dependency default (behavior identical),
  while `ollama` (any Ollama embedding model via `STELLA_EMBED_MODEL`,
  stdlib HTTP only) and `minilm` (`all-MiniLM-L6-v2` on CPU through the
  optional `stella[embed]` extra) add real embedding models. Neither is
  ever auto-activated. Index rows carry their provider label and vector
  dimension and search only compares within one model, so vectors from
  different models can never be mixed; switching providers is healed by
  the existing reconcile. The Ollama provider applies nomic's
  `search_document:`/`search_query:` prefixes client-side (measured:
  Ollama passes input verbatim), and an unreachable backend returns no
  vector — the turn degrades to keyword recall with an honest
  `SemanticSearchUnavailableEvent`, never a fake similarity.
- **B3. Memory-scale policies — done.** Per-turn recall now carries at
  most `MAX_RECALL_WINDOW = 16` best-scoring candidates from each
  relevance-sorted query into fusion and the conversation merge (the
  brain-facing cap stays `MAX_RETRIEVED_MEMORIES = 5`; the index
  reconciler still sees the full store — bounding recall is not bounding
  maintenance). A second write of an already-stored fact still stores
  (guidance never vetoes or silently prunes) but the result honestly
  names the existing copy and points at `memory_update`; the match is
  the same normalized term set recall uses, guarded to ≥2 terms so
  single-word facts can never shadow others. `memory_forget` was already
  an approval-gated DANGEROUS capability and a test now re-proves the
  gate rather than assuming it (`tests/test_memory_scale.py`).

### Measured, and closed

- **B8a. The registry travels once (#229) — done.** The second of those
  two items was reframed by the measurements above: the latency premise
  did not exist (prefill is cached; lazy loading could at best recover the
  ~0.9 s of a *cold* prefix), while the *capacity* premise did — every turn
  was sending `ToolDispatcher.describe()` twice, once as prompt prose and
  once as the provider's own `tools` array. The duplicate is gone: a client
  that declares the capabilities on the wire (`LLMClient
  .declares_tools_natively`, true for every shipping client) is no longer
  handed the prose copy, and a client that cannot carry them still is.
  1,198 tokens per turn at the shipped tool set, 2,172 with the OS, shell
  and browser families; measured on the wire in two identical CLI sessions
  from one tree with only the client's promise flipped, the largest prompt
  on the turn that retrieves a memory went **7,757 → 5,949 tokens**, and it
  answered instead of overflowing. No discovery layer was built —
  `ARCHITECTURE.md` still says there is none, and now says why that is the
  right answer for #229. Proof: the routing A/B in `docs/MODELS.md` (*The
  routing A/B that let this ship*) and the conformance case
  `registry-travels-once`, replayed on all five dialects.

### What is still not true

- **"Weeks later" has not been waited out.** B8 is one restart in one
  sitting against the dev tree's own state. Nothing here measures a fact
  surviving an ordinary fortnight of use, which is what the commitment says.
- **The Friday question needs the switch that is off by default.** At shipped
  settings recall is lexical and needs two shared normalized terms, so the
  paraphrased question found nothing until `STELLA_SEMANTIC_MEMORY` was
  switched on (B8). Whether semantic recall should be on by default is #245 —
  an owner decision, because the index duplicates memory content into a second
  local file.
- **A fact about the owner needs an approval to be stored** — B8's first
  condition, and rule 10 working as designed. #246 asks whether that gate
  should stand between the owner and a sentence they simply said; the
  commitment's own test is the argument for changing it, and the answer is the
  owner's.

## 5. She notices when you are not yourself

*The test:* type something sharply out of character. She should notice the
change in **register**, not comment on the content — once, plainly, and
without making a thing of it. It is allowed to be wrong: a false positive
costs one question, a false negative costs only her staying quiet. So it must
stay cheap, stay private, and never become a diagnosis or a claim about you
(rule 8: awareness, never authority).

### What enforces it now

- **A1. Idle reminder firing — done, then narrowed.** The ticker was built to
  sweep Stella's own reminder table. That table is gone — reminders are now an
  alert written into the connected Outline workspace — and `ReminderScheduler`
  was removed with it. What replaced it is smaller: `ReminderTicker` wakes on
  an interval only to ask the bridge for one Outline claim sweep, so an idle
  window can still surface an alert the user scheduled elsewhere without
  owning a schedule of its own. The queue discipline the original feature
  proved — a wake posts onto the one command queue and never evaluates on the
  wake's own thread — is still pinned by
  `test_bridge_delivers_due_reminder_while_idle_without_any_turn` and
  `test_bridge_panel_command_queues_behind_a_busy_turn` in
  `tests/test_app.py`. See `docs/REMINDERS.md`.
- **B5. Two-tier event bus — done.** Compendium item 5, built exactly
  as reports 02/12 measured it (`src/stella/event_bus.py`,
  `src/stella/laya_judge.py`, `src/stella/laya_runner.py`). Events
  (source + text + structured fields) are dispatched against at most
  `MAX_INTENTIONS = 10` registered intentions: Tier 0 is deterministic
  regex/field rules — free, auditable, and they gate Tier 1 per event
  so an intention already matched lexically is never paid for twice;
  Tier 1 is one batched typed-laya question set per residue event,
  accepted only in the shipped `laya.presets` shape (report 12:
  hand-rolled criteria drift off the trained distribution). Trust
  rules hold: the bus **records `Match` reasons and never acts** —
  consumers decide; because the checkpoint ships uncalibrated
  confidences, a `noul` probability is advisory data unless the
  intention registered an explicit threshold, while choice questions
  fire deterministically on the label. laya is not a Stella
  dependency: the judge spawns the standalone runner under the layav
  venv interpreter over line-delimited JSON, and any hang, crash or
  refusal degrades to an honest `TIER_ONE_UNAVAILABLE` status (never
  a silent "no match") with lazy respawn — routing dies, nothing
  else. Live on the real rig: 12.6 s model load, ~2.6 GiB VRAM
  resident (coexists with the A10 brain's 2.8 GB), 23–39 ms per
  batched multi-question event, clean teardown to 0 MiB. The B0
  pre-decision router remains separate and sketch-only.

### What is still not true

- **The sense does not exist.** Commitment 5 compares today's sentence to
  how you usually write, and nothing in `src/stella/` holds that comparison.
  A1's ticker delivers an alert the owner scheduled elsewhere; B5's Tier 0
  matches a regex someone authored. Both are notices the system was *told* to
  give, not ones she noticed — which is why this section has no
  "measured, and closed" part.
- **The one calibration this file measured for a cheap always-on judge
  failed.** Report 18 asked Laya whether a turn needs a tool and got AUROC
  0.550 (Stella-authored questions) and 0.567 (the shipped control), biased to
  silence 16–17 of the 18 tool-requiring turns at threshold 0.5 — a coin flip
  (B0). A register-shift detector is a different and possibly easier question,
  but it starts from that precedent, and the Vision's own limits stay binding:
  cheap, private, never a diagnosis.

## 6. Easy, and pleasant to be there

*The test:* hand the machine to someone who has never seen it and say "ask her
to remember something". If they need help, this commitment is broken. Cool,
fun and simple are requirements, not decoration — one command to start, one
screen that shows what she is doing and what she heard, nothing that asks you
to know her internals to use her. The Vision's three concrete rules are the
measure this section is written against: **no setting may be reachable only by
an environment variable; no failure may be reported only in a log; no
capability may exist that the interface gives you no way to see or switch.**
The doctor, the config keys and the dev tree are for whoever builds Stella.

### What enforces it now

- **`stella doctor` now asks the endpoint (#240) — done.** The command
  `install.sh` prints at the end of an install used to answer its most
  important question with a name: `provider: openai (preset freellmapi)`
  and `api key: present` both read READY while every turn came back
  `Connection error.`, because nothing in the report had contacted the
  address the config pointed at. A configured provider is now probed with
  the same bounded `GET /models` the Settings panel and the startup key
  sweep use — key-authenticated, no completion, five seconds — and the
  address it resolves is the address `build_application` hands to the
  client (override, then preset, then the SDK's own default), so a green
  line means the server a turn would reach answered. A dead endpoint reads
  MISSING and names the URL in both the detail and the fix; a preset with
  no key is INFO and is not probed at all; a chat-dialect router that
  guards its model list is reported as unjudgeable rather than as a
  rejected key, exactly as the launch sweep already does. Two defects in
  the same function went with it: a llama.cpp box was being told
  `api key: MISSING — add it in stella-ui` (it takes no key; it is now
  asked for the GGUF file and the server binary instead), and the report's
  one sentence about a failed turn names the provider, model and endpoint
  it was aimed at, so the diagnosis is the same wherever it is read —
  including the resident ear, which writes it to stderr while still saying
  only "Sorry, I couldn't do that." out loud.
  What it still cannot do: `config.json` is written only by the Tk window,
  so a cold machine's first step remains `stella-ui`. Nothing tonight
  demonstrated a headless first run, and rule 13 says a second writer needs
  a demonstrated need.
- **The surfaces that say where Stella stands now report the runtime, not
  the checkboxes — done (2026-10).** Four defects in the four places an owner
  reads to find out what is connected, each fixed on its own terms. *"Test
  connection" tested OpenAI, not the provider you chose*: a saved
  configuration deliberately carries no base URL for a preset that has one
  built in, so the dialog passed `None` and the SDK substituted
  `https://api.openai.com/v1` — a user pasting a working FreeLLMAPI token was
  told the key had been rejected, by a server they never chose; the probe now
  resolves `base_url or provider_keys.base_url_for(preset)`, the identical
  expression `app.py` uses to build the real client, and removing that one
  line makes the test fail with the address it should have asked. *A pasted
  key was tested with bytes the store throws away*: `save_api_key` strips
  before storing, so a token copied out of a terminal was saved clean and
  tested dirty and only the dialog could notice; the trim now happens at all
  three header-building sites, and a value that is only whitespace counts as
  absent rather than sending a bare `Bearer `. *`/status` was reading the
  checkbox instead of the dispatcher*: Outline and the desktop tools are
  doubly gated, so a ticked box with nothing behind it registered nothing and
  `/status` still claimed the capability — the owner asks for a reminder, is
  honestly refused, and is told the tool is connected; the line now comes from
  `stella.tool.describe()`, the same call the decision prompt is built from,
  with a family that was asked for and refused named on its own
  `not connected:` line. *Outline could be dark and say nothing about it*: an
  empty tool list is right for the model and leaves the person who enabled it
  unable to tell disabled from broken, so `probe_outline()` answers "could
  Stella use Outline right now?" with the gate's own precedence and a reason,
  never the token, and `stella doctor` reads it.
- **C7. Slash commands (CLI/TUI I/O layer) — done.** A typed line
  starting with `/` is a runtime command, never an utterance: it is
  intercepted in `run_cli` and the window's `_send` before the Brain,
  so no model call or approval can be steered by it.
  Two tiers: built-in control commands (`/exit`, `/status`, `/help`,
  `/version`, `/clear` for the session's conversation, `/history` for
  the newest action records, `/usage` for the token counts the provider
  reported this session (`docs/MODELS.md`), and terminal-only
  `/trace`/`/debug`
  toggles that make
  the startup flags session-mutable) and user-owned prompt templates
  in `~/.config/stella/commands/<name>.md` whose `$ARGUMENTS`
  expansion re-enters as *ordinary user input* — no authority beyond
  typing the sentence out, approvals still gate every tool. Template
  reads reuse the persona discipline (name regex, realpath
  containment, no symlinks, 8 KiB cap); unknown names error locally
  with suggestions and are never forwarded. Voice transcripts
  and events take other paths into `run_turn` and are
  structurally never command-parsed — a spoken "/exit" is a sentence.
  Deliberately not built: inline shell execution, permission
  frontmatter, `@file` embedding (rules 4, 13, 15).
- **D6. Hands-free wake, on the owner's terms (landed 2026-10-03 on
  `feature/voice-integration`, reversing part of the note below).** The
  owner asked for the wake word back, so the question was never *whether*
  but *what it may be*: one local detector whose entire authority is
  pressing the existing **Listen** button. It records nothing until a
  phrase is confirmed, it never answers an on-screen approval, and there is
  deliberately no `auto` mode — the ear exists only while a box the owner
  ticked says so. A wake that hears only a transcriber's filler for an
  empty room is reported and sent nowhere. The microphone is held by one
  shared capture process instead of one per consumer, and the voice row
  gained the two things that make an open microphone livable: a dot that
  says when it is really open and a mute switch that puts every ear down
  for the session. What the reversal did *not* buy: continuous
  transcription, speaker identification, or any cloud speech hop. See
  `docs/VOICE.md`. **Follow-up (owner request, 2026-10).** The phrase is
  the owner's now, not "hey jarvis": the ear arms every openWakeWord
  classifier detected in `~/models/openwakeword` (or exactly the ones
  Settings picks), any hit is still exactly one Listen press, and a
  custom "hey X" means a classifier the owner trained or placed there —
  Stella never downloads or trains anything herself.
- **D7. The ear stops borrowing the headset's microphone (#235, landed
  2026-10-10).** Observed on this machine on 2026-10-06, not theorised: the
  always-open tap named no capture node, so it took PipeWire's default
  source — the Bluetooth headset's own microphone whenever one is current —
  and holding that device pins the headset in its hands-free profile, which
  drops its playback to mono call-quality for as long as the ear is armed.
  The owner's music was the cost, and it was charged by default. A new
  `stella/capture_target.py` now decides the node: it deviates only in that
  one case (a Bluetooth input leading with something else available, ranked
  by WirePlumber's own `priority.session` so a plugged-in USB microphone can
  win), and answers `None` — the argv this program always built — for every
  other machine, including one with no PipeWire to ask. Settings gained a
  *Microphone* row that lists what discovery found and states, in the line
  under it, what listening on a headset's microphone costs; the choice is
  saved as `wake_source` and `STELLA_WAKE_SOURCE` still outranks it for one
  launch. Consequence stated plainly: on a machine that today silently grabs
  the headset, `stella voice --serve` now captures something else. See
  `docs/VOICE.md`, *Which microphone the ear takes*. **Follow-up (owner
  report, landed 2026-10-11).** It still took the headset, and the narrowness
  this bullet praised was the defect. Measured with the owner's headset
  connected, playing stereo, and nothing recording: `pactl info` reported
  `Default Source: bluez_input…` — a source node that was in no candidate list,
  because the first unnamed capture is what makes PipeWire create it and BlueZ
  answers that capture by switching the card profile. A rule that only steps
  aside when a headset is *already* ranked first was asking "what are the
  candidates" of a machine that had not been asked yet, so the do-nothing argv
  was the one that cost the music. `pick_preferred_source` now names the first
  non-Bluetooth input whenever one exists (a machine whose only microphone is
  the headset keeps it — refusing there would remove voice, not protect it), and
  the answer is recomputed at every capture open through a command *factory*
  (`barge_in.live_capture_command`) rather than frozen argv plus a
  process-lifetime memo, because a headset that connects after the server
  started was the second way the same promise broke. `pw-record` makes the
  fallback safe: a target it cannot find is ignored silently and the default is
  taken, so a microphone that vanished mid-session degrades one capture instead
  of killing the ear. Proof: `wpctl status` naming the built-in node as the live
  source-output's `target.object` while the card stayed `a2dp-sink` with both
  stereo links up; `pw-dump` ranking bluez 2010 over the built-in's 2009; a
  ~13 ms probe, which is why no cache stands in front of it.
- **D8. A recording is trimmed down to its speech before she reads it (#252,
  landed 2026-10-11).** The complaint was transcription rather than latency:
  *background clutter is wrecking what she hears*. Measured on this machine
  with Stella's own capture argv while music played through the headset —
  7.98 s, 249 frames of 32 ms — every single frame sat above the energy floor
  (RMS 0.13 to 0.28) while the learned detector scored the most speech-like of
  them 0.046, so 0 of 249 passed any threshold the barge-in ear works to. Loud
  is not speech, and that is why trimming on silence was rejected before it was
  written: nothing that measures volume can tell a chord from a vowel.
  `barge_in.SpeechGate` runs the Silero model the ears already load, keeps the
  span from the first sustained run of five voiced frames to the last, and
  hands it over with ~320 ms preserved either side — deliberately shorter than
  the wake endpointer's 800 ms of trailing silence, because a finished capture
  is bounded already. It is a filter and never a veto. It does not shorten a
  clip with speech at both edges, it never cuts inside the span, and when it
  finds no speech at all it returns the recording byte-identical, leaving the
  hallucination judgement where it already lived (`audio.is_transcription_junk`).
  That last rule is not caution for its own sake: no real-speech clip exists on
  this machine, so the false-negative side of the model is unmeasured here, and
  a gate that could swallow a spoken request would be a worse defect than the
  one it answers. One leg covers both callers, because `stop_and_transcribe` is
  where a push-to-talk capture and a wake capture both hand their file to the
  recogniser, and the trimmed sibling lives in the recorder's own temporary
  directory — so the dispose and cancel rules already written still hold, and
  raw audio still never enters the conversation. Saved as `voice_capture_gate`,
  shipped **on**, with `STELLA_VOICE_CAPTURE_GATE` outranking the saved answer
  for one launch and never rewriting it, and a Settings row to switch it off in
  a room where her voice starts disappearing. Proof: 19 cases in
  `tests/test_speech_gate.py` driven by a fake detector that scores frames in
  order (the edge trim, a run reaching the end not cut short, a transient
  shorter than the confirmation window anchoring nothing, a loud but unvoiced
  clip left alone, unreadable shapes and a failed write costing nothing, one
  model call per frame), two cases in `tests/test_voice.py` proving the
  recogniser is handed the gated file rather than the recorded one, and the
  config round trip below. **What this did not buy:** anything at all on the
  way in — no denoise, no beamforming, no cloud speech hop, and no change to
  what the wake ear or barge-in decide. See `docs/VOICE.md`.
- **D9. Where the detector lives is a saved setting (#254, landed
  2026-10-11).** `vad_model` was the last voice choice that arrived only from
  `STELLA_VAD_MODEL`, and the gate above made that a real problem rather than a
  tidy-theory one: a machine keeping `silero_vad.onnx` elsewhere would have
  received a gate that silently never engaged, with nothing on screen saying
  why the clutter was still there. It left `StellaSettings._environment_fields`
  — the step that gives any field a saved half, since a value the environment
  owns outright cannot have one — and now runs the six touchpoints like every
  other setting: `vad_model_env_override`, a `from_saved` parameter,
  `config.json`, `resolve_settings`, a Settings row, and the drift guard's
  tables. A blank row means the usual place, which is the same reading
  `resolve_settings` gives a file written before the key existed, and Apply
  refuses a path it cannot find — the gate, the ear and the wake listener all
  fail quietly or in a sentence the owner may never open, so a typo saved here
  would be a trim that stopped working with no symptom to trace. Proof, run on
  this machine with no `STELLA_VAD_MODEL` in the environment at all: a
  `config.json` naming the real file resolves and builds a panel holding a
  `SpeechGate`; the same code with the same file naming a path that is not
  there resolves that path faithfully and builds a panel with no gate. Nothing
  moved on the owner's disk and no default was flipped — `default_vad_model()`
  still points at `~/models/silero/silero_vad.onnx`, which is where this
  machine already keeps it.

### What is still not true

- **The rest of the voice configuration is still environment-only.** The three
  switches this bullet used to name as a live violation — `STELLA_SPEECH_RESIDENT`
  (D2), `STELLA_VOICE_BARGE_IN` (B7, D4) and `STELLA_VOICE_CAPTURE_GATE` (D8) —
  became saved Settings on 2026-10-11, in the same shape every other saved
  choice uses: a row in the panel, one key in `config.json`, and the environment
  variable outranking the saved answer for a single launch without rewriting it.
  `STELLA_VAD_MODEL` joined them the same day (D9), because a gate that reads a
  model the owner cannot point at is a gate she cannot turn on. What is left is
  the deliberate half: `STELLA_BARGE_SOURCE`, `STELLA_SPEECH_COMMAND` and the
  transcription and speech commands are still reachable only by export, still
  absent from `src/stella/ui.py`, and so still unsettable from a machine whose
  owner never opens a terminal. No saved default was flipped.
  `voice_barge_in` stays `auto` because the ear also hears
  Stella speaking, and `on` is the spelling that arms it on a capture nobody
  chose; `speech_resident` stays off because the worker Stella finds at
  `~/tools` is already resident, so a shipped `True` would change nothing on
  the one machine this project runs on and promise something the checkbox
  cannot keep; `voice_capture_gate` is the one that ships on, and it is the
  exception rather than a contradiction — it removes audio rather than adding a
  capability, it can never refuse a turn, and the complaint it answers is live.
  Proof: `tests/test_settings_wiring.py` (both names walked
  through every touchpoint a saved setting needs, including the panel variable
  that writes the save dict), `tests/test_config.py` (round
  trip, a file older than the keys, a hand-edited mode read as a name not an
  invention, the environment outranking the file in both directions),
  `tests/test_ui.py` (the rows open on the saved answer and write the widget's
  current answer), `tests/test_barge_in.py` (a nonsense mode refused at every
  door, including the one that would otherwise have armed the ear).
- **A cold desktop still needs a screen.** The #240 bullet above names this
  from the doctor's side: `config.json` is written only by the Tk window, so
  `install.sh`'s closing advice — run `stella doctor`, configure in
  `stella-ui` — assumes a desktop session, and a headless first run has no
  documented path. Nothing demonstrated one, and rule 13 wants a demonstrated
  need before a second writer.
- **The diagnosis still has a channel the owner may never open.** #240 put
  the aimed-at sentence on the resident ear's stderr rather than into the
  audio, which makes a failed spoken turn *reportable* — not visible. Someone
  who only ever speaks to Stella hears "Sorry, I couldn't do that." and
  nothing else.
- **Nobody has handed the machine over.** Every "usable" claim in this file
  comes from the owner or from a test bench; the commitment's stranger has not
  appeared yet.

## The tiers, and what enforces them

The Vision draws safety as a three-tier table by **blast radius** — just do
it / tell me while you do it / never without you. That is not a seventh
commitment and this file does not renegotiate it. What lives here is the
machinery that makes the third tier real: what a person is shown before an
irreversible action, what authorizes it, and what can be answered afterwards.
#232 — which tools move into which tier — is open and needs the owner's
explicit yes before any code changes.

- **A2. Content-aware approval previews — done.** Approvals now show a
  bounded unified diff (edit), new content (write), loss excerpt
  (delete) or validated URL (network read), computed by app code only
  for already-validated arguments and never part of the authorization
  token (`ActionPreview` in `src/stella/tools.py`,
  `docs/APPROVAL_BOUNDARY.md`).
- **A3. Durable action history — done.** The bounded audit trail is now
  an injectable store (`ActionHistory` in `src/stella/history.py`):
  SQLite-backed in the application (`STELLA_HISTORY_DB`, retention
  enforced on every append) and in-memory by default for tests. Entries
  reuse the `AuditRecord` shape, stay metadata-only, and are visible in
  the desktop History tab (`docs/AUDIT_LOGGING.md`).
- **A4. `network_read` receipts — done.** Every fetch attempt carries a
  `fetch` receipt (`verified` + byte count, or `failed`/`invalid`) that
  lands in the durable history with the validated URL, so "what did
  Stella read?" is answerable after the fact
  (`docs/NETWORK_READ.md`).
- **B1. Argument-aware risk classification — done.** The deferral's
  condition (must not weaken the exact-match approval boundary) is
  satisfied structurally: `Tool.argument_risk` is application-owned code
  and the dispatcher takes the **maximum** of the capability floor and
  any argument elevation, so elevation can only ever add scrutiny —
  a DANGEROUS floor never loses its approval for any arguments, and
  `ApprovalRequest` exact-match verification is untouched. Two honest
  rules ship today: a credential-named path (`.env…`, `*secret*`,
  `*.pem/.key`…) read through `filesystem_read` escalates to an approval,
  and `screen_read` with `scope=full_screen` does too (every window, not
  the focused one). `execute` re-derives the effective risk after
  validation (defense in depth) and the audit line records the risk
  actually applied. Proof: `tests/test_argument_risk.py` (monotonicity
  over every capability × argument samples, token-match regression,
  validation-before-elevation ordering).

A3 and A4 do double duty: an audit trail and a receipt are also
commitment 6's rule that no capability may be invisible from the interface.

## Where the stages went

The five stages are the order problems appeared in, not the shape of the
project, so their headings are gone from the body of this file. Each stage's
own framing is kept verbatim below — provenance is not something a restructure
should silently drop — with the item ids it contained and where each now sits.

### Stage A — a functional daily assistant (complete — released in 1.1.0)

The bar for Stage A: "I should be talking, asking, and it should be
working and helping me, even if not the fastest."

**Now under:** A1 → commitment 5; A2, A3, A4 → the tiers; A5, A6, A7, A8,
A9, A10 → commitment 1.

### Stage B — sharper judgment and scale (complete — released in 1.2.0)

**Now under:** B0, B4, B7, B8b → commitment 1; B1 → the tiers; B2, B2.1,
B3, B8, B8a → commitment 4; B5 → commitment 5; B6 → commitment 3.

### Stage C — personality (complete — released in 1.2.0)

Stella's character becomes durable, editable data — while the trust
model stays exactly as in Stage A: the model proposes, the app decides,
approvals bind to exact arguments. Personality may change *style* only;
risk levels, tool registration and dispatch remain application-owned
code no text file can influence (`src/stella/persona.py`,
`src/stella/tools.py`, `docs/PERSONA.md`).

**Now under:** C1, C2, C3, C4, C5, C6 → commitment 2; C7 → commitment 6.

### Stage D — conversational voice and decision speed (complete)

All five items carry measured outcomes (2026-09-27→28): D1 keeps the
shipped brain line, D2 and D3 shipped, D4 unparked and accepted
barge-in with the `auto` default, D5 was tested and rejected.

Source of the direction: a third-party local-voice-agent walkthrough
(the "Pythagoras" video transcript reviewed 2026-09-27) whose stack —
a 35B MoE with all experts on CPU, Whisper STT, a quantized TTS model
and Silero VAD — reached ~2.5–3.5 s voice turns on one 12 GB card.
None of his numbers are verified on this machine; they are research
input, not facts (rule 14). What qualifies each item here is a
*demonstrated Stella weakness* it aims at: the parked barge-in
blocker (report 19), the prefill-dominated decision turn (reports
15 and 20 measured ~5.0k tokens in, ~110 characters out), and the
silent gap a voice user hears while tools work. The trust model is
untouched by every item: streaming carries final response text only,
the ear proposes nothing, and any spoken filler is chosen by
application code, never by the model.

Work order agreed 2026-09-27: D1 is mic-free and can run any time;
D2–D4 land together in a dedicated TTS/STT session (barge-in resumes
inside that session, not before); D5 waits behind the voice work.

**Now under:** D1, D2, D3, D4, D5 → commitment 1; D6, D7 → commitment 6.

*(Its closing paragraph — the borrowings explicitly not adopted from the
external walkthrough — sits with the other declined measurements under
commitment 1.)*

### Stage E — agreed-stack capabilities (planned 2026-09-27)

The measured model choices are registered in `docs/MODELS.md` — that
file, not prose scattered across stages, is the authority on what runs
where, and a swap there requires its stated evidence bar. One decided
item is still awaiting implementation:

**Now under:** E1 → commitment 3; E2 → commitment 1.

Direction note (user, 2026-09-27): the ambition is a *JARVIS-like
feel* — immediate, spoken, aware of the desk, proactive within rule 8.
That ambition changes what we build *toward*, not the trust model it
runs through: the Vision's non-goals stand, and wake-word/always-
listening remain on the out-of-scope list until that section is
deliberately rewritten with a measured case for them. *Rewritten
2026-10-03, by the owner's own request rather than by research — and the
rewrite changed the scope list, not the trust model: D6 in Stage D and the
note at the end of this file record what landed and what still stands.*

## Deferred designs and their non-negotiable constraints

Three reviewed-but-unbuilt designs whose constraints must survive if any is
ever picked up (the streaming and fast-path reviews were archived; the
agentic-work review kept its own document):

Two of them serve commitment 1 — response streaming and the fast-path
router — and the third serves commitment 3. They stay in their own section
rather than inside a commitment, because the thing worth keeping is the
constraint list, and a constraint outlives the design it was written for.

- **Response streaming** — stream only final response text, never Brain
  decisions, memory writes, capability validation, approvals, tool execution,
  or multi-step transitions: partial structured JSON must never trigger an
  action. A future design adds a separate optional streaming capability rather
  than changing `chat()`'s return type, keeps the provider-agnostic interface
  free of OpenAI-specific events, and must prove streaming does not alter
  decisions, authorization, audit records, memory writes, or step limits.
  Measured TTFT (~1.6 s) says the win is modest until the final-response path
  is separated from the structured decision JSON. Spoken replies were the
  case hoped to justify it: overlap chunk-0 synthesis (~0.42–0.87 s of
  measured render) with the tokens of a longer reply still arriving.
  **Tried and rejected on this stack (2026-10-03, `4e818b2`).** Stages 1-5 of
  the copper-delta-trout plan landed an optional `stream_chat`, a native
  Ollama streaming transport, a `StreamingSentenceSplitter`, a lazy chunked
  pipeline that consumed it, and a `STELLA_TURN_TRACE` diagnostic. The bench
  (9 turns per arm, three fixed three-sentence prompts, real Ollama on 11434
  with the resident Kokoro worker) reported `first_artifact` at essentially
  the same time on both arms, and none of the marks the streaming path exists
  to fire ever appeared. Root cause is not the wiring: `LLMBrain` sets
  `answer_content_is_final = True` and `Stella.process` at the ANSWER branch
  returns `decision.content` directly without ever calling `synthesise`, so
  a real spoken turn installs the callback and never runs the code that would
  consume it. Streaming's premise — that a long spoken reply sits silent
  while the model writes the rest — is only true when the reply is composed
  on a path that has a "rest" to overlap; on the fast path, the reply exists
  whole at the end of the same LLM call that made the decision. Re-opening
  this needs a Brain design that separates decide from answer for spoken
  turns (which is exactly what the fast path's own comment rejects: an extra
  LLM call for identical authority), not another streaming layer.
  **Measured from the other side and declined (2026-10-11, #230).** That
  re-opening condition was tested with numbers instead of an argument. The
  exact decision prompt of ten turns was replayed against the resident brain
  with `"stream": true` and every delta timed: the first byte of the decision
  JSON arrives at a median 95% of the turn, so the only span a stream could
  hand over early — the reply's own decode — is a median **0.62 s**, and
  0.16 – 0.21 s on the short replies a spoken turn actually makes. The gate
  (≥ 2.0 s median tail, ≥ 0.8 s earlier first audio) failed on both limbs, and
  the two-pass design it names was measured too: a separate prose call makes
  the first character land later than today's whole reply and the finished
  reply land at a median 33.0 s against 18.5 s. What the run proved in the
  negative's favour is recorded in `docs/MODELS.md` (*The tail a stream could
  have handed over*) so nobody re-measures it: Ollama's deltas are genuinely
  incremental and `kind` always precedes `content`, so a safe streaming tap
  is buildable — there is simply nothing in it worth delivering. No code
  changed.
- **Fast-path local router** — limited to existing `SAFE` fixed-argument
  capabilities (`datetime` first, `system_info` later, only after observing
  real false-positive behavior), a short documented exact-phrase allowlist
  that returns no match on ambiguity, construction of fixed arguments in
  trusted code, execution through the existing tool's own validation, and a
  trusted locally formatted response (routing to a tool while keeping LLM
  synthesis saves nothing). It must never bypass dispatcher, risk, approval,
  audit, or step limits, never turn natural-language text into filesystem
  paths or write arguments, and never become a second hidden decision system.
- **More agentic work (multi-tool requests)** — reviewed 2026-10-04 and
  recorded in `docs/AGENTIC_TASKS.md`: the four real ceilings are the
  two-step bound, one tool call per decision, nothing surviving the turn
  and one approval per dangerous action, each drawn deliberately from
  measurement. The first of those is now closed (#231, 2026-10-10): the loop
  runs on two owner-settable turn budgets — 8 tool calls and 90 seconds,
  saved in `config.json` and edited in Settings — and an exhausted budget
  stops with a sentence naming what ran instead of a hardcoded two-step
  message. Of the techniques reviewed, reading the existing `ActionReceipt`
  before a retry remains a candidate;
  batched plan-ahead calls, plan-level approval, critic passes and
  sub-agents are rejected here. Cross-turn tasks are not a new engine: the
  doctrine's answer is an Outline item that the existing reminder tick
  already notices. The "autonomous agent loop" boundary in *Explicitly out
  of scope* below is unchanged, and a stored plan must inform rather than
  act.

## Packaging and release

- The repository is published on GitHub as `origin`; branches are
  pushed only with the user's explicit approval, never force-pushed.

- Versions follow semver per milestone: **1.1.0 = Stage A complete**,
  **1.2.0 = Stages B and C complete**, and so on. Every tag from `v1.0.0`
  onward stays where it is — a wheel installed by checksum has to remain the
  bytes it was published as, so a bad release is superseded, never retagged.

- Every release gets a `CHANGELOG.md` section written from the user's
  point of view, and work lands as one commit per deliverable.

- From 1.5.0 the release is what users run: `install.sh` installs the
  published wheel after verifying its checksum, and a checkout is the
  development copy, kept in its own state directory by `bin/stella-dev`.
  `docs/RELEASE.md` is the whole procedure and what CI proves.

- What `stella doctor` can now say about a cold desktop is filed under
  commitment 6, because it is an interface promise; the release mechanics
  above are what CI proves.

- **The plumbing is portable; the desktop is not.**
  `src/stella/portable.py` makes how a command string becomes an argv, where a
  file lives and how a private file is made private correct by construction on
  Linux, macOS and Windows, and `src/stella/childproc.py` gives Stella's
  subprocess fleet a lifetime guarantee that survives any death, chosen per
  platform and never assumed — on Linux `PR_SET_PDEATHSIG`/`SIGKILL` armed
  between fork and exec by the kernel itself, proven by a test that kills a
  real parent with SIGKILL and watches the child die. Stella remains a Linux
  desktop assistant: there are no screen, keyboard or window adapters in the
  portable layer, and only Linux is *used* by this repository's owners. The
  suite running on all three platforms in CI is what turned the first real
  Windows ACL attempt into a bug report rather than a release.

## Explicitly out of scope

Not planned, in any stage, unless this section is deliberately rewritten:
MCP integration, a plugin system, an autonomous agent loop, multi-user
accounts, cloud hosting, mobile apps, speaker identification, emotion
detection, a model marketplace, automatic large-model downloads,
self-modifying behavior and self-learning beyond the approval-gated style
notes of Stage C, Kubernetes/Docker orchestration, arbitrary shell access,
and unrestricted computer control.

Two lines here were deliberately rewritten on 2026-10-03, and the rewrite
is the whole record of why: *wake-word detection* became an opt-in
capability (Stage D's D6, `docs/VOICE.md`), and *always-listening audio*
was narrowed to what it has always meant here — continuous **recording**,
which is still out. A ticked wake box holds the device open for a local
classifier and stores nothing until a phrase is confirmed; a mute switch
and an on-screen dot are what keep that visible and reversible. Speaker
identification and emotion detection were not part of that rewrite and
stay out.
