# Changelog

## Unreleased

### The window learned to change its clothes (dark and light themes)

- **One palette became two.** `src/stella/ui.py` replaced its
  hard-coded dark constants with `Theme` dataclasses carrying a full
  dark and a full light palette (surfaces, fields, text, accent,
  status colors, bubbles), tuned for a minimal, quiet look in both
  modes. Every widget builder reads the active palette; nothing else
  about the window's behavior moved.
- **Live toggle, remembered choice.** A Light/Dark button in the
  header switches without restarting or interrupting anything: ttk
  styles are re-applied, the plain Tk widgets (transcript, composer,
  lists, open approval dialogs with their previews) are recolored in
  place. The choice persists in a `ui-theme` file beside
  `config.json` and is loaded at launch; unreadable state falls back
  to dark.
- **Presentation only, by construction.** The bridge contract,
  transcript wording, approval exactness and the setup wizard are
  untouched; the theme file stores one word. `docs/UI.md` documents
  the design; two focused window tests cover the recolor, the
  persisted choice and the fallback.

## 1.2.0 — 2026-09-27

### Stella can be interrupted by voice (Stage B7, opt-in barge-in)

- **The ear shipped, still just a button.** `src/stella/barge_in.py`
  runs a raw 16 kHz mono capture (`pw-record -a`, `arecord` fallback)
  through the Silero VAD v6 ONNX model on one daemon thread; five
  consecutive frames above both a probability and an energy floor
  (~160 ms of speech) fire exactly one callback, and that callback is
  `StellaBridge.cancel_current_turn()` — the documented any-thread
  Cancel press from A8. The detector can approve nothing, inject
  nothing, and never records: its only output is the interrupt.
- **Opt-in and environment-only, like all voice settings.**
  `STELLA_VOICE_BARGE_IN` defaults to `off`; `STELLA_VAD_MODEL`,
  `STELLA_BARGE_SOURCE` (e.g. `ec_mic`) and `STELLA_BARGE_THRESHOLD`
  cover the rest, and nothing is added to `config.json`. The one new
  dependency is `onnxruntime`, isolated in a `barge-in` extra
  (`uv sync --extra barge-in`) because the pip `silero-vad` package
  would have dragged CUDA torch in (report 08).
- **Echo cancellation is a prerequisite, documented as a recipe.**
  Measured: uncancelled playback registers as speech on 23% of
  playback frames; through PipeWire's WebRTC echo-cancel it is 0% at
  −29.6 dB. `docs/VOICE.md` now carries the `pactl` setup (load the
  module while the built-ins are default, then move the defaults to
  `ec_out`/`ec_mic`) and the Bluetooth caveat (keep SCO out of the
  ec chain).
- **Degrades exactly like every other voice part.** A missing extra
  or model says so once at startup and leaves everything else
  working; an internal fault retires the ear for the session; a
  broken ear never touches text, Listen, or speech. 20 offline tests
  (judge, subprocess listener via a synthetic byte writer,
  settings, bridge arm/disarm lifecycle) — no microphone needed.
- **Live testing then found a blocker, and barge-in is parked with it
  documented.** With the ear on, the first sentences of each spoken
  reply played stretched and glitching (a 7.5 s file took 45.8 s,
  another 76.6 s in the UI trace) while the detector logged *zero*
  voiced frames — and the byte-identical UI with the ear off plays
  perfectly. Five controlled reproductions, up to and including the
  full UI shape (per-episode ear churn, real producer/consumer
  threads, real Kokoro synthesis during playback, zero-delay arming),
  all stayed clean: the interference needs the fully loaded UI and is
  still unexplained (leading hypothesis: PipeWire real-time
  starvation with brain + TTS + capture live at once). The human
  double-talk acceptance is therefore not taken; `off` remains the
  default (research report 19 records every measurement).

### The System-1 router gate: measured, and it says no (Stage B0)

- **All three B0 prerequisites are now facts, not hopes** (research
  report 18). Coexistence passed: the shipping brain plus a resident
  Laya sidecar fit in 5745/6141 MiB with no mutual eviction. But the
  router's whole premise — that a cheap classifier can decide "does
  this turn need a tool?" — failed on Stella's own decision corpus:
  authored routing questions scored AUROC 0.550 and the verbatim
  shipped `router_questions` preset 0.567, a coin flip biased to
  silence 16 of the 18 tool-requiring turns at any sane threshold.
