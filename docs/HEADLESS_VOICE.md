# `stella voice` — one hands-free turn for a keyboard shortcut

A no-screen voice mode. You press a key, Stella runs **one** turn from your
most-recently-saved settings — speak in, speak out, and the actual work in
between — then the process exits. Nothing stays resident between presses.

## The flow

1. **Press the shortcut.** The command is `stella voice`. On this machine it is
   wired to **Super + D** in `~/.config/hypr/bindings.lua`, which launches it
   as `/usr/bin/uv run --project <repo> stella voice` because the `stella`
   console script is not on the launcher's PATH. It runs from the latest saved
   `config.json` (`resolve_settings`) — the same settings the desktop app uses.
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

## Honest failure, distinct exit codes

A missing peripheral is never a silent success.

| Code | Meaning |
| --- | --- |
| `0` | ran a turn, or honestly heard nothing ("I didn't catch that.") |
| `2` | no saved configuration yet — set Stella up once in the app first |
| `3` | configured, but voice input or speech output isn't available |
| `4` | build/VAD failure (e.g. the local silence-detection model is missing) |

If you get `3`, enable voice input and "Speak replies" once in the desktop
Settings, then the key will work.

## Constraints it respects

- **One microphone handle.** It opens a single `MicTap` shared by the recorder
  and the silence-watcher and releases it, so a hotkey press never double-opens
  the device that the desktop app may already be using.
- **Nothing secret is spoken or printed.** No key, and no transcript, is echoed
  to the terminal; command/file bodies are never read aloud.
- **No new dependency.** It is pure orchestration over the existing voice, wake
  and tool primitives.

## Validation

`tests/test_headless_voice.py` drives the whole path against fakes — honest
exit codes when there is no voice, a heard request runs exactly one spoken turn
and disposes its artifact, the silence endpoint ends a capture, a dead mic
degrades to "didn't catch that", the approver approves only a clear "yes" and
denies no/garbage/silence, and the file-write body is never read aloud.

## See also

`docs/VOICE.md` (voice in the desktop app), `docs/SHELL_TOOLS.md` (what a
voice-approved `shell_run` does), `docs/HEADLESS_VOICE.md` is this file.
