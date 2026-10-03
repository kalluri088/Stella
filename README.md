# Stella

Stella is a personal assistant for your Linux desktop that runs on **your**
machine and keeps **your** data. You chat with her like a person: ask
questions, request file edits, store things to remember, schedule an alert
in the notes app you already use. She
can run entirely offline with a local model.

The rule she never breaks: **nothing that changes your system happens without
your explicit approval.** And she never claims something worked unless she
checked that it did.

## What she can do (as of 1.4.0)

- **Chat** — a desktop window or the terminal, with your choice of model.
- **Remember** — say "remember that …" and she asks first; the Memories tab
  shows exactly what is stored, and you can delete anything, any time.
- **Find related memories (opt-in)** — the Settings checkbox (or
  `STELLA_SEMANTIC_MEMORY=1`) adds a local embedding index, so a memory
  can surface because its words look alike even when your exact keywords
  miss. Keyword matches always keep first place, at most two extra hints
  are added, and each is labeled with how it was found — a similarity
  score, never a claim of understanding. The default index is a
  zero-dependency word-shape fingerprint; you can opt into a real
  embedding model instead (`STELLA_SEMANTIC_PROVIDER=ollama` for a local
  Ollama model, or `minilm` with the optional `stella[embed]` extra,
  CPU-only). Both stay local, and if the model server is unreachable
  Stella says so and answers from keyword recall alone. (With Ollama the
  embedding model coexists with your chat model, but the first call after
  a long idle can take a few seconds while it reloads.) Off by default,
  and while off the index file is never created.
- **Remind** — "in 20 minutes, tell me to take the pan off the stove." Stella
  has no notifier of her own: she writes that as a task or event carrying a
  remind time in your connected Outline workspace, and **Outline** is what
  alerts you. If no workspace is connected she says she cannot schedule it,
  rather than pretending she did.
- **Work with files** — read, create, edit files inside her workspace folder.
  Before anything is written, you see a real diff or the exact new content
  and approve or cancel it. Afterwards she re-reads the disk and reports
  whether the change was actually verified.
- **Look things up** — answer questions from file content, check the time,
  fetch a web page for you to read (after asking, with the URL shown). A
  TinyFish web key, if you use one, is stored in the same private key file as
  model keys (Settings → the *TinyFish key* box); an exported
  `TINYFISH_API_KEY` still wins for that launch, and the key never lands in
  `config.json` or a log.
- **Talk and listen** — press the mic button, speak; replies can be spoken
  back on a local voice. Neither side needs a cloud service: the transcriber
  and the synthesizer already on the laptop are found automatically and named
  on screen. There is also a **no-screen one-shot**: `stella voice` runs a
  single hands-free turn from your latest settings and exits — ideal behind a
  keyboard shortcut (it is wired to **Super + D** here). It auto-stops on a
  short pause, cues listening/working with a chime, and confirms anything risky
  **out loud, failing closed** on any unclear answer (`docs/HEADLESS_VOICE.md`).
  Continuous hands-free listening is off by default: the wake
  word — the *Wake word* box in Settings, whose answer is saved, or
  `STELLA_WAKE_WORD=on` for a single launch — is the only way Stella ever
  holds the microphone open while you are not speaking to her. A red dot
  says whenever it is open, and *Mute mic* puts every ear down for the
  session; `uv sync --extra wake` and local wake models are what the ear
  needs to exist at all.
- **Use your desktop (opt-in)** — with one setting on, Stella can read the
  focused Hyprland window, focus or move windows, and type into the window
  you nominate. Everything else about her is unchanged: the screen-wide
  read asks first, and nothing leaves your machine.
- **Personality** — a written persona (`~/.config/stella/persona.md`) you
  create with `stella persona` or a preset (`snark`, `warm`, `terse`), and
  can change in chat: she proposes a diff, you approve it. Ask to "be
  drier" and that is what happens — nothing persona-related writes itself
  behind your back. Every replacement keeps a snapshot, so a change you
  regret is one `stella persona revert` away from undone.
