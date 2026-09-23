# Stella

Stella is a local-first desktop assistant for Linux. You talk to it, it
proposes what to do, and **nothing that changes your system happens without
your explicit approval**. Memory, reminders and conversations stay in your
own account directory, and the model can run entirely on your machine.

## Why Stella exists

Most assistants send your data to a service and act on your machine with
permissions you never granted. Stella inverts that: the language model only
*suggests* actions, a trusted application layer checks every suggestion
against a fixed tool list and risk level, and a dangerous action runs only
after you approve exactly that action, in a dialog you control.

## What Stella v1 can do

- **Chat** through a desktop window (Tkinter) or the terminal.
- **Answer with model knowledge**, or read workspace files to answer with
  real content (reads need no approval; fetching a URL does).
- **Remember facts you ask it to remember.** Memory is user-controlled:
  proposals appear as approvals, you can view and forget entries at any time.
- **Create, edit and delete files inside its workspace** — each one requires
  your approval, and every result is *independently verified* on disk before
  Stella claims success.
- **Set one-shot reminders** ("remind me in 10 minutes…"). When due, Stella
  shows the note; reminders can never execute tools.
- **Optional voice**: press the mic button, speak, and your words are
  transcribed into the same chat you'd type into; replies can be spoken.
- **Fail honestly.** Outcomes are reported as verified (✓), failed or denied
  (✗), or inconclusive (?) — Stella never presents an unverified action as
  done.

## How the trust model works

1. The **Brain** (your configured model) returns a structured decision:
   answer, use a tool, propose a memory write, or propose a reminder.
   A malformed decision does nothing.
2. The **ToolDispatcher** looks the tool up by exact name, validates its
   arguments, and applies a hard-coded risk level the model cannot override.
3. **Dangerous actions** — file writes, edits and deletes, memory changes,
   reminder creation or cancellation, and any network read — pause and ask
   *you* for approval. The approval token is single-use and tied to the
   exact pending request; stale or forged approvals fail.
4. **Verification is application-controlled**: after an action, Stella
   re-checks the real world (does the file exist, with those bytes?) and
   reports what it observed — the model's words never mark an action done.

Tool output, file contents, memory text and reminder text are all treated as
data. None of them can grant authority. Details:
`docs/APPROVAL_BOUNDARY.md` and `docs/ARCHITECTURE.md`.

## Requirements

- Linux with Python **3.12 or newer**.
- The desktop UI needs Tkinter: install your distro's `python3-tk`
  (Debian/Ubuntu), `tk` (Fedora), or `python-tkinter` equivalent.
