# Changelog

## Unreleased

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