- **Learn her style (opt-in)** — with transcript recording on,
  `stella reflect` turns your observed friction ("too long", cancelled
  rambles) into at most two style proposals, surfaced as approval
  prompts. Reflection never writes on its own. See `docs/PERSONA.md`.
- **Opt-in tool families** — four gates, each a Settings checkbox or an
  environment variable: Outline (`STELLA_OUTLINE`) lets her create, search
  and update your tasks over Outline's local API; desktop
  (`STELLA_OS_TOOLS`, Hyprland only) lets her read the screen, focus
  windows and type; web (`STELLA_WEB`) adds search and fetch; and shell
  (`STELLA_SHELL_TOOLS`) lets her **run one command you approve by name**,
  starting in your workspace, output and time bounded — off by default and
  asking on every single use (see `docs/SHELL_TOOLS.md`; it is a confined
  start directory plus your approval, not a filesystem jail). Every one
  of their calls is still an approval prompt.
  See `docs/WEB.md`, `docs/SHELL_TOOLS.md` and the capability sections of
  `docs/ARCHITECTURE.md`.
- **Show her work** — a History tab lists what she recently did, kept
  between launches (names and outcomes only, never file contents). The
  terminal prints the same durable trail with `stella audit` —
  `--last N`, `--capability`, `--outcome denied`, or `--json` (details in
  `docs/AUDIT_LOGGING.md`). While a
  turn is running you see it working with elapsed seconds, and a Cancel
  button stops a turn that is taking too long.

## Why you can trust what she says

Stella splits "decide" from "do":

1. The language model only *suggests* an action.
2. A trusted app layer checks the suggestion against a fixed tool list and
   hard-coded risk levels the model cannot change.
3. Anything that could alter your system pauses and asks **you** — approval
   is tied to the exact action shown; approving one thing can never run
   another.
4. After acting, Stella verifies the result in the real world (does the file
   exist, with those bytes?) and reports what she *observed* — verified,
   failed, or inconclusive. She never presents an unverified claim as done.

Tool output, file contents and memory text are treated as data. None of them
can grant themselves permission.

## What you need

- Linux with Python 3.12+ (the window needs Tkinter: `python3-tk` on
  Debian/Ubuntu, `tk` on Fedora).
