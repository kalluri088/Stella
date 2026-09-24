# Stella Roadmap

This is the whole-project plan: the standing principles every feature must
honor, the staged work that turns Stella into a dependable daily assistant,
and the boundaries that will deliberately never be crossed. Design
rationale lives in the dedicated documents linked below; this file only
orders the work and states what enforces each rule in code.

## Standing principles

Each principle is a property of the running system, enforced by a named
mechanism — not an aspiration.

1. **The model proposes; the app decides.** The LLM emits `Decision`s;
   only the application-owned `ToolDispatcher` executes anything, and
   only for capabilities registered by trusted code
   (`src/stella/tools.py`, `src/stella/brain.py`; see
   `docs/TOOL_DISPATCHER.md`).
2. **Memory is selective and user-controlled.** Nothing is stored without
   an approval whose `ApprovalRequest` matches the exact content, and
   tool outputs report honestly (`src/stella/memory.py`;
   `docs/OUTCOME_MEMORY.md`).
3. **Awareness is not authority.** Proactivity and reminders run on a
   notify-only runtime path that never consults the Brain, the LLM or
   the dispatcher, and reminder text is treated as untrusted data
   (`src/stella/proactivity.py`, `src/stella/reminders.py`;
   `docs/REMINDERS.md`).
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

## Stage A — a functional daily assistant (complete — released in 1.1.0)

The bar for Stage A: "I should be talking, asking, and it should be
working and helping me, even if not the fastest."

- **A1. Idle reminder firing — done.** The UI bridge runs a daemon
  ticker whose only action is posting a reminder check onto the
  single-worker-thread command queue; delivery stays once-only via the
  atomic `pending → handled` update. The CLI keeps fires-on-next-
  interaction by design. (`ReminderScheduler` in `src/stella/app.py`,
  `docs/REMINDERS.md`.)
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
- **A6. Per-turn duration UX — done.** Every finished turn is timed by
  the session (`TurnOutcome.duration_seconds`) and the transcript
  renders it ("Stella: … (took 47 s)"), so slow answers read as
  "local model", not "broken". The duration is display-only; the
  stored conversation never carries it.
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

## Stage B — sharper judgment and scale

- **B0. System-1 decision router (Laya) — design sketch only, no
  code.** A small non-autoregressive classifier (Laya 0.3.20,
  421M) consulted *before* the language-model Brain, so cheap
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
    its VRAM. (2) Re-validate VRAM coexistence with whatever
    ships as the brain config. (3) Routing-question conformance
    and calibration on Stella-shaped inputs — ties directly into
    the B4 provider conformance suite.
- **B1. Argument-aware risk classification.** *Explicitly deferred by
  the user.* Today risk is per-capability and hard-coded; a classifier
  would have to be proven not to weaken the exact-match approval
  boundary before it earns a place.
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
- **B3. Memory-scale policies.** As stored memory grows: bounded
  retrieval windows, dedupe guidance, and forgetting tools that stay
  approval-gated.
- **B4. Provider conformance suite.** One test harness that any
  OpenAI-compatible endpoint or Ollama model must pass, so provider
  swaps are verified rather than hoped for.

## Stage C — personality

Stella's character becomes durable, editable data — while the trust
model stays exactly as in Stage A: the model proposes, the app decides,
approvals bind to exact arguments. Personality may change *style* only;
risk levels, tool registration and dispatch remain application-owned
code no text file can influence (`src/stella/persona.py`,
`src/stella/tools.py`, `docs/PERSONA.md`).

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

## Packaging and release

- The repository has no git remote yet; the first push happens only when
  the user provides a remote URL.
- The `v1.0.0` tag stays where it is. Versions follow semver per
  milestone: **1.1.0 = Stage A complete**, 1.2.0 = Stage B, and so on.
- Every release gets a `CHANGELOG.md` section written from the user's
  point of view, and work lands as one commit per deliverable.

## Explicitly out of scope

Not planned, in any stage, unless this section is deliberately rewritten:
MCP integration, a plugin system, an autonomous agent loop, multi-user
accounts, cloud hosting, mobile apps, wake-word detection,
always-listening audio, speaker identification, emotion detection, a
model marketplace, automatic large-model downloads, self-modifying
behavior and self-learning beyond the approval-gated style notes of
Stage C, Kubernetes/Docker orchestration, arbitrary shell access, and
unrestricted computer control.
