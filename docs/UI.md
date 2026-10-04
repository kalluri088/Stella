# Stella Desktop UI

## What it is

`stella.ui` is a small Tkinter window over the same Stella core the CLI uses.
It is an interface, not a second Stella: every request flows through the shared
application layer in `stella.app` and then through the existing trusted
`Stella`, `ToolDispatcher`, and `Memory` boundaries. The UI holds no
decision, authorization, or execution logic of its own.

## Launching

```bash
uv sync
uv run stella-ui
```

A display is required. No environment variables are needed: on first launch
`stella.config.resolve_settings()` finds no saved configuration and the
window opens a **setup dialog** instead of a broken application. The dialog
is a provider-preset picker (Ollama and llama.cpp local modes, then OpenAI,
Claude, Grok, Groq, OpenRouter, Gemini and "Other OpenAI-compatible"); for
Ollama it scans the models actually installed (Ollama's `/api/tags`, never a
hardcoded list), hosted presets arrive with their endpoint and suggested
models prefilled, and every choice must pass **Test connection** before
*Start Stella* unlocks. Completing setup saves the non-secret
provider/preset/model/endpoint fields to `config.json` (private file mode
under the Stella data directory), so the next launch goes straight to the
chat window.

`STELLA_MODEL`, `STELLA_LLM_PROVIDER`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`,
`STELLA_PRESET` and `OLLAMA_BASE_URL` keep working for advanced users and
win over the saved file. An API key that passes its connection test is
stored in `api_keys.json` (owner-only, mode 0600) in the same directory and
reused in every later session; it is never written into `config.json`, never
shown again in full (only a last-four hint), and the UI performs no
environment writes at all. See `docs/SECRETS.md`.

## Architecture

```text
Tk window (stella.ui)
  -> StellaBridge (stella.app)        # command queue in, event queue out
    -> worker thread "stella-app"     # owns the SQLite connections
      -> StellaApplication / StellaSession (stella.app)
        -> Stella core (brain, dispatcher, memory)