- A language model. Stella is **not self-contained** — it has no built-in
  model. Choose one:
  - **Local (recommended): [Ollama](https://ollama.com)** running on your
    machine, with any ChatML-capable model pulled, e.g. `qwen3:4b`.
  - **Or an OpenAI-compatible API** (including a local server that speaks
    that API) — enter the key in the setup window, or set `OPENAI_API_KEY`.

## Install

Download `stella-1.0.0-py3-none-any.whl` from the v1.0.0 release and install
it with [uv](https://docs.astral.sh/uv/) (or pipx):

```bash
uv tool install ./stella-1.0.0-py3-none-any.whl
```

To install from source instead:

```bash
git clone <this repository> && cd Stella
uv build
uv tool install ./dist/stella-1.0.0-py3-none-any.whl
```

This exposes two commands: `stella-ui` (desktop window) and `stella` (CLI).

## Configure

**No environment variables are required.** On first launch `stella-ui` shows
a welcome dialog asking how Stella should run:

1. **Local model (Ollama)** — Stella checks that Ollama is reachable, lists
   the models you actually have installed (via Ollama's own model API, never
   a hardcoded list), and lets you pick one. *Refresh* rescans; if nothing is
   installed it tells you how to add one, e.g. `ollama pull qwen3:4b`
   (Stella never downloads models for you). If Ollama is not running it says
   so and how to start it (`ollama serve`).
2. **OpenAI API** — type your key into the masked field, test the connection,
   done. The full key is never shown again after entry.
3. **Other OpenAI-compatible API** — same, plus a base URL field.

Every choice ends with **Test connection**; *Start Stella* only unlocks after
a test succeeded, and the resulting provider/model/endpoint configuration is
saved under `~/.local/share/stella/config.json` (private file mode, no
secrets). The next launch goes straight to the chat window. Change provider
or model later in the window's **Settings** tab, which offers the same
fields, a *Test connection* button, and a live
"Provider: … Model: … Status: …" line.

**Limitation (deliberate):** Stella has no secure credential store, so an
API key entered in the UI lives only for that session — it is never written
to disk. To keep an OpenAI key across launches, set `OPENAI_API_KEY` in your
environment (an advanced-user option, not a requirement for Ollama users).

### Advanced: environment variables

Environment settings keep working and always win over the saved file — useful
for scripts, development, and multiple configurations:

| Variable | Meaning | Default |
| --- | --- | --- |
| `STELLA_MODEL` | Model name; also forces first-run setup to be skipped | unset |
| `STELLA_LLM_PROVIDER` | `ollama` or `openai` | saved config, else auto |
| `OPENAI_API_KEY` | OpenAI-compatible API key (persisted across sessions) | unset |
| `OLLAMA_BASE_URL` | Ollama server URL | `http://127.0.0.1:11434` |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | provider default |
| `STELLA_WORKSPACE` | folder for file actions | `~/.local/share/stella/workspace` |
| `STELLA_MEMORY_DB` / `STELLA_REMINDERS_DB` | state file locations | under `~/.local/share/stella` |
| `STELLA_VOICE_TRANSCRIPTION` / `STELLA_VOICE_SPEECH` | voice on/off/auto | `auto` |

The CLI (`stella`) uses the same saved configuration; with no config and no
`STELLA_MODEL` it prints a short message pointing at `stella-ui` instead of
failing silently.

Voice is configured last and only if wanted — see `docs/VOICE.md`. A broken
voice command disables just that voice feature; the rest of Stella keeps
working.

## Launch

```bash
stella-ui    # desktop window
stella       # terminal chat
```

In the desktop window: type and press **Enter** (or use *Listen* for voice).
When Stella proposes a dangerous action, an approval dialog appears —
**Enter approves, Escape denies**, and the result line tells you what was
actually verified.

## Using memory

Ask: *"Remember that my Wi-Fi password convention is X."* Stella proposes a
memory write, which appears as an approval — you decide. Use the **Memory**
panel to list what is stored, pin entries into context, or forget them.
Forgetting is immediate and only happens because you asked. Memory lives in
a plain SQLite file you own.

## How file actions work

Stella can read, create, edit and delete files **only inside its workspace**
(`STELLA_WORKSPACE`). Writes and deletes require your approval; afterwards
the status line reports whether the change was *verified* on disk. Reject a
request and the workspace stays untouched — Stella says so.

## How reminders work

*"Remind me in 20 minutes to take the pan off the stove."* A one-shot
reminder is created and listed in the **Reminders** panel; you can cancel it
any time before it is due. When due, Stella displays the reminder text in
the conversation. Reminders are notification-only: their content never
executes anything, and Stella does not run in the background while closed.

## How voice works

Voice is one-shot, not always-on. There is no wake word and Stella never
listens continuously. You press the button, speak, and the recording is
transcribed by a command you configure (e.g. `whisper.cpp`, an OpenAI key
for automatic transcription, or any stub that prints text). Speech output
plays back a rendered reply with a command such as `espeak-ng`. Recording
files are deleted as soon as they are transcribed. Details and examples:
`docs/VOICE.md`.

## Where Stella stores data

Everything lives under your XDG data directory
(`~/.local/share/stella`, or `$XDG_DATA_HOME/stella`):

- `config.json` — your provider/model choice from setup (no secrets,
  private file mode)
- `stella_memory.db` — your memory entries
- `stella_reminders.db` — pending/completed reminders
- `workspace/` — the only place file actions can touch

No telemetry, no cloud sync. With the local Ollama setup, nothing leaves
your machine.

## Uninstall

```bash
uv tool uninstall stella      # removes the app
rm -rf ~/.local/share/stella  # only if you also want the data gone
```

## Supported platforms

Linux is the supported, tested platform (the Tk UI is Linux-first). The
logic is plain Python, so the CLI generally works on macOS too, but that is
not what v1 promises.

## Current limitations

- An external model provider (Ollama or an API) is required — Stella ships
  no model of its own.
- One-shot reminders only; nothing recurring, and Stella must be running
  when a reminder comes due.
- File actions are workspace-bounded by design; there is no arbitrary
  shell access.
- Voice quality depends entirely on the transcription/TTS commands you
  configure; with `auto` and no API key, voice input is simply unavailable.
- The desktop UI is a single functional window, not a polished product
  surface; no multi-user support.
- Response quality depends on the model you configure. Small local models
  occasionally propose the wrong tool (or none); Stella then fails closed
  to doing nothing rather than acting on a guess, and never performs an
  action without your approval.

## Source & problems

Source: this repository (`src/stella`, tests under `tests/`, design notes
under `docs/`). Please open an issue with what you did, what you saw, and
the status line Stella printed — outcome wording matters to us.
