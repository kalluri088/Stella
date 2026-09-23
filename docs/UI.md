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
`reminder_delivered`, `error`) back into widgets. All friendly error text is
produced by the bridge; failures never surface as raw exceptions.

The CLI (`uv run stella`) builds the identical `StellaApplication` through
`build_application(settings)` using the same `stella.config.resolve_settings`
startup path, and shares turn handling with the UI through `StellaSession`.
`stella.config` also holds the bounded setup probes (Ollama model scan,
connection test): they perform fixed HTTP requests, treat every response —
including model names — as data to be sanitized and length-capped before
display, and can never approve a tool or change permissions.

## What the UI can do

- Conversation: send multi-line messages (Ctrl+Return or Send), see replies,
  a visible "Stella is thinking..." state, honest error lines, and continuous
  history within the window session.
- Approvals: a dialog shows "Stella wants to: <capability and arguments>" for
  every `DANGEROUS` action, with Allow and Cancel.
- Memory: list stored memories, keyword-search them, and forget the selected
  one. The list shows content only; internal database ids never appear and
  forgetting works by visible list position through `MemoryPanel`.
- Reminders: list pending one-shot reminders with due times, create one from
  content plus an ISO due time, and cancel by query text — all by executing
  the same trusted reminder tools the CLI uses. No scheduler is added: due
  reminders are delivered when the next turn is processed, same as the CLI.
- Action outcomes: each tool result is rendered with the honest status from
  `outcome_status` (verified ✓, unverified ✗, inconclusive ?, failed ✗,
  denied ✗). An unverified action is never shown as verified.
- Settings: change provider/model, the Ollama endpoint, an OpenAI-compatible
  base URL, and (masked, session-only) an API key, with *Test connection*,
  *List models* for Ollama, and a live
  "Provider: … Model: … Status: Connected / Not connected" line. Apply
  rebuilds the application through `build_application` before anything is
  saved, so a failed rebuild keeps the working session alive and can never
  overwrite the last known-good configuration.
- Voice: press **Listen** to record one explicit utterance, see the transcript
  enter the same conversation path as typed input, and optionally hear the
  final response spoken ("Speak replies", off by default). See `VOICE.md` for
  providers, privacy behavior, and controls.

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
- History lives in this session only; there is no persistent chat log.
- Memory search is the existing deterministic keyword matcher, not semantic.
- Reminders are user-created one-shots delivered during interaction; there is
  no background delivery.
- Voice is one explicit utterance per Listen press with local-first providers;
  there is no wake word, continuous listening, or streaming recognition.
- No browser automation, email/calendar, cloud accounts, plugins, or remote
  integrations.
