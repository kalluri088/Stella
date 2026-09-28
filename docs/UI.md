# Stella Desktop UI

## What it is

`stella.ui` is a small Tkinter window over the same Stella core the CLI uses.
It is an interface, not a second Stella: every request flows through the shared
application layer in `stella.app` and then through the existing trusted
`Stella`, `ToolDispatcher`, `Memory`, and reminder boundaries. The UI holds no
decision, authorization, or execution logic of its own.

## Launching

```bash
uv sync
uv run stella-ui
```

A display is required. No environment variables are needed: on first launch
`stella.config.resolve_settings()` finds no saved configuration and the
window opens a **setup dialog** instead of a broken application. The dialog
offers Local model (Ollama), OpenAI API, and any OpenAI-compatible API; for
Ollama it scans the models actually installed (Ollama's `/api/tags`, never a
hardcoded list), and every choice must pass **Test connection** before
*Start Stella* unlocks. Completing setup saves the non-secret
provider/model/endpoint fields to `config.json` (private file mode under the
Stella data directory), so the next launch goes straight to the chat window.

`STELLA_MODEL`, `STELLA_LLM_PROVIDER`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`
and `OLLAMA_BASE_URL` keep working for advanced users and win over the saved
file. An API key entered in the UI is never persisted — Stella has no
secure credential store, so the key lives only in that process's environment
(set `OPENAI_API_KEY` to keep it across launches).

## Architecture

```text
Tk window (stella.ui)
  -> StellaBridge (stella.app)        # command queue in, event queue out
    -> worker thread "stella-app"     # owns the SQLite connections
      -> StellaApplication / StellaSession (stella.app)
        -> Stella core (brain, dispatcher, memory, reminders)
