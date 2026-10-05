# `stella voice` — hands-free voice for a keyboard shortcut

A no-screen voice mode in two shapes. The plain command runs **one** turn:
press, speak, answer, exit — nothing stays resident. `--serve` is the resident
shape: the saved wake ear stays armed, and a wake phrase or a `--toggle` opens
a **conversation** — turn after turn with no button between them — until
silence, a spoken stop, or the key again. Both shapes run from the
most-recently-saved `config.json` (`resolve_settings`), the same settings the
desktop app uses.

## The flow

1. **Press the shortcut.** For one turn the command is `stella voice`; the
   shortcut on this machine (**Super + D** in `~/.config/hypr/bindings.lua`)
   now runs `stella voice --toggle`, which talks to the resident server
   described below (and starts it if none is up). It launches as
   `/usr/bin/uv run --project <repo> stella voice --toggle` because this
   machine is still bound to the development tree and the installed
   `stella` console script was not on the launcher's PATH when the line was
   written — an installed release is bound by absolute path instead
   (`~/.local/bin/stella voice --toggle`, which is what `install.sh` prints;
   see `docs/RELEASE.md`). Both shapes run
   from the latest saved `config.json` (`resolve_settings`) — the same
   settings the desktop app uses.
2. **A short chime** says *I'm listening*. (Two tones, generated with the
   standard library — no asset files, no new dependency.)
3. **You speak.** One shared microphone handle is opened for the utterance; a
   local VAD silence-watcher (`WakeEndpoint`, ~800 ms of trailing silence) ends
   the capture automatically — no second button to press, matching the
   push-to-talk path already used in the desktop panel.
4. **A second chime** says *working*, then the transcript runs through the
   **identical** `StellaSession.run_turn` as a typed message: same history,
   memory, Brain decisions, dispatcher risk checks, and receipts. Voice grants
   no extra authority.
5. **The answer is spoken**, and the process tears itself down
   (`application.close()`).

## Approving risky work — by voice, fail-closed

When the turn wants a dangerous action, Stella **asks out loud**: it speaks the
capability and a *bounded* one-line summary — never the raw arguments, so a big
file body is not read to the room — then listens for your answer.

- A clear **"yes"** (yes / yeah / sure / go / do it) approves.
- **"no"**, a garbled answer, or **silence** during that window **denies**.

Approvals fail closed: if in doubt, nothing dangerous happens. You can also
interrupt a turn; cancellation is honoured exactly as in the desktop app.

## Natural conversation: `--serve` and `--toggle`

`stella voice --serve` builds the same application with the **saved wake
choice honoured** — that continuous ear is exactly what the owner asked for.
Shell and browser stay forced off (a spoken approval must not smuggle them
in), and desktop window control is not a setting at all, so "open Chromium
in workspace 1" still answers out loud. The loop:

1. **Arm the wake ear.** Saying the chosen phrase rings the opening bell —
   the ear's only power, the same as one Listen press.
2. **Open a conversation:** chime, capture, run the turn, speak the answer —
   then re-open the ears for the follow-up automatically. No button between
   turns; that is the whole point.
3. **End on silence**, on a spoken "stop" / "never mind" / "that's it", or
   on the shortcut again — and return to the idle ear. The always-open
   spotter is suspended while Stella talks (she must never wake on her own
   voice) and re-armed when she is idle.

`stella voice --toggle` is what the keyboard shortcut runs (Super+D). With
no server up it starts one detached and then presses the same word; with a
server up it opens a conversation, or closes the live one. The two talk
through one word over a private unix socket (`0600`, under
`XDG_RUNTIME_DIR`, named `stella-voice-<uid>.sock`): the protocol carries
only `toggle` and `stop` — a doorbell, never a control channel. A server
that started and failed writes its honest reason to `.log` beside the
socket, so a dead key is diagnosable.

`stella voice --stop` shuts the resident server down; a foreground
`--serve` also exits on Ctrl-C.

The doorbell is a unix socket, and a platform without one has nothing to
ring: `control_path()` answers `None`, every probe reads that as "no
server, and there cannot be one", and `--serve` says so and exits `3`
before warming a speech model. `stella doctor` reports *"no control
socket on this platform"* rather than crashing on a call that does not
exist there — it is the command the installer tells every user to run, so
being unable to answer is an answer, not an error.

## Honest failure, distinct exit codes

A missing peripheral is never a silent success.

| Code | Meaning |
| --- | --- |
| `0` | ran a turn (or conversation), or honestly heard nothing ("I didn't catch that.") |
| `2` | no saved configuration yet — set Stella up once in the app first |
| `3` | configured, but voice input or speech output isn't available |
| `4` | build/VAD failure (e.g. the local silence-detection model is missing), or a resident server that never came up |
| `5` | `--serve` only: a live control socket already belongs to another server — it exits instead of hijacking the microphone |

If you get `3`, enable voice input and "Speak replies" once in the desktop
Settings, then the key will work.

## Constraints it respects

- **One microphone handle.** It opens a single `MicTap` shared by the
  recorder and the silence-watcher — and, in the resident shape, by the
  wake listener too — so a hotkey press never double-opens the device
  that the desktop app may already be using.
- **Nothing secret is spoken or printed.** No key, and no transcript, is
  echoed to the terminal; command/file bodies are never read aloud. The
  control socket carries no payload beyond `toggle` and `stop`.
- **No new dependency.** It is pure orchestration over the existing voice,
  wake and tool primitives.

## Validation

`tests/test_headless_voice.py` drives the whole path against fakes — honest
exit codes when there is no voice, a heard request runs exactly one spoken turn
and disposes its artifact, the silence endpoint ends a capture, a dead mic
degrades to "didn't catch that", the approver approves only a clear "yes" and
denies no/garbage/silence, and the file-write body is never read aloud. The
resident shape adds its own proofs: the loop chains turns with no re-press and
re-arms the ear between sessions, the spotter is suspended while Stella
speaks, only a whole-utterance dismissal closes a conversation, the control
handler moves events and nothing else, the socket answers one word and is
0600, a second server exits rather than hijacking, and `--toggle` starts a
missing server exactly once.

## See also

`docs/VOICE.md` (voice in the desktop app), `docs/SHELL_TOOLS.md` (what a
voice-approved `shell_run` does), `docs/HEADLESS_VOICE.md` is this file.
