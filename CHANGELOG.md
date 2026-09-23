# Changelog

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