- **`SystemOneRouter` is a recorded non-build decision.** No code was
  written for it, which is the point of measuring before building;
  Laya keeps its already-validated B5 event-bus role, and ~1.4 GiB of
  VRAM stays headroom. The decision reopens only at AUROC ≳ 0.75 on
  the re-runnable calibration bench.

### A provider swap is now a measured claim, not a hope (Stage B4)

- **One written contract.** `src/stella/conformance.py` states the
  obligations Stella's decision path depends on from any provider as
  data: 16 named cases (clean, code-fenced and prose-embedded decision
  JSON; replies with no JSON failing closed to silence; native tool
  calls with dict, JSON-string and malformed arguments; the reserved
  `tool_final` marker stripped before the dispatcher; invented
  capabilities refused by the trusted dispatcher; the REQUIRED answer
  guard; an HTTP 500 surfacing as an error, never as a fake reply).
- **Proven offline on the real clients.** A loopback simulated provider
  replays each case's wire shape at all four shipped clients
  (OpenAI-compat via the Responses API, Ollama compat, Ollama native
  `/api/chat`, llama-server compat) through the actual `LLMBrain`
  decision path — 61 case×dialect checks, deterministic, no GPU, no
  network beyond 127.0.0.1 (`tests/test_conformance.py`).
- **Proven live on the shipping endpoint.** The same script re-asks the
  endpoint-only obligations
  (`~/tools/bench_provider_conformance.py`, measured today on
  `qwen3:4b` at `num_ctx=8192`): a nonexistent model returns an honest
  HTTP 404; the whole real decision prompt was evaluated
  (`prompt_eval_count=3442` vs a 4,149-token size estimate — the
  written, checkable form of the report-15 truncation question); the
  compat tool channel delivered a clean native `datetime` call.
- **A prompt-bloat tripwire ships as a test:** the decision prompt must
  keep fitting the wired context budget, so a future prompt grows
  caught by the suite, not by a silently truncated answer.

### Some ordinary requests now cost an approval (and one fewer floods recall)

- **Argument-aware risk (Stage B1).** Risk is no longer purely
  per-capability: two trusted, additive rules raise scrutiny for
  specifically dangerous shapes. Reading a credential-bearing file
  (`.env`, `secrets/…`, `*.pem`…) through `filesystem_read` now requires
  an approval like a write does, and an explicit full-screen
  `screen_read` requires one because it captures every window, not just
  the focused one. The mechanism can only ever **add** approvals: the
  effective risk of a call is the maximum of the capability's floor and
  the argument elevation, so no argument phrasing can demote a dangerous
  capability, the exact-`ApprovalRequest` match is untouched, and an
  audit line honestly records the risk that was actually applied
  (`tests/test_argument_risk.py` proves all of this).
- **Memory scale policies (Stage B3).** Per-turn recall is bounded to the
  best-scoring 16 candidates before fusion (a large store can no longer
  make every turn scan everything), a repeated memory write still stores
  but now honestly notes the existing copy and suggests `memory_update`
  instead of growing duplicates, and the approval gate on `memory_forget`
  is re-proved by tests rather than assumed (`tests/test_memory_scale.py`).

### The Ollama brain was losing most of its own instructions

- **Silent truncation found by measurement, not mood.** Every decision
  turn sends a ~5,300-token prompt (system contract plus context), but
  Ollama's own `prompt_eval_count` reported exactly 2050 evaluated
  tokens on every turn at `num_ctx=4096` — and still finished with
  `stop`, never a length error. The head of the system prompt, where
  the strict-JSON and routing rules live, simply never reached the
  model.
- **Fixed to `num_ctx=8192`, with a test pinning the wiring**
  (`build_application` now asks for a context that fits the whole
  decision prompt). The effect is measured, not assumed: on the decision
  bench the incumbent goes from 21/30 to 29/30 strict cases, and on the
  injection bench it obeyed instructions planted inside a tool output on
  4 of 8 identical decisions before the fix and 0 of 8 after. (research
  report 15; before/after via `~/tools/measure_prompt_ctx.py` and
  `~/tools/bench_injection_repeat.py`)

### Stella can read and act on the Hyprland desktop, only when proven