```

SQLite connections are bound to the thread that opened them, so the whole
application is built and used on one daemon worker thread. The UI thread never
touches `Stella`, tools, memory, or the database directly. It posts commands
(`post_turn`, `post_memories`, `post_forget`, ...) and a 100 ms poll
drains `UiEvent` values (`turn`, `memories`, `memory_result`, `settings`,
`notice`, `error`) back into widgets. All friendly
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

- Conversation: send multi-line messages (Enter sends, Shift+Enter adds a
  line, or use the Send button), see replies,
  honest error lines, and continuous history within the window session.
  Up/Down arrows in the composer recall the messages this window sent,
  newest first, like a terminal: they only fire when the cursor is on
  the first (Up) or last (Down) line, so multi-line editing keeps
  normal cursor keys, and pressing Down past the newest entry restores
  the draft the recall started from.
  A large paste collapses to a colored chip in the composer —
  `[pasted 12 lines · 3,400 chars]` — so a wall of text (a log, a file,
  a stack trace) never blows up the three-line input box or the
  transcript. A paste becomes a chip at **4 or more lines, or 400 or
  more characters**; anything smaller is inserted verbatim. The chip is
  a display label only (not click-to-expand) and is purely decorative:
  the **full pasted text is kept in the window and sent to Stella
  unchanged** when the message is posted, and it is what slash-command
  templates and the model actually see. The transcript renders the same
  chip (its own violet tag, shared with the composer) rather than the
  raw blob, and recalling a sent message re-applies the chip so re-sending
  still expands to the complete text. Deleting part of a chip breaks that
  one chip's match and the leftover is sent literally — the chips are
  plain editable text, a deliberate Tk trade-off over non-atomic widgets.
  While a turn runs, the status line counts elapsed seconds
  ("Stella is working · 12 s") so a slow local model reads as slow, not
  broken; once the runtime validates a tool call, the line upgrades to
  name the capability ("Stella is calling memory list · 7 s") — the
  application's own knowledge, never model text — and a **Cancel**
  button appears beside Send. Cancel stops the
  turn at the next safe point between steps and — since A7 — also
  interrupts a provider request that is still in flight: the native
  Ollama wait is abandoned within about a second and the connection
  closed, and an OpenAI-compatible request is likewise given up on
  (reply discarded). It never interrupts an approval prompt or an
  executing action, and the cancelled turn is discarded whole from the
  conversation. A cancel while an approval dialog is open denies that
  action (fail-closed) and dismisses the dialog; nothing executes.
  Every finished turn also ends with how long it took and when it
  finished in the transcript ("… (took 47 s · 14:32)", in Stella's
  plain reply) — display-only, the stored conversation never carries
  that text.
  The CLI keeps Ctrl+C as its immediate-stop equivalent.
- Approvals: a dialog shows "Stella wants to: <capability and arguments>"
  for every `DANGEROUS` action, with Allow and Cancel, plus a read-only
  app-computed preview (diff, new content, loss excerpt or validated URL)
  as review material only — the answer still binds solely to the exact
  `ApprovalRequest`.
- Memory: list stored memories, keyword-search them, and forget the selected
  one. The list shows content only; internal database ids never appear and
  forgetting works by visible list position through `MemoryPanel`.
- No reminder surface. Stella keeps no reminder store, so there is nothing to
  list, schedule or cancel from the window: a "remind me" is written into the
  connected Outline workspace and that application owns the alert
  (`REMINDERS.md`). What the window does keep is the delivery path — an idle
  ticker asks Outline for the reminders this process can claim, and each claim
  appears in the chat as an amber alert line. It is a read, not a turn: it
  cannot reach the Brain, the LLM or any tool, and it is silent when no
  Outline server is configured.
- History: a section lists what Stella recently did — capability, time and
  honest outcome, newest first — from the durable action history, so
  earlier sessions are visible too. Entries are metadata only; file
  contents and tool output never appear.
- Action outcomes: each tool result is rendered with the honest status from
  `outcome_status` (verified ✓, unverified ✗, inconclusive ?, failed ✗,
  denied ✗). An unverified action is never shown as verified.
- Settings: change the provider preset (which fixes provider, endpoint and
  key slot), the model, the Ollama endpoint, and — for "Other
  OpenAI-compatible" only — a base URL; the API key field is masked and
  stores a matching, verified key on Apply (a key shaped like a different
  provider's is refused with a hint), with *Test connection*,
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
  file is never created. A separate
  *Wake word (always-open microphone)* box, also off by default, arms the
  local detection ear the moment Apply rebuilds and stops that capture
  when unticked — the privacy contract, the models it needs and the
  `STELLA_WAKE_WORD` override are in `VOICE.md`.
- Capability families: the Settings panel carries one checkbox per opt-in
  tool family — *Desktop awareness* (on by default, but still doubly gated),
  *Outline app tools*, *Web capability*, *Browser (open a page, run its
  scripts)*, and *Shell commands (run programs in your workspace)* — each
  mirrored by a `STELLA_OS_TOOLS` / `STELLA_OUTLINE` / `STELLA_WEB` /
  `STELLA_BROWSER_TOOLS` / `STELLA_SHELL_TOOLS` environment override that wins
  for a single launch in either direction. A checked box only offers the tools;
  every dangerous call still raises an approval prompt naming the exact target
  (and, for the shell, the literal command; for the browser, the literal
  address). The browser capability reuses the shell's `STELLA_SHELL_SANDBOX`
  jail and, when bubblewrap is present, wraps the headless browser in it; the
  separate `STELLA_BROWSER` variable is a *path* to the browser binary to
  drive, not an on/off switch. The web row also has a masked
  **TinyFish key** box: the value is stored in the same private `0600` key file
  as model keys, shown only as a redacted hint, and never written to
  `config.json` — an exported `TINYFISH_API_KEY` still overrides it for that
  launch. See `WEB.md`, `BROWSER.md` and `SHELL_TOOLS.md`.
- Persona: chat style changes go through the normal `persona_edit`
  approval dialog with a real diff. If `stella reflect` has queued style
  proposals, the window shows each one as an ordinary approval prompt
  shortly after launch (never during startup) — deny and nothing changes.
- Voice: press **Listen** to record one explicit utterance, see the transcript
  enter the same conversation path as typed input, and optionally hear the
  final response spoken ("Speak replies", off by default). A voice turn is
  cancellable end to end: **Cancel** beside Listen works while transcribing,
  and **Cancel** beside Send also silences any spoken audio. The dot beside
  the controls is red for as long as the microphone is really open —
  including while a wake-word ear is armed with no button pressed — and
  **Mute mic** puts every ear down for the session without touching the
  speakers. See `VOICE.md` for providers, privacy behavior, and controls.

## Appearance and themes

The window is built around a full-height **navigation rail** (Chat,
Memories, History, Settings) instead of the old side
notebook, so every panel uses the full window; the active section is
lifted onto the content surface in the accent color. Chat is a framed
transcript card that separates roles the way a terminal does — the
user's message is a full-width `>` blockquote band, Stella answers in
plain left-aligned text with no label — and a card composer whose
buttons stack beside the input; the other sections are header + card
layouts. While a turn runs the status line shows a quiet braille
spinner after the elapsed seconds — display only.

Two complete palettes — **dark** (the default) and **light** — are
modelled on the Outline app Stella integrates with (the CSS custom
properties served by the app at `127.0.0.1:8741`): dark mode
`#141210` background, `#1F1C1A` panels, `#33302D` lines, stone text
grays and teal accent `#2DD4BF`; light mode `#FAFAF9` background,
white panels, `#E7E5E4` lines and teal `#0D9488`. The user's quote
band is `#292524` / `#E7E5E4` (Stella's reply carries no band),
warnings amber `#D97706`. They are defined once as `Theme`
dataclasses in `stella.ui` and read by every widget builder through the
module-level `THEME`.
The toggle sits at the bottom of the rail and switches live: the ttk
styles are re-applied (they repaint all styled widgets), the few plain
Tk widgets (transcript, composer, the two lists, any open approval
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
- Voice is one explicit utterance per Listen press — or per wake phrase,
  with local-first providers; the wake word is a way to press the button
  with your voice, not an open conversation. There is no streaming
  recognition, and no speaker identification: the ear answers whoever
  says the phrase.
- No *interactive* browser automation (clicking, typing, a persistent driving
  session) — the opt-in browser capability is a one-shot headless render only
  (`BROWSER.md`). No email/calendar, cloud accounts, plugins, or remote
  integrations.