- A language model — Stella ships none of her own:
  - **Local (recommended):** [Ollama](https://ollama.com) with a chat model
    pulled, e.g. `ollama pull qwen3:4b`. Nothing leaves your machine.
  - **Or a local llama.cpp brain:** point Stella at a downloaded `.gguf`
    file and she starts and stops `llama-server` herself — no server to
    manage (provider `llama`, needs llama.cpp's `llama-server` on `PATH`
    or `STELLA_LLAMA_SERVER_BINARY`).
  - **Or any OpenAI-compatible API** (including a local server).

## Install

From a release: download the `.whl` file and run

```bash
uv tool install ./stella-1.4.0-py3-none-any.whl
```

From source:

```bash
git clone https://github.com/kalluri088/Stella && cd Stella
uv build
uv tool install ./dist/stella-1.4.0-py3-none-any.whl
```

This gives you two commands: `stella-ui` (desktop window) and `stella`
(terminal chat, plus subcommands like `stella voice` for one hands-free turn,
`stella persona` and `stella reflect`). Above that, three optional extras exist:
`stella[embed]` adds a CPU MiniLM embedding model you can select for
semantic recall instead of the default word-shape index;
`stella[barge-in]` adds the ONNX runtime that lets you interrupt spoken
replies; `stella[web]` adds keyless DuckDuckGo search for the web
capability (with a TinyFish key the web tools need no extra).
To remove Stella: `uv tool uninstall stella`, and delete
`~/.local/share/stella` if you also want your data gone.

## First launch

`stella-ui` opens a short setup dialog — no environment variables needed:

1. Pick a provider: **Local model (Ollama)**, **Local llama.cpp**,
   **OpenAI**, **Claude (Anthropic)**, **Grok (xAI)**, **Groq**,
   **OpenRouter**, **Google Gemini**, **FreeLLMAPI (local router)**,
   or **Other OpenAI-compatible**.
2. Stella lists the Ollama models you actually have installed; hosted
   providers arrive with their endpoint and suggested models prefilled
   (still editable). Paste your API key into the masked field — if it is
   visibly a different provider's key shape, Stella says so instead of
   sending it anywhere.
3. Press **Test connection**; the window only continues after a real
   connection worked. A key that passed the test is then stored in your
   private Stella data directory (`api_keys.json`, readable only by your
   user) and used in every later session — you never paste it again, and
   it is never shown again either, only its last four characters.

Your provider/model choice is saved under `~/.local/share/stella/` and can
be changed later in the window's Settings tab, which shows a live
"Connected / Not connected" status. Switching there takes effect on the
very next message — no restart, and the conversation and memory carry
over. **FreeLLMAPI** is the preset for a self-hosted
[FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) router pooling
free-tier keys on your machine; it expects the router at
`localhost:3001` and its locally generated `freellmapi-…` unified token.

### Advanced: environment variables (optional)

Environment settings always win over the saved file — useful for scripts,
development, or running several configurations side by side:

| Variable | Meaning | Default |
| --- | --- | --- |
| `STELLA_MODEL` | Model name (for `llama`: full path to a `.gguf` file); also skips first-run setup | unset |
| `STELLA_LLM_PROVIDER` | `ollama`, `openai` or `llama` (Stella-owned llama-server) | saved config, else auto |
| `OPENAI_API_KEY` | Overrides the stored key for the OpenAI slot (and a custom endpoint) for this launch | unset |
| `STELLA_PRESET` | Which provider preset the OpenAI key and endpoint come from (e.g. `anthropic`) | saved config |
| `OLLAMA_BASE_URL` | Ollama server URL | `http://127.0.0.1:11434` |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | provider default |
| `STELLA_LLAMA_SERVER_BINARY` | `llama-server` command for the `llama` provider | `llama-server` |
| `STELLA_LLAMA_SERVER_PORT` | Port for the Stella-owned brain | `8080` |
| `STELLA_WORKSPACE` | Folder file actions may touch | `~/.local/share/stella/workspace` |
| `STELLA_MEMORY_DB` / `STELLA_HISTORY_DB` | State file locations | under `~/.local/share/stella` |
| `STELLA_PERSONA_DIR` | Persona file location | `~/.config/stella` |
| `STELLA_TRANSCRIPTS` | Transcript recording on/off (`1`/`0`; overrides the saved setting) | off |
| `STELLA_TRANSCRIPT_DB` | Transcript file location | `~/.local/share/stella/stella_transcript.db` |
| `STELLA_SEMANTIC_MEMORY` | Semantic memory recall on/off (`1`/`0`; overrides the saved setting) | off |
| `STELLA_SEMANTIC_PROVIDER` | Recall index: `local-hash`, `ollama` or `minilm` (`stella[embed]` extra) | `local-hash` |
| `STELLA_EMBED_MODEL` | Ollama embedding model name | `nomic-embed-text` |
| `STELLA_SEMANTIC_DB` | Semantic index file location | `~/.local/share/stella/stella_semantic_index.db` |
| `STELLA_OS_TOOLS` / `STELLA_OUTLINE` / `STELLA_WEB` / `STELLA_SHELL_TOOLS` | Opt-in tool families (`1`/`0`; overrides the saved checkbox) | off (OS tools on) |
| `STELLA_VOICE_TRANSCRIPTION` / `STELLA_VOICE_SPEECH` | Voice on/off/auto | `auto` |
| `STELLA_TRANSCRIPTION_ENGINE` | Engine name handed to the detected `voxtype` STT CLI (an engine, not a model size) | `whisper` |
| `STELLA_TRANSCRIPTION_TIMEOUT` | Seconds a cloud transcription request may take before it is abandoned (`0`–`600`) | `30` |
| `STELLA_SPEECH_LOCAL_VOICE` / `STELLA_SPEECH_LOCAL_SPEED` | Voice name and rate asked of a resident speech worker (extra request keys; a worker may ignore them) | unset |
| `STELLA_TRANSCRIPTION_COMMAND` / `STELLA_TRANSCRIPTION_MODEL` | A local transcriber to run instead of the detected `voxtype` CLI (a `{input}` template, run without a shell) / the OpenAI transcription model when the cloud path is chosen | unset / `whisper-1` |
| `STELLA_SPEECH_COMMAND` / `STELLA_SPEECH_MODEL` / `STELLA_SPEECH_VOICE` | A local synthesizer (a `{text}` + `{output}` template that writes one audio file) / the OpenAI speech model and voice | unset / `tts-1` / `alloy` |
| `STELLA_SPEECH_RESIDENT` | Keep one speech process warm between sentences so a local voice answers in milliseconds instead of re-loading its model on every phrase (`VOICE.md`) | off |
| `STELLA_VOICE_BARGE_IN` / `STELLA_BARGE_SOURCE` / `STELLA_BARGE_THRESHOLD` / `STELLA_VAD_MODEL` | Interrupt Stella by speaking: `auto`/`on`/`off`, the capture source it reads (an echo-cancelled one is what makes it usable), how speech-like a frame must look, and where the small VAD model file is | `auto` / unset / `0.5` / found automatically |
| `STELLA_WAKE_MODEL` / `STELLA_WAKE_MODEL_DIR` / `STELLA_WAKE_SOURCE` / `STELLA_WAKE_THRESHOLD` | Which openWakeWord classifier answers for the wake ear and where it is looked for, the capture source it reads, and how sure it has to be before it counts as a phrase | `hey_jarvis_v0.1.onnx` / `~/models/openwakeword` / unset / `0.5` |
| `STELLA_WAKE_WORD` | Hands-free wake word (`on`/`off`) for this launch only; the *Wake word* box in Settings is what is saved (`wake_word_enabled`), and this variable wins over it either way. Arms the always-open detection ear, which needs the `wake` extra and models under `~/models/openwakeword`. There is deliberately no `auto`. | off |
| `STELLA_DECISION_MAX_TOKENS` / `STELLA_ANSWER_MAX_TOKENS` | Per-call-kind output-token caps: the decision call and the answer call each stop decoding at their budget (`0` removes the cap) | `8192` / `2048` |
| `STELLA_OLLAMA_THINK` | Force Ollama hybrid reasoning (`qwen3`-class models) on/off (`1`/`0`); unset keeps the model's own default | unset |

Normal users never need any of these. One environment fact no setting
can fix: Ollama shares the machine's GPU with everything else, and turn
times roughly double while another GPU job runs (report 35) — that is a
scheduling choice, not a Stella configuration.

## Using the window

- Type your message, press **Enter** to send (**Shift+Enter** adds a line).
- Approvals show what will really happen — a diff, the new content, or the
  URL — with **Allow** and **Cancel**. Closing the dialog means deny.
- **Cancel** during a working turn stops it: it also interrupts a provider
  request that is still in flight (the wait is abandoned within about a
  second), but never an action that has started executing — that step
  lands first. A cancelled turn is discarded and never remembered as
  having happened. (In the terminal, Ctrl+C does the same job.)
- A voice turn is cancellable end to end: **Cancel** next to Listen drops
  a recording that is still being transcribed (nothing is sent to Stella),
  and **Cancel** also silences audio that is being spoken. Speech
  produced around a cancel is discarded, never played.
- What Stella says aloud is the reply's words, not its formatting: markdown
  marks, table pipes and emoji are stripped before any voice engine sees the
  text, and a link is spoken by its label ("a link" for a bare address)
  because you cannot open one with your ears. Nothing is paraphrased or
  summarised on the way.
- When "Speak replies" is on, a due Outline alert is said as well as shown —
  one at a time, never over a reply, and never when speech is off.
- The tabs manage **Memories**, **History** and **Settings**.

### Slash commands

A line that starts with `/` is a command Stella's interface handles
itself — it never reaches the model, and saying "/exit" out loud is
still just a sentence. Built in: `/exit`, `/status` (what Stella is
connected to and where your data lives), `/help`, `/version`,
`/clear` (forget this session's conversation — stored memories and the
action trail are untouched), `/history` (the most recent action
records), and in the terminal `/trace on|off` and `/debug on|off`. You can also write
your own: a Markdown file named `~/.config/stella/commands/plan.md`
becomes `/plan`, and whatever you type after the command replaces
`$ARGUMENTS` in it (or is appended, if the file has no token). An
expanded template is treated exactly like a message you typed out in
full — nothing more, nothing less; dangerous actions still ask. An
unknown `/name` says so and suggests near matches.

## Where your data lives

All in `~/.local/share/stella` (or `$XDG_DATA_HOME/stella`): the config
file, your verified API keys (`api_keys.json`, stored so only your user
can read it, never printed and never part of a backup), your memories,
and the action history, and her workspace
folder — plain files you own. Her persona files are yours too, under
`~/.config/stella`. The conversation transcript (used only for style
reflection) is off by default and, when you turn it on, is one bounded
local file; so is the semantic memory index, which keeps a second local
copy of each memory's text alongside its vector — computed locally, by the
word-shape hash, by your local Ollama server, or by the CPU MiniLM model.
No
telemetry, no accounts, no cloud sync.
With the local Ollama setup, nothing ever leaves your machine.
`stella backup <dir>` snapshots exactly these files — every state database
stays consistent even while Stella runs — plus the config (never the key
file); `stella restore
<dir>` confirms before replacing anything and keeps what it displaced in a
`pre-restore-*` directory. `stella verify-backup <dir>` answers "will this
backup restore cleanly" by integrity-checking the archive read-only,
without touching the live state.

## Honest limits (no marketing here)

- Local models are slow and sometimes pick the wrong tool — Stella then
  fails closed to doing nothing rather than guessing. The window shows how
  long each turn took so "slow" never looks like "broken".
- Stella keeps no reminders of her own. There is no reminder store, no
  reminder panel and no reminder tool: "remind me to X at 18:00" is an alert
  written into the notes app you connect, and that app is what rings. Stella
  does still wake the desktop window on an interval to ask that app what is
  due — a read that becomes one line of chat, with no tool call and no model
  turn behind it — and she only does it while the notes app is actually
  configured. A remind alert there reaches only you, so "remind the team" is
  something she will ask about rather than fake.
- File actions are limited to her workspace on purpose. There is no shell
  access and no "control my computer" mode.
- Voice quality depends on the transcription/speech tools you install.
- The UI is a single functional window — dependable, not fancy.
- API keys are stored as private plaintext on your machine
  (`api_keys.json`, mode 0600) — protected by file permissions, not by a
  keyring or encryption. Backups never carry them, and they never appear
  in logs or config; `OPENAI_API_KEY` still overrides the OpenAI slot for
  one-off launches.

## Under the hood (optional reading)

- `docs/ARCHITECTURE.md` — how the pieces fit together
- `docs/APPROVAL_BOUNDARY.md` — why approvals cannot be tricked
- `docs/SECRETS.md` — where API keys live, how they are verified, and
  what is deliberately not done
- `docs/PERSONA.md` — personality as data: trust tiers, filters, and the
  opt-in reflection loop
- `docs/ROADMAP.md` — what is done, what is next, what is deliberately out
  of scope (plugins, autonomous agents, cloud accounts…)
- `tests/` — 1400+ tests; every "done" claim in this README is checked by
  one

## Found a problem?

Open an issue with what you did, what you saw, and the status line Stella
printed. How outcomes are worded matters here: if she says "verified", it
must mean she actually verified it.