- **Three opt-in desktop capabilities** — `screen_read` (a local
  screenshot fed to local OCR — no cloud, no vision model), `window_focus`
  and `key_send`. They are registered only when the settings flag is on
  *and* this really is a Hyprland session with the tools installed, so
  a model never sees a capability that could only fail.
- **Nothing is trusted, everything is re-queried.** Reads accept only
  parseable JSON of the expected shape (the compositor answers garbage
  with rc 0), and every act is followed by a fresh compositor query, so
  a receipt says *verified* only when the session proves it. A keystroke
  send is honestly *inconclusive*: the keys reach a focus-confirmed
  window, but what the application did with them is unknowable from
  here.
- **Privacy bound on what the screen says** — OCR text is capped and
  obvious credentials are masked before the model sees them, and pixels
  are never written to disk.
- **Corrected against the live machine, not the notes** — the plan's
  measured syntax was wrong in four places (the instance flag, the focus
  dispatch route, grim's geometry format and its stdout argument); each
  is fixed to what this Hyprland 0.56.2 build actually accepts and was
  verified end-to-end with a real window. (live: reads ~8 ms, focus ~26 ms,
  key send ~100 ms, window OCR ~0.4 s, full-screen OCR 6.9 s)

### Events now route through two cheap tiers before anything acts

- **The event bus** (`stella.event_bus`) registers up to ten standing
  intentions and dispatches events (a source, text and structured
  fields) through Tier 0 — deterministic regex/field rules a human can
  read and audit — and only the events a rule did not already explain
  are asked of Tier 1, one batched laya question set per dispatch in
  the shipped preset shape. A match is a *record with a reason*, never
  an action: consuming a match stays each feature's own approved
  decision.
- **Uncalibrated confidence never acts.** The laya checkpoint's
  probabilities are advisory data unless an intention explicitly
  registered a threshold; choice-type questions fire on the label,
  which is a deterministic reading of the answer.
- **laya stays outside Stella.** A separate interpreter venv runs a
  small line-JSON runner (`stella.laya_judge` + `stella.laya_runner`);
  a hung, crashed or refusing judge is dropped with an honest
  "unavailable" status — "could not ask" never reads as "no match" —
  and is restarted on the next question. (live: 12.6 s to load,
  ~2.6 GB VRAM beside the brain, 23–39 ms per event, clean teardown)

### Stella now runs her own llama.cpp brain

- **Provider `llama`** — point Stella at a downloaded `.gguf` file
  (Settings tab, or `STELLA_LLM_PROVIDER=llama` with the path in
  `STELLA_MODEL`) and she launches `llama-server` as her own child
  process with the measured high-quality launch line — no systemd
  unit, no manual server, nothing left running behind her.
- **Honest lifecycle** — startup waits for the server's own health
  endpoint and reports the server's last output when it refuses to
  come up; a port someone else already answers on is refused rather
  than quietly shared; shutdown sends the server its own clean
  SIGINT path first and escalates only if ignored (live: 4–19 s to
  ready, ~2.9 GB VRAM, gone on close).
- **Test connection** for the llama provider truthfully checks what a
  launch needs — the model file and the server command — since no
  server exists until Stella starts one.

### The window got a real look

- **Dark, calm theme** — one palette across the chat window, the
  approval dialogs and the first-run setup wizard: deep slate surfaces,
  a themed font picked from what the system has, and consistent
  buttons, tabs, lists and fields.
- **Speech bubbles** — your messages render as right-aligned bubbles,
  Stella's replies as left ones with a highlighted name; reminders,
  errors and quiet system notes each have their own readable style
  instead of one undifferentiated block of text.
- **Clearer chrome** — a window header, an accent-colored Send button,
  an italic status line, and approval dialogs that read as a heading,
  the exact action, the diff preview, and two obvious answers.
- **Nothing semantic moved** — every word the transcript, status line
  and dialogs assert is the same text through the same bridge
  contracts; this is presentation only.

### Speech starts speaking while the reply is still being written out

- **Sentence by sentence** — a multi-sentence spoken reply is now
  synthesized one sentence at a time and played from a queue: the
  first sentence reaches the speakers after roughly one sentence of
  rendering instead of after the whole reply (measured ~10.7 s →
  ~1–2 s first-audio on local synthesis, which renders faster than
  real time). Single-sentence replies behave exactly as before.
- **Stop speaking means stop** — the button (and **Cancel**) now
  silences the entire spoken reply: the sentence playing and the
  sentences only queued, which are discarded unplayed. The underlying
  decision is still never touched.
- **Honest partial speech** — if synthesis fails mid-reply, the
  sentences already rendered are still spoken, one error line says
  the rest could not be prepared, and the text response remains fully
  available. Every played or drained artifact is deleted; command
  providers now name each synthesis uniquely so queued audio can never
  overwrite a sentence still playing.

### Persona writes became reversible

- **Every replacement keeps a snapshot** — before an approved
  `persona_edit`, an editor session, a preset, an onboarding draft or
  a revert touches `persona.md` / `persona.addons.md`, the previous
  bytes are copied into a `history/` directory you own (newest 10 per
  file, labeled with what replaced them; identical content is
  deduped).
- **One command away from undone** — `stella persona revert` lists the
  undoable versions; `stella persona revert <number>` shows a unified
  diff, asks you to confirm, restores those exact bytes and verifies
  the read-back. The restore snapshots what it displaces, so reverting
  a revert is just one more revert.
- **Honest limits** — a failed snapshot never blocks a write you
  already approved (the result then says the change cannot be
  reverted), the editor copy predates the `$EDITOR` session, and
  reflection still writes nothing: approved proposals inherit history
  through the same `persona_edit` path as always.

### Cancel reaches the whole voice periphery

- **The transcribing window is cancellable** — pressing **Cancel**
  while "Transcribing..." is shown abandons the capture and a running
  local transcription command within about a second (the command
  providers grew a `cancel()` handle with a SIGINT → terminate → kill
  ladder, mirroring the player), and honestly reports that nothing was
  sent: a cancelled transcript is never replaced with invented text
  and no turn starts.
- **Cancel means silence** — `cancel_current_turn` now also stops
  playback, and speech synthesized around a cancel is discarded
  (its file deleted) rather than played. A cancel raised during
  transcription survives into the turn instead of being erased: the
  turn ends cancelled at its next safe point.
- **Unchanged by design** — tools and approvals stay atomic
  (cancel never interrupts an executing action), the CLI path is
  untouched, and an abandoned cloud transcription/speech request is
  given up on without a socket close — the honest limit of an SDK
  call that exposes no per-request cancel.

### Real embedding recall (opt-in)

- **Three embedding providers, one honest boundary** — semantic recall
  keeps `local-hash` (the zero-dependency word-shape index) as its
  default, and can now be switched (Settings choice or
  `STELLA_SEMANTIC_PROVIDER`) to a real embedding model: `ollama`
  embeds through a local Ollama server (`STELLA_EMBED_MODEL`, default
  `nomic-embed-text`, stdlib HTTP — no new dependency) or `minilm` runs
  `all-MiniLM-L6-v2` on CPU via the optional `stella[embed]` extra.
  A missing extra or backend fails its own use honestly and never
  auto-switches the choice. Ollama's nomic prefixes
  (`search_document:`/`search_query:`) are applied client-side, because
  measurements confirmed Ollama passes embedding input verbatim.
- **No cross-model vector mixing** — every index row now records its
  provider label and vector dimension, and search computes cosine only
  against rows from the same model; legacy and foreign rows are skipped
  until the next reconcile rewrites them (reconciliation is also the
  provider-switch migration).
- **Unavailable is not irrelevant** — when the embedding backend cannot
  answer a query, the turn proceeds on keyword recall and the trace
  records a `SemanticSearchUnavailableEvent` (visible with
  `STELLA_TRACE=1`); fusion supplements carry the active provider's
  label in their provenance, so `"ollama-embedding"` and
  `"local-hash-embedding"` hits are always told apart.

### Semantic memory recall (opt-in)

- **Keyword-dominant fused recall** — a new Settings checkbox (or
  `STELLA_SEMANTIC_MEMORY=1`) wires the local word-shape embedding index
  (`LocalHashEmbeddingProvider` + `SQLiteSemanticIndex`) into memory
  retrieval. Keyword matches keep their exact place; at most two
  semantic supplements may fill the remaining space, so nothing about a
  lexical hit's ranking silently changes. Disabled installs never create
  the index file at all (`STELLA_SEMANTIC_DB` moves it if enabled).
- **Honest provenance, no ranking magic** — every retrieved memory
  reports how it was found (`keyword` with the lexical score, or
  `local-hash-embedding` with the cosine similarity) in the Brain
  payload; the two scales are never compared with each other, and the
  prompt states that a word-shape match means the words look alike —
  never that anything understood the meaning.
- **Self-healing index with honest failures** — the index is rebuilt
  from the authoritative memory store at startup and after every
  in-turn memory mutation, so edits made while Stella was down are
  caught up too. A failed refresh is reported (a trace event plus a
  note on the answer) and never changes the memory write's own verified
  outcome.

### Personality (data, never code)

- **A written persona** — `~/.config/stella/persona.md` (and
  `persona.addons.md` for learned style notes) is composed into the
  system prompt *below* a fixed app-owned invariant: Stella is an AI
  assistant and no persona text — hers or yours — can override that or
  the rules and approvals beneath it. With no persona file, the prompt is
  byte-for-byte what it always was. `stella persona` opens the file in
  your `$EDITOR`; `stella persona preset snark|warm|terse` writes a
  starter instead of a blank page (refuses to clobber without
  `--force`); first CLI launch after setup asks three questions, drafts a
  persona with your model, and writes it only if you say yes.
- **`persona_edit` — style changes go through approval** — asking Stella
  in chat to "be drier" can only take effect via a `DANGEROUS` tool call:
  path locked to exactly one of the two persona files (realpath-checked),
  a unified diff in the approval dialog, exact-match approval, and a
  verified-byte receipt afterwards. Learned style notes pass a
  forbidden-content filter (authority words are discarded, with the count
  reported, not hidden) and hard caps of 20 bullets / ~1 KB.
- **Opt-in transcripts and `stella reflect`** — a new Settings checkbox
  (or `STELLA_TRANSCRIPTS=1`) records a bounded local transcript (newest
  2 000 rows, turn text and cancellations, never read back into chat;
  off by default). `stella reflect` derives only observable signals
  (cancels during long replies, your own style pushback — praise is
  deliberately not a signal), asks for at most two style edits, and
  **writes nothing**: candidates are re-checked app-side (authority
  lines, agreement-only drift, evidence, consolidation at cap), queued
  in the same database, and surfaced as real `persona_edit` approval
  prompts at the next CLI or window session. With recording off or no
  signals it says so and exits. `docs/PERSONA.md` has the full trust
  model.

### Responsive cancellation

- **Cancel interrupts a provider request in flight** — pressing Cancel
  in the desktop window no longer waits out a slow model reply. On
  the local Ollama path the pending HTTP read is abandoned within
  about a second and the connection closed; an OpenAI-compatible
  request is likewise given up on and its late reply discarded. The
  cancelled turn is still discarded whole, approvals and executing
  actions remain atomic (a running step lands first), and an
  uninterrupted turn behaves exactly as before. Honest caveats:
  server-side generation for an abandoned request stops best-effort,
  and the voice transcription segment remains non-cancellable.

## 1.1.0 — 2026-09-24

### First-run setup and model configuration

- **Setup window on first launch** — with no configuration, `stella-ui`
  shows a welcome dialog offering Local model (Ollama), OpenAI API, or any
  OpenAI-compatible API. No environment variables or terminal commands are
  required.
- **Real model discovery** — the Local model path lists the models actually
  installed in Ollama via its own API (with Refresh, empty-state guidance,
  and clear messages for an unavailable server); Stella never downloads
  models and never hardcodes a model list.
- **Test-before-use** — *Start Stella* unlocks only after a successful
  connection test, and editing the configuration invalidates a previous
  test. Settings gained the same Test connection and a live
  "Provider / Model / Status" line, plus masked session-only API key entry.
- **Saved configuration** — provider, model and endpoints persist in a
  private `config.json` under the XDG data directory; the CLI shares the
  same startup path. API keys are deliberately not persisted (no secure
  credential store); use `OPENAI_API_KEY` to keep one across launches.
  Environment variables keep working and override the saved file.

### Stella informs while idle

- **Idle reminder firing** — the desktop window now checks for due
  reminders on its own every few seconds; a reminder comes to you even
  if you send no message. Delivery stays exactly-once (an atomic store
  transition, shared across ticks, turns and processes), the check runs
  on the same single worker thread as everything else, and the CLI
  keeps its fires-on-next-interaction behaviour by design.

### Reviewable approvals

- **Content-aware approval previews** — approving a file change now
  shows what actually changes: a unified diff for edits, the bounded new
  content for writes (with a warning if the create would fail), the
  beginning of the content a delete would lose, and the validated URL
  for network reads. Previews are computed by the application from
  already-validated arguments only, are bounded and honestly labelled
  when clipped, and are never part of the authorization token — the
  verified post-execution receipt remains the ground truth. Shown in
  both the CLI prompt and the Tk approval dialog.

### Durable action history

- **What Stella did survives restarts** — the dispatcher's bounded
  audit trail moved from a process-memory deque into a small SQLite
  history file (`stella_action_history.db`, override with
  `STELLA_HISTORY_DB`). Records stay metadata-only: capability,
  redacted argument summaries, risk, approval outcome, result and the
  tool's verified receipt — never file contents or tool output. The
  retention of the newest 256 entries is enforced on every append, and
  a new History tab in the desktop window lists recent actions,
  newest first.
- **`network_read` receipts** — every fetch attempt now ends with a
  `fetch` receipt: `verified` with the byte count on success, or an
  honest `failed`/`invalid` outcome otherwise, recorded alongside the
  validated URL in the history.

### Working feedback and cancel

- **Elapsed-time feedback** — while a turn runs, the desktop status
  line counts seconds ("Stella is working · 12 s") so a slow local
  model reads as slow, not broken.
- **Cooperative cancel** — a Cancel button asks the running turn to
  stop at its next safe point: between steps only. An in-flight
  provider request, an open approval prompt and an executing action
  are never interrupted; they land, and the turn stops right after.
  A cancelled turn is discarded whole from the conversation; nothing
  half-taken is claimed as done. Cancelling while an approval is open
  denies that action fail-closed and dismisses the dialog, so it can
  never be an accidental bypass. The CLI keeps Ctrl+C as its
  immediate-stop equivalent.
- **Per-turn duration in the transcript** — every finished turn now
  ends with how long it took ("Stella: … (took 47 s)"), so slow
  answers from a local model read as slow rather than broken. The
  duration is display metadata only; the stored conversation never
  carries it.

## 1.0.0 — 2026-09-23

First public release of Stella, a local-first desktop assistant for Linux.

### Capabilities

- **Conversational assistant** with a Tkinter desktop UI (`stella-ui`) and
  a terminal CLI (`stella`), driven by one shared application layer.
- **User-controlled memory** — facts are stored only after you approve the
  proposal, listed/pinned/forgotten through the Memory panel or chat.
- **Safe computer actions** — create/edit/read/delete files and search the
  workspace, strictly bounded to Stella's workspace directory.
- **Independently verified outcomes** — every action result is re-checked
  against the real world by the application; responses distinguish verified
  (✓), failed/denied (✗) and inconclusive (?) outcomes.
- **One-shot reminders** — user-approved reminders that display their note
  when due; reminder content can never execute tools.
- **Basic voice interface** (optional) — one-shot microphone input
  transcribed through a command you configure, and spoken playback of
  replies; no wake word, no continuous listening.

### Trust architecture

- The model only proposes; a fixed dispatcher validates every tool call
  against hard-coded risk levels the model cannot influence.
- Dangerous actions (file writes/edits/deletes, memory changes, reminder
  changes, network reads) require a single-use human approval; stale,
  replayed or forged approvals fail.
- Malformed model decisions fail closed to doing nothing.
- Local-first by default: without an `OPENAI_API_KEY`, Stella targets a
  local Ollama server; no account or telemetry.

### Known limitations

- Requires an external language model (Ollama or an OpenAI-compatible API);
  Stella ships no model itself.
- Reminders are one-shot and only fire while Stella is running.
- Voice depends on user-configured transcription/TTS commands; a broken
  voice command disables only voice.
- File actions are workspace-bounded by design (no arbitrary shell access).
- Tool-choice quality depends on the configured model; small local models
  occasionally propose the wrong tool (or none). Stella then fails closed
  to doing nothing and never acts without your approval.
