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
  non-cancellable blocking segment.

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
- **B2. Semantic memory retrieval.** The `LocalHashEmbeddingProvider`
  exists but is unwired (`src/stella/semantic_memory.py`); connect it to
  memory recall with honest relevance reporting, no silent ranking
  magic.
- **B3. Memory-scale policies.** As stored memory grows: bounded
  retrieval windows, dedupe guidance, and forgetting tools that stay
  approval-gated.
- **B4. Provider conformance suite.** One test harness that any
  OpenAI-compatible endpoint or Ollama model must pass, so provider
  swaps are verified rather than hoped for.

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
model marketplace, automatic large-model downloads, self-modifying or
self-learning behavior, Kubernetes/Docker orchestration, arbitrary shell
access, and unrestricted computer control.
