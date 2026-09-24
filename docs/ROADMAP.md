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
  checked only between steps (before a Brain decision, immediately
  after one returns, and right after a dispatched step lands), so an
  in-flight provider request, an approval prompt or an executing
  action is never interrupted mid-way. A cancelled turn is discarded
  whole from the conversation; a cancel with an approval still open
  denies it fail-closed (`Stella.process(should_cancel=...)` and
  `StellaBridge.cancel_current_turn` in `src/stella/app.py`,
  `docs/UI.md`). The CLI keeps Ctrl+C as its immediate-stop
  equivalent by design.
- **A6. Per-turn duration UX — done.** Every finished turn is timed by
  the session (`TurnOutcome.duration_seconds`) and the transcript
  renders it ("Stella: … (took 47 s)"), so slow answers read as
  "local model", not "broken". The duration is display-only; the
  stored conversation never carries it.

## Stage B — sharper judgment and scale

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