```

SQLite connections are bound to the thread that opened them, so the whole
application is built and used on one daemon worker thread. The UI thread never
touches `Stella`, tools, memory, or the database directly. It posts commands
(`post_turn`, `post_memories`, `post_reminder_add`, ...) and a 100 ms poll
drains `UiEvent` values (`turn`, `memories`, `reminders`, `settings`,
`reminder_delivered`, `notice`, `error`) back into widgets. All friendly
error text is produced by the bridge; failures never surface as raw
exceptions.

The CLI (`uv run stella`) builds the identical `StellaApplication` through
`build_application(settings)` using the same `stella.config.resolve_settings`
startup path, and shares turn handling with the UI through `StellaSession`.
`stella.config` also holds the bounded setup probes (Ollama model scan,
connection test): they perform fixed HTTP requests, treat every response —
including model names — as data to be sanitized and length-capped before
display, and can never approve a tool or change permissions.

## What the UI can do

- Conversation: send multi-line messages (Ctrl+Return or Send), see replies,
  honest error lines, and continuous history within the window session.
  While a turn runs, the status line counts elapsed seconds
  ("Stella is working · 12 s") so a slow local model reads as slow, not
  broken, and a **Cancel** button appears beside Send. Cancel stops the
  turn at the next safe point between steps and — since A7 — also
  interrupts a provider request that is still in flight: the native
  Ollama wait is abandoned within about a second and the connection
  closed, and an OpenAI-compatible request is likewise given up on
  (reply discarded). It never interrupts an approval prompt or an
  executing action, and the cancelled turn is discarded whole from the
  conversation. A cancel while an approval dialog is open denies that
  action (fail-closed) and dismisses the dialog; nothing executes.
  Every finished turn also ends with how long it took in the transcript
  ("Stella: … (took 47 s)") — display-only, the stored conversation
  never carries that text.
  The CLI keeps Ctrl+C as its immediate-stop equivalent.
- Approvals: a dialog shows "Stella wants to: <capability and arguments>"
  for every `DANGEROUS` action, with Allow and Cancel, plus a read-only
  app-computed preview (diff, new content, loss excerpt or validated URL)
  as review material only — the answer still binds solely to the exact
  `ApprovalRequest`.
- Memory: list stored memories, keyword-search them, and forget the selected
  one. The list shows content only; internal database ids never appear and
  forgetting works by visible list position through `MemoryPanel`.
- Reminders: list pending one-shot reminders with due times, create one from
  content plus an ISO due time, and cancel by query text — all by executing
  the same trusted reminder tools the CLI uses. An open window also fires
  due reminders unprompted: a ticker posts a reminder check onto the
  worker thread every few seconds, so an idle Stella still informs (the
  CLI keeps firing on the next interaction by design).
- History: a section lists what Stella recently did — capability, time and
  honest outcome, newest first — from the durable action history, so
  earlier sessions are visible too. Entries are metadata only; file
  contents and tool output never appear.
- Action outcomes: each tool result is rendered with the honest status from
  `outcome_status` (verified ✓, unverified ✗, inconclusive ?, failed ✗,
  denied ✗). An unverified action is never shown as verified.
- Settings: change provider/model, the Ollama endpoint, an OpenAI-compatible
  base URL, and (masked, session-only) an API key, with *Test connection*,
  *List models* for Ollama, and a live
  "Provider: … Model: … Status: Connected / Not connected" line. Apply
  rebuilds the application through `build_application` before anything is
  saved, so a failed rebuild keeps the working session alive and can never
  overwrite the last known-good configuration. Two extra checkboxes —
  *Record transcripts for persona reflection* (off by default; see
  `PERSONA.md`) and *Semantic memory recall (embedding index)* (off
  by default; see `ARCHITECTURE.md`) — turn the two optional local files
  (transcript, semantic index) on or off. Beside the recall checkbox a
  small choice selects the embedding provider: *Local word-shape (no
  model)* (the default), *Ollama embedding model*, or *MiniLM
  (stella[embed] extra)*; picking a provider whose backend is missing
  fails the rebuild honestly and keeps the working session. The semantic
  index duplicates
  every stored memory's text into its own file; while unchecked, that
  file is never created.
- Persona: chat style changes go through the normal `persona_edit`
  approval dialog with a real diff. If `stella reflect` has queued style
  proposals, the window shows each one as an ordinary approval prompt
  shortly after launch (never during startup) — deny and nothing changes.
- Voice: press **Listen** to record one explicit utterance, see the transcript
  enter the same conversation path as typed input, and optionally hear the
  final response spoken ("Speak replies", off by default). A voice turn is
  cancellable end to end: **Cancel** beside Listen works while transcribing,
  and **Cancel** beside Send also silences any spoken audio. See `VOICE.md`
  for providers, privacy behavior, and controls.

## Appearance and themes

The window is built around a full-height **navigation rail** (Chat,
Memories, Reminders, History, Settings) instead of the old side
notebook, so every panel uses the full window; the active section is
lifted onto the content surface in the accent color. Chat is a framed
transcript card with roomy speech bubbles and a card composer whose
buttons stack beside the input; the other sections are header + card
layouts. While a turn runs the status line shows a quiet braille
spinner after the elapsed seconds — display only.

Two complete minimalist palettes — **dark** (the default) and
**light** — are defined once as `Theme` dataclasses in `stella.ui`
and read by every widget builder through the module-level `THEME`.
The toggle sits at the bottom of the rail and switches live: the ttk
styles are re-applied (they repaint all styled widgets), the few plain
Tk widgets (transcript, composer, the three lists, any open approval
dialog and its preview box) are recolored explicitly, and nothing is
rebuilt — a running turn, a recording, or a pending approval is never
interrupted by a theme change.

The choice persists in a one-word `ui-theme` file next to
`config.json` under the Stella data directory and is loaded at
launch, so the window opens the way the user last left it; an absent
or unreadable file falls back to dark. This file holds only the
palette name — no configuration, no secrets, no conversation — and
writing it can never affect authorization: approvals, transcripts
and the bridge contract are styling-independent.

## How approvals stay trusted

`ToolDispatcher` still decides what requires approval and validates it. When a
dangerous action is proposed, the dispatcher's approval provider is
`ApprovalBroker.request`, called on the worker thread. The broker wraps the
dispatcher's own `ApprovalRequest` object under a numeric token, queues it, and
blocks until the UI answers. The window presents the request and calls
`resolve_approval(token, approved)`; the broker then returns a `ToolApproval`
constructed around the original request, so exact-match validation in the
dispatcher behaves identically to the CLI path.

The UI cannot approve an action the dispatcher did not request: unknown or
stale tokens are rejected, closing the dialog or the window counts as a denial,
and shutdown denies every unanswered request. A denial follows the existing
core path and reports that nothing was changed.

## Current limits

- Single window, single worker thread; one conversation per process.
- Conversation history lives in this session only; there is no persistent
  chat log (the durable History is action metadata, not chat). The opt-in
  transcript file records turn text for persona reflection only — off by
  default, and never read back into a conversation (`PERSONA.md`).
- Memory search is the existing deterministic keyword matcher, not semantic.
- Cancel interrupts an in-flight provider request (native Ollama: the
  wait is abandoned within about a second; OpenAI-compatible: the reply
  is discarded) and stops a turn at the next safe point between steps.
  It cannot abort an executing action mid-way — the running step lands
  first — and server-side generation for an abandoned Ollama request
  stops best-effort, not with a guarantee.
- Cancel reaches the whole voice periphery: a running local transcription
  or synthesis command is killed within about a second, playback stops,
  and audio finished around a cancel is discarded instead of played.
  Honest residue: an abandoned cloud transcription or speech request is
  given up without closing its socket (the result is never used), and a
  partial cloud speech file is cleaned up when Stella exits.
- Voice is one explicit utterance per Listen press with local-first providers;
  there is no wake word, continuous listening, or streaming recognition.
- No browser automation, email/calendar, cloud accounts, plugins, or remote
  integrations.
