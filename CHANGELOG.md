# Changelog

## Unreleased

### Run a command for you: the `shell_run` capability

- **Stella can now run one shell command, and it is the most heavily fenced
  tool she has.** A new `shell_run` capability runs a single command through
  the platform shell inside the Stella workspace and returns its combined
  output and exit status — the assistant's `Bash`/`sandbox` equivalent. It is
  **off by default** (`shell_tools_enabled`, mirrored by `STELLA_SHELL_TOOLS`),
  and its floor is `RiskLevel.DANGEROUS`, so the dispatcher asks the owner to
  approve the **literal command** before anything runs — every single use. Four
  fences back it up: a confined starting directory, closed stdin (never waits
  on a terminal), a 64 KB output cap read without buffering the excess, and a
  120 s wall-clock timeout that takes the **whole process group** down
  (`SIGINT→SIGTERM→SIGKILL`), using the existing `stella.childproc`
  parent-death guarantee so a killed command cannot outlive Stella.
- **"Sandbox" is stated honestly, not oversold.** This is a confined start
  directory plus a human who approves the exact command — *not* kernel
  isolation. A command runs as you and can still reach anything your own
  account can; `docs/SHELL_TOOLS.md` says so plainly and points at
  container/VM isolation for anyone who wants a real jail. Captured output is
  wrapped in the same `<<<UNTRUSTED_WEB_CONTENT>>>` markers as web fetches and
  defanged against marker forgery, so a build log is data, never a new
  instruction back to the runtime.
- **A Settings checkbox and a drift-guard, like every other family.** The
  capability is wired across all six touchpoints (override, field, `from_saved`
  / `from_environment`, persisted config, checkbox), which
  `tests/test_settings_wiring.py` now enforces for `shell_tools_enabled` too.
  Only the on/off bool is ever written to `config.json` — a command string is
  never persisted, printed, or spoken. `tests/test_shell_tools.py` proves every
  decision through an injected fake runner and then runs the real bounded reader
  against a few tiny, workspace-confined commands to prove the byte cap and the
  group-kill actually fire.
- **The standard agent-tool list, mapped rather than padded.** `docs/SHELL_TOOLS.md`
  records how the requested set maps to Stella today (Read/Write/Edit/Glob/Grep/
  WebFetch/WebSearch/DeliverArtifacts were already covered by existing tools; the
  workspace is the outbox). `ImageGen` and `ImageSearch` are **intentionally not
  added** — both need a paid API key and send prompts off the machine, and Stella
  does not ship a model- or network-dependent behavior it has not validated live.

### `shell_run` gets a real jail: bubblewrap, when it is installed

- **The shell command now runs in a filesystem jail, not just a start
  directory.** On this Arch/Omarchy machine `bubblewrap` (`bwrap`) is already
  present, so `shell_run` launches each approved command through it: your whole
  filesystem is **read-only** inside the jail, the Stella workspace and a
  private scratch `/tmp` are the **only** writable exceptions, your real home is
  masked, privileged supplementary groups (docker/kvm/wheel…) are dropped, and
  PID/mount/IPC/UTS namespaces are isolated. The command still runs as your
  uid inside a user namespace, but it can no longer wander `cd`-ing into
  `~/.ssh` or your project trees to write. Nothing was downloaded or installed —
  `bwrap` is a pre-existing system binary, so Stella gains **no new dependency**.
- **Honest and fail-closed about it.** The approval card now says which state it
  is in: an *active jail* warning, or an *unavailable* warning ("the jail is NOT
  active") when `bwrap` is missing or user namespaces are off, and every result
  run without the jail carries a prelude saying so — the guard never silently
  pretends isolation it does not have. Switch it off with
  `STELLA_SHELL_SANDBOX=0`; there is **no config field for the jail** (a single
  env knob, so it never needs the six-touchpoint settings wiring). A new
  per-command `network` argument (default on) can drop `--share-net` to block a
  command's network for that one use. The command string is still carried as a
  trailing positional argument and re-executed via `sh -c "$1"`, never
  interpolated into the wrapper, so it cannot forge jail flags.
- **Proven against the real thing.** `tests/test_sandbox.py` checks the argv
  shape (read-only host bind, home mask ordered before the workspace re-bind,
  network toggle, `clearenv` + a fixed `PATH`/`HOME`, positional command) and
  runs one `bwrap` echo for real; `tests/test_shell_tools.py` adds `TestSandboxJail`
  (active/unavailable/off routing) and a `TestRealJail` that, when the jail is
  available, proves the host is read-only, home is masked, and `~/.ssh` is not
  reachable — each still workspace-confined and sub-second.



- **`stella voice` — one hands-free turn for a keyboard shortcut.** A new
  no-screen mode runs a single voice turn from your most-recent saved settings,
  then exits; nothing stays resident between presses. A chime says *listening*,
  a local VAD silence-watcher (~800 ms) ends the capture so you never press a
  second button, a second chime says *working*, the turn goes through the
  **identical** `run_turn` path as typed input, and the answer is spoken.
  Anything risky is confirmed **out loud and fails closed**: only a clear "yes"
  approves, while "no", a garbled answer, or silence deny. It speaks a bounded
  summary — never raw arguments, so a big file body is not read to the room.
  Missing peripherals are honest, distinct exit codes, never a silent success
  (`docs/HEADLESS_VOICE.md`). Behind a shortcut, `Super + D` is wired in
  `~/.config/hypr/bindings.lua` to launch it through the project's `uv`
  environment.
- **A TinyFish web key can live in Settings now.** Web tools previously read the
  key only from `TINYFISH_API_KEY`; you can also type it into the Settings web
  row, where it is stored in the same private `0600` atomic key file as model
  keys (a new named-secret store in `stella.provider_keys`), shown only as a
  redacted hint, and excluded from backup. **The environment still wins for a
  launch**, and the key never reaches `config.json`, a log, or a notice. The
  private file now carries a `secrets` map beside the model keys, each
  preserving the other on every write, and an older key file with no `secrets`
  reads back cleanly. `tests/test_provider_keys.py` and `tests/test_config.py`
  cover the round-trip, the redaction, the env-overrides-store precedence, and
  that a stored key stays out of the saved configuration.

### Stella can open a real web page: the headless browser capability

- **Two capabilities that run a page's JavaScript.** `web_fetch` never executes
  scripts, so a page drawn by JavaScript comes back nearly empty. Behind a new
  opt-in gate (`STELLA_BROWSER_TOOLS`, a Settings checkbox, off by default) Stella
  can now drive an **already-installed** Chromium-family browser headlessly:
  `browser_read` loads one https URL, lets it render, and returns the visible
  DOM text as untrusted data; `browser_screenshot` loads one URL and writes a
  bounded PNG into the workspace, reporting the path. **No dependency added and
  nothing downloaded** — it only runs a browser the machine already has
  (probed on `PATH`, or pointed at with `STELLA_BROWSER`), and answers a
  structured "browser is off" when there is none.
- **Fenced for the largest attack surface Stella can touch.** It is `DANGEROUS`,
  so every single use stops for you to approve the literal address, and four
  runtime fences hold regardless: only a vetted **public https** URL is opened
  (the `web_fetch` scheme/credentials checks, plus localhost/`.local`/`.internal`
  refusals and a DNS resolve that rejects any non-global answer); Chromium is
  started with `--host-resolver-rules` mapping loopback, RFC1918, link-local,
  CGNAT, `.local`/`.internal` and the cloud-metadata name to `~NOTFOUND`, closing
  the DNS-rebinding redirect a one-time resolve cannot; a **throwaway profile and
  isolated `HOME`** mean the page never rides your real logins or history; and the
  same bubblewrap jail that guards `shell_run` wraps the browser when present
  (`--no-sandbox` only *inside* the jail, so outside it Chromium keeps its own
  sandbox). Renders are wall-clock bounded, the whole process group is taken down
  on timeout via the parent-death guarantee, DOM bytes and screenshot size are
  capped, and rendered text is wrapped and defanged exactly like fetched content.
- **Honest about what the jail does not mean.** It shrinks the blast radius of
  loading a hostile page; it is not a promise against a browser zero-day — which
  is why the capability is off by default and asks every time. `docs/BROWSER.md`
  says so, and records what is deliberately **not** done: no interactive
  click/type automation, no analysis of screenshot pixels. A `STELLA_BROWSER`
  path is never read as the on/off toggle — the capability switch is the distinct
  `STELLA_BROWSER_TOOLS`.
- **Proven without a browser, and one render against the real thing.**
  `tests/test_browser_tools.py` drives every decision through injected
  `find`/`render` seams (URL and DNS refusals, the three jail states and their
  warnings, `--no-sandbox`-only-in-jail and the bwrap wrap, the host-resolver
  rules, the throwaway profile, timeout/truncation/over-cap shaping, marker-forgery
  defang, the "browser is off" result, config round-trip and env override both
  ways) and, where a browser is installed, runs one bounded local-file render
  through the real renderer to prove the byte cap and clean teardown.

### A small bug-and-latency pass (behaviour-preserving, each test-verified)

- **The headless-voice chime stopped racing itself.** `_chime` used the
  deprecated `tempfile.mktemp` and reopened the path with `wave.open`, which
  follows a pre-created symlink; it now uses `mkstemp` and writes the WAV
  straight through the returned descriptor (the atomic-write pattern already
  used in `provider_keys`). Covered by the existing end-to-end chime test.
- **`browser_read` no longer hides a vanished DOM file.** `_read_capped` called
  `os.path.getsize` before reading — redundant (the `cap+1` read already proves
  truncation) and a false-negative, because a transient `getsize` error returned
  an empty "not-truncated" result and the tool reported "no readable text"
  instead of failing. The stat is gone and the open error now propagates to an
  honest failure.
- **Memory recall scores each item once.** Both recall paths filtered with
  `_matches_query` (which calls `relevance_score`) then sorted on a key calling
  `relevance_score` again — double tokenization per item. Folded into one
  `_rank_by_relevance` helper with identical match/order semantics; the now
  unused `_matches_query` is removed.
- **Bounded file reads reuse the probe buffer.** For a file larger than the
  64 KiB binary probe, `_read_bounded_text` re-opened the file and re-read a
  shorter prefix from byte 0 than it had already buffered; it now slices the
  existing buffer. A new test covers the past-the-probe truncation branch.
- **`network_read` handles a malformed response.** A server speaking garbage
  makes `getresponse()` raise an `http.client` protocol error (not `OSError`),
  which escaped the tool's own shaping into the dispatcher's blanket handler;
  it now catches `http.client.HTTPException` like `web_tools` does, so every
  fetch failure returns the honest message and receipt.
- **A file preview survives the byte cap cutting a character.** The approval
  preview's `_preview_file_text` reads one 8 KiB window and decodes it strict;
  when the cap landed inside a multi-byte character the decode failed and a
  perfectly good text file showed no preview at all. It now drops up to three
  trailing bytes — only when the read really was truncated — so a partial code
  point recovers while genuinely non-UTF-8 bytes still yield nothing.
- **The kill reaping window is measured on the monotonic clock.** The orphan
  cleanup loop timed its grace period with the wall clock, so an NTP step
  landing mid-shutdown could cut the wait short and report a just-killed
  process as not gone. File-mtime comparisons stay on the wall clock, where a
  real timestamp is what is being compared.
- **Approval summaries show non-ASCII as written.** `outline_tool_summaries`
  and `web_tool_summaries` quoted the subject with `json.dumps`' default
  `ensure_ascii=True`, so an accented or CJK task title, person name, tag or
  search query came back as `\uXXXX` escapes on the approval card. They now use
  `ensure_ascii=False` like the shell and browser summaries already did; the
  quoting still escapes quotes, backslashes and newlines, so a crafted title
  cannot forge an extra line.
- **A literal `{output}` in spoken text stays text.** `CommandSpeechProvider`
  built its argv with two chained `str.replace` calls, so the `{text}` filled
  first could then have a `{output}` inside it substituted with the output wav
  path by the second pass. Both placeholders now fill in one regex pass whose
  callback is never rescanned, so a token in the answer text is read aloud as
  the literal word.
- **Intentionally left alone after checking:** the web budget is a
  *Stella-side* per-hour throttle, not TinyFish's server quota, so charging it
  before the backend is known is correct (and refunding on failure would invite
  a retry storm); the voice subprocess cancel is bounded by the parent-death
  guarantee plus `_cancel_process_tree` on ordered shutdown, so it is not an
  orphan leak; and `config`'s `… is True` reads are the safe idiom — a plain
  `bool()` would wrongly enable a capability for a hand-edited `"false"`.

### Settings panel: honest outcomes, a fairer key check, friendlier defaults

- **The after-Apply label now reflects what really happened.** The panel
  label used to sit frozen on "Restarting Stella with these settings…"
  forever, because the worker's success and error events only wrote to the
  chat transcript and never touched the label. Applying now tracks a
  pending rebuild: when the settings event lands the label turns to
  "Applied — Stella is running with these settings.", and if the rebuild
  fails the label honestly says the restart failed and the previous
  settings still stand. An unrelated error while no apply is pending never
  overwrites the panel.
- **A valid FreeLLMAPI (or similar chat router) key is no longer a false
  "connection failed".** Chat-dialect endpoints often guard or omit
  `/models` while accepting the same key on `chat/completions`, but the
  connection test only fell through to a one-token chat probe on a `404`.
  It now probes chat for a chat-dialect preset on `404` *or* `401/403`, so
  a router whose model list refuses is judged by the endpoint that actually
  answers — and a genuinely bad key still fails the probe, which is what
  gets reported. The picker's stored-key hint matches: a chat-dialect
  `401/403` reads as "can't be checked automatically, use Test connection"
  instead of an alarming "key rejected". OpenAI's responses dialect is
  unchanged and still never probes chat.
- **Desktop awareness starts checked.** The opt-in checkbox is on by
  default for a fresh setup, and a config written before the flag existed
  is treated as on. The capability stays doubly gated — a real windowing
  adapter must be present and every single use still asks for trusted
  approval — so the default only decides whether the tools are offered at
  all. An explicit unchecked choice, or `STELLA_OS_TOOLS=0`, still wins.
- **Enter sends, Shift+Enter adds a line.** The composer follows the
  chat-app convention (numpad Enter included) instead of Ctrl+Enter; the
  hint text and docs moved with it.

### A silent speech artifact stops being a spoken turn

- **`ResidentSpeechProvider` now checks what the worker actually wrote.**
  A worker that reports `ok` but produces a zero-byte body, a bare RIFF
  header, a sub-10 ms file, or a full-length file of samples below
  audibility used to slip through with "the file exists at the path we
  chose", play as nothing, and leave the UI dotting `speaking` while the
  user waited for a voice that never came. The output side now gets the
  same shape and silence check the input side already applies to a
  recording (`stella/childproc.py::recording_finalized_ok`): the artifact
  is opened with `wave`, its peak sample scanned, and any of unreadable,
  too short, or silent raises `VoiceError`. `app.py`'s D2 degradation rule
  already handles that case — the text reply stands and no false
  "speaking" state is set — so the fix is honest failure, not new UI.
- **PCM s16 mono gets a clean onset.** For the one shape Kokoro actually
  produces, the artifact is additionally trimmed of leading and trailing
  silence (25 ms pad before the first loud sample, 15 ms after the last)
  and given linear edge fades (15 ms in, 10 ms out). Stereo, 24-bit,
  and any compressed variant is validated and passed through unchanged —
  this is a correctness fix, not a rewriting service — and the trim is
  skipped when it would leave less than 50 ms of anything. Measured cost
  is ~3 ms per synthesized chunk on the producer thread, well inside the
  existing synth-while-play overlap.
- **The trust model did not move.** Nothing about the ANSWER fast path,
  `sentence_chunks`, `VOICE_STYLE_NOTE`, or the resident worker's
  line-JSON protocol changed. `_speak_chunks` and its 3-deep queue are
  untouched. The one-at-a-time player rule and the barge-in arming
  discipline are the same as before this fix.
- `uv run pytest tests/test_voice.py` → 122 passed (nine new cases in a
  `resident speech artifact checks` section, and the resident-worker
  fakes updated to emit a real short PCM s16 mono WAV in place of the
  `b"RIFF"` bytes they wrote when nothing was checking); `uv run pytest
  tests/test_app.py tests/test_audio_output.py` → 95 passed; `uv run
  ruff check .` clean; `git diff --check` clean. `docs/VOICE.md` records
  the new shape rule and the trim/fade window.

### Stella answers to her name, and says when the microphone is open

- **A wake word whose entire authority is one button press.** `stella/wake.py`
  runs a local openWakeWord classifier over raw frames and its only output is
  the callback the **Listen** button already uses: one confirmed phrase is
  exactly one press, and nothing else may follow from it. It holds the device
  but records nothing until the phrase is confirmed, it never answers an
  on-screen approval, and a wake that hears only a transcriber's filler for an
  empty room is reported on screen and sent nowhere. There is deliberately no
  `auto` — the ear exists only while a box the owner ticked says it does. The
  `wake` extra and the ONNX files under `~/models/openwakeword` are the owner's
  to install; Stella does not download a model, and the ear involves no key and
  no network at all.
- **One microphone, not three of them.** `stella/mic_tap.py` is the single
  `pw-record`/`arecord` child that push-to-talk, the wake ear and the utterance
  watcher that endpoints a wake-initiated recording all subscribe to, so the
  device is never opened three times at once — the state that makes every
  "device busy" voice failure hard to explain. A subscriber that falls behind
  loses its oldest frame rather than stalling the reader; a dead capture tells
  each consumer once and Stella's text path carries on. Barge-in deliberately
  keeps its own process: it arms only while Stella speaks, when the wake ear is
  suspended, and it may read a different echo-cancelled source.
- **An open approval dialog takes every ear off the device.** Not merely
  ignores it: the wake ear is suspended, so an interrupt-by-voice cannot cancel
  from the voice the turn that is waiting on the owner's own click. Rule 10 is
  the reason this is a suspend rather than a filter.
- **Comprehension is found, named and bounded.** A local transcriber is
  detected before any recording leaves the laptop, and whichever engine is
  running is named on screen once per session (`Voice input uses voxtype
  (whisper).`) rather than on every turn. `STELLA_VOICE_TRANSCRIPTION=openai`
  is an instruction, not a fallback: naming the cloud skips the local branch
  instead of quietly being intercepted by it. Cloud requests now carry a
  timeout, because a stalled call holds the microphone's turn open in a way the
  user cannot cancel.
- **The voice already installed is used, and kept warm.** Synthesis prefers a
  resident worker the owner placed at `~/tools/stella-speak-server` — probed
  for whether it may run, never started at launch — over the robotic
  `espeak` fallback, and `STELLA_SPEECH_RESIDENT` keeps one process loaded
  across sentences so inter-sentence silence stops being model start-up.
  Detection is local-first in both directions and no key decides anything while
  a working local engine exists.
- **Settings grew the box, and Apply acts on it.** `wake_word_enabled` is the
  saved bool — the only new entry in `config.json`, and no secret in it — while
  every consumer still reads the mode, so the two spellings cannot drift.
  `STELLA_WAKE_WORD` overrides it for one launch in either direction and
  decides the bool with it. Rebinding stops a replaced spotter and ear before
  building the new session, so unticking really closes that capture instead of
  leaving two subscribers behind.
- **A dot, and a mute switch that is not decorative.** The dot is red while
  Stella really holds the device — an armed ear, a capture in flight, an
  interruption listener — read from the parties that can know, and dark the
  moment the microphone is released. *Mute mic* stops the wake ear, refuses a
  wake phrase that races the switch and a Listen press that arrives after it
  with one shared sentence, takes down a wake capture already under way, and
  leaves a hand-started recording and all output alone. The refusal lives in
  one resume path, so no route — a Settings rebind included — can arm the ear
  behind the switch by forgetting to ask.
- **The trust model did not move.** A transcript still has exactly the
  authority of typed user input and none of its own; `DANGEROUS` actions still
  raise the same approval; nothing a wake ear or VAD hears is written to disk or
  stored; and no voice path is ever stored in `api_keys.json`. What the scope
  list gave up is one line, not the boundary: *wake-word detection* became an
  opt-in capability and *always-listening audio* still means continuous
  **recording**, which stays out, along with speaker identification and
  streaming recognition.
- **One file runs the real engines, by request.** `tests/test_voice_roundtrip.py`
  is gated on `STELLA_VOICE_ROUNDTRIP=on` — skipped, never silently passed —
  and never opens the microphone: fixed sentences go out through the resident
  worker and back through the detected transcriber, using the same builders a
  launched Stella uses, and every word has to return in order. Measured
  2026-10-03, three sentences survived Kokoro → whisper whole, which is also
  the deferred engine question answered. The same run found a real defect no
  fake could see: the detected tool printed its own progress block on stdout,
  so every live transcript carried it as user words until the transcript was
  read out of that output instead of taken from it whole.
- `uv run pytest` → 1854 passed, 7 skipped; `uv run ruff check .` clean;
  `git diff --check` clean; the round trip run once with its switch on.
  `docs/VOICE.md` is the authority for how any of this behaves, and
  `docs/ROADMAP.md` records the scope rewrite as Stage D's D6.


### Stella's voice stops reading the markup aloud

- **Spoken replies are now the words, not the formatting.** Everything a
  speaker renders goes through one new boundary —
  `stella/spoken_form.speakable()`, applied when `SpeechOutput` is built — so
  no engine is ever asked to say "asterisk asterisk". Heading hashes,
  bold/italic/strikethrough markers, backticks, bullets and list numbers,
  table pipes, rules, blockquote chevrons, emoji and zero-width marks are
  removed; list items are joined with commas, which is also what gives the
  voice its pauses. Applies to every provider (local command, resident
  worker, OpenAI) and to the whole reply or each chunk of it, because the
  conversion is idempotent and belongs to the boundary, not to a provider.
- **Two honest substitutions, and no others.** A written link keeps its label
  and loses its address; a bare URL becomes "a link" — a listener cannot open
  either. Nothing is paraphrased, reordered, summarised, or expanded: times
  stay "18:00", numbers stay numbers, because a wrong expansion is worse than
  an awkward one and Stella owns no locale for hours. A reply made entirely
  of decoration would become silence, so `SpeechOutput` keeps the original
  text in that one case rather than looking broken.
- **A delivered Outline alert is also spoken.** When speech output is on, the
  amber alert line a claim produces is read aloud too, through its own
  single slot: never while another announcement owns the speaker, dropped
  unheard the moment a reply speaks or the user cancels or stops playback,
  silent on failure, and never spoken at all when speech is off. The text is
  the runtime's own line; the reminder title inside it stays untrusted
  information that is rendered and nothing more.
- **Shutdown now retires background speech.** `StellaBridge.stop()` performs
  the same flush a reply performs, so an unheard narration phrase or alert
  cannot start playing after the application has been closed.
- `uv run pytest` → 1750 passed, 5 skipped; `uv run ruff check .` clean.
  `docs/VOICE.md` carries the rules ("Spoken alerts", and the rewritten
  speech-output paragraph).

### Stella stops keeping its own reminders

- The reminder **store** is gone: `stella/reminders.py` (its SQLite table and
  the `pending → handled` transition), the
  `reminder_create` / `reminder_list` / `reminder_cancel` tools,
  `ReminderAction` / `ToolResult.reminder_action`, the window's Reminders
  panel and nav entry, `ReminderScheduler`, `StellaSettings.reminders_db` /
  `STELLA_REMINDERS_DB`, and the reminders database's place in
  `stella backup`. No reminder is user-approved any more, because no reminder
  is stored.
- **What survives is delivery, retargeted at Outline.**
  `Stella.check_due_reminders()`, `ReminderDelivery` and
  `ReminderLifecycleEvent` come back in a narrower form: they ask the Outline
  reminder pump for the alerts *this process just claimed* and surface each as
  one chat line, and the pump's `due → fire` claim is what makes an alert
  reach the user exactly once. The desktop interval returns as
  `ReminderTicker`, which exists only to post that read onto the bridge's
  single command queue, arms only when a real Outline server is reachable, and
  can never reach the Brain, the LLM or a tool. Without this, "remind me"
  would be a silent no-op whenever the browser is closed.
- **"Remind me" is now an Outline alert.** The same request creates or
  updates a task or event carrying a `remind` time, and the Outline app
  owns the notification. When no Outline capability is available Stella
  says plainly that it cannot schedule a notification instead of
  inventing a reminder or claiming one exists. `docs/REMINDERS.md`
  records where the behaviour went.
- **The rulings that were never about storage stayed.** A due alert can
  reach only this user, so "remind the team …" is still `kind=ask`
  rather than a note the user alone would receive; a vague "what's on
  today?" is still the user's own schedule rather than a document
  lookup; one request is never satisfied by both systems; and when no
  due time can be determined Stella asks instead of guessing one.
- **What this deliberately gives up:** nothing polls on Stella's own schedule.
  The ticker exists to ask a question, not to keep time — the pace of the
  HTTP cycle belongs to the pump and the exactly-once decision belongs to
  Outline. An alert therefore depends on Outline running with its own alerts
  enabled, which is the point of moving it. Stella still schedules no
  notification of its own and keeps no list of things that will fire later.
- **What this deliberately does not touch:** the proactivity layer
  (`DueTaskEvent`, `ProactivityDelegation`, the informed/asking/silent
  decision) survives intact, because its rules — an external event is
  untrusted information (rule 7) and proactivity may raise awareness but
  never authority (rule 8) — are not reminder-specific. Only the
  reminder→`DueTaskEvent` adapter was cut. Existing
  `stella_reminders.db` files are left exactly where they are: nothing
  is migrated, rewritten or deleted.
- **What this gives up on confirmation:** `reminder_create` was
  `DANGEROUS`, so every "remind me" asked before it wrote. `outline_create`
  is `SENSITIVE` — Stella's standing classification for Outline writes —
  so the same sentence is now an ordinary workspace write with no dialog.
  Accepted: the write is bounded to the connected workspace, it is
  reversible in Outline, and gating it would mean gating every Outline
  mutation. `docs/REMINDERS.md` records how to re-elevate just the
  alert-carrying call (`Tool.argument_risk()`) if that trade turns out to
  be wrong.
- **A claimed reminder is still untrusted information (rule 6/7).** The
  sweep never consults the Brain or the LLM, the trace records an id and a
  title length rather than the title, and a hostile reminder title is
  delivered as text only — a dangerous tool proposed afterwards still refuses.
  `tests/test_outline_reminder_delivery.py` pins all of that.
- Every invariant the reminder tests pinned was retargeted onto a
  surviving capability rather than dropped: panel-command authority onto
  the memory panel, worker-thread serialization onto a dedicated queue
  test, multi-word approval-mismatch verbs onto `key_send`, and audit
  classification and startup honesty onto the memory tools.
  `uv run pytest` → 1727 passed, 5 skipped; `uv run ruff check .` clean.

### Turns stop paying for words the model didn't need to write

- Report 35's first target — decode time is the whole turn: the two
  call kinds now carry separate output-token budgets end to end
  (decision 8192, answer 2048 by default; `STELLA_DECISION_MAX_TOKENS`
  / `STELLA_ANSWER_MAX_TOKENS`, 0 removes a cap). They are deliberately
  generous backstops against rambling completions, never truncators of
  legitimate work — a 20k-character outline body still fits the
  decision budget. Ollama's native endpoint also gained a
  `STELLA_OLLAMA_THINK` toggle (`0`/`1`) for hybrid-reasoning models
  like qwen3: measured on the live stack, thinking roughly doubles
  per-call time. The shipped default *sends nothing* — report 26's
  kill gate says the reasoning channel stays untouched until a corpus
  run on this provider line proves think-off costs nothing.

### The wait now says what it is doing

- Report 35's third target: between the first decision and the answer a
  turn went silent for 6-17 s. The activity observer gained a
  `calling:<capability>` event fired the instant a validated tool is
  about to run (app-known name, never model text), and every surface
  uses it: the CLI prints "(Stella is calling memory_list...)", the Tk
  status line upgrades "Stella is working · 7 s" to "Stella is calling
  memory list · 7 s", and voice keeps its existing filler untouched.

### The composer remembers what you sent

- Up/Down arrows walk the messages this window sent, newest first,
  like any terminal: Up recalls, Up again goes older, Down comes back,
  and Down past the newest entry restores the half-typed draft the
  recall started from. The arrows only fire when the cursor is on the
  very first (Up) or very last (Down) line, so editing a multi-line
  message keeps normal cursor movement; repeated sends are stored
  once.
- Every finished turn now says *when* it finished as well as how long
  it took: "(took 47 s · 14:32)". Like the duration, the clock time is
  display-only — the stored conversation never carries it.

### `stella verify-backup`

- Answers "will this restore work" without restoring: it checks a
  backup directory read-only — the manifest, SQLite's integrity check
  on every stored database exactly as archived, and the config the
  manifest promises — and touches no live state. A database too
  damaged to even open now reports broken instead of raising.

### Typed slash commands join the CLI and the window

- A line starting with `/` is now handled by the interface itself and
  never reaches the model: `/exit`, `/status` (provider, model, web
  backend and where your data lives), `/help`, `/version`, `/clear`
  (forget this session's conversation; memories and the action trail
  are untouched), `/history` (the newest action records, the same
  bounded trail `stella audit` prints), and
  `/trace on|off` / `/debug on|off` in the terminal — the startup
  flags, now session-mutable. Users can also drop a Markdown file into
  `~/.config/stella/commands/` and it becomes a command: `$ARGUMENTS`
  in the file is replaced by what you type after the name, and the
  expansion enters as ordinary input with no extra authority —
  approvals still gate every tool. Unknown names error locally with
  near-miss suggestions; template reads follow the persona discipline
  (no symlinks, size cap, containment); voice transcripts are never
  command-parsed, so a spoken "/exit" remains a thing you said.

### One LLM call for cheap read-only answers

- Report 35's second target: tools whose successful output is already
  user-facing text now carry a `terminal` flag in the dispatcher
  contract (`datetime`, `system_info`). When the
  brain marks such a call `tool_final`, the runtime renders the
  observation verbatim and the turn costs exactly one model call
  instead of two — halving the dominant latency on those turns.
  Failed observations and every non-terminal capability keep the
  honest synthesis path; approval gating is untouched.

### The quote band finally spans the whole window

- A short message like `> Hi` wore a band only as wide as its text.
  Tk stretches a tagged line's background to the full display width
  only when the line's newline character itself carries the tag, and
  the transcript deliberately left every newline untagged. Each
  quote/reply line's newline now carries its own line tag (verified
  with a pixel-measured probe), so the band runs edge to edge for
  messages of any length, and the widget test pins the newline tags
  instead of their absence.

### Provider presets, and your API key is now kept

- Setup and Settings replace the three provider choices with a real
  provider list: Ollama, local llama.cpp, OpenAI, Claude (Anthropic),
  Grok (xAI), Groq, OpenRouter, Google Gemini, or any other
  OpenAI-compatible endpoint. Picking one fills in the endpoint and
  suggests models, and each hosted provider's key is verified against
  that provider before it is ever stored.
- A pasted key that clearly belongs to a different provider (a Claude
  key under OpenAI, say) is refused with a hint to switch presets —
  offline, before any request is sent and without the key material
  appearing in the message.
- Verified keys are stored permanently in `api_keys.json` in Stella's
  private data directory, mode 0600, written atomically — never in
  `config.json`, never in an environment variable the UI writes, never
  shown again (only the last four characters). `OPENAI_API_KEY` still
  overrides the OpenAI slot for a launch; named presets resolve only
  against their own stored key. `stella backup` deliberately excludes
  the key file. See `docs/SECRETS.md`.
- **FreeLLMAPI (local router)** joins the picker: a self-hosted router
  that pools your own free-tier provider keys behind one local
  OpenAI-compatible endpoint (`localhost:3001`) with a `freellmapi-…`
  unified token — Stella's first preset where genuinely bill-free
  inference is the point. Groq's suggested models now name its current
  free lineup; the previous ones were retired upstream.
- Mid-session switching is a first-class path: Settings rebuilds the
  model live on Apply (conversation and memory carry over, no restart),
  and switching to a preset with no stored key is refused in the panel
  instead of failing as a mysterious background error.
- Under the hood the one OpenAI client learned a second tool dialect:
  hosted presets that only speak Chat Completions get function tools
  over that wire, proven on the full conformance matrix; OpenAI itself
  keeps the Responses API.

### Malformed tool calls can no longer interrupt you for approval

- Report 33's W5: an empty-argument or `{"arguments":…,"function":…}`
  envelope from the model used to render a real "approve?" prompt that
  then executed nothing. The dispatcher now validates before the
  approval prompt is ever built — uncallable calls get a parse failure
  as the model's feedback (still audited), and only calls that could
  actually run are shown to you.

### The transcript database now bounds itself completely

- Chat rows were already pruned to the newest 2,000 on every append,
  but the persona-reflection proposal queue had no bound: resolved
  rows were never read again yet accumulated forever, and pending
  proposals grew without end if a user never opened a session to
  review them. Enqueueing now prunes to the newest 100 pending and 50
  resolved proposals, so the file stays ~45 KB even after a 400-entry
  flood.

### Success paths now carry action receipts

- Report 33's W4: failures always logged rich receipts, but a
  *successful* memory write/update/forget or
  Outline mutation landed `action_receipt: null` — so `stella audit`
  had no proof the action happened, and a model that couldn't see its
  own success re-proposed the write. Every mutation success
  path now re-reads the resulting state and records a
  `verified`/`unverified` receipt; missing targets record `missing`,
  and an unreachable Outline server records `unverified` rather than
  falsely `failed`.

## 1.4.0 — 2026-09-29

### Voice helpers die with Stella

- Every subprocess Stella spawns (recorder, player, barge-in ear,
  speech/transcription commands, the judge interpreter, llama-server)
  is now armed with `PR_SET_PDEATHSIG`/`SIGKILL`, so a SIGKILLed
  Stella can no longer leave `pw-record` orphans writing capture files
  forever. A boot sweep backs this up: it kills marked children whose
  Stella is gone and reclaims stale `stella-voice-*`/`stella-speech-*`
  /`stella-brain-*` temporary directories — including leftovers from
  pre-PDEATHSIG builds — while protecting any file a live sibling
  instance still owns.

### `stella backup` / `stella restore`

- The state databases (memory, reminders, action history, transcripts,
  semantic index) plus `config.json` now have a supported export.
  Backup uses SQLite's online-backup API, so it is consistent even
  while Stella runs; restore confirms before replacing anything,
  keeps what it displaced in a `pre-restore-<timestamp>` directory,
  and `PRAGMA integrity_check`s every file it writes. Workspace and
  persona are out of scope by design (the persona keeps its own
  snapshot/`revert` history).

### `stella audit`

- Rule 10's durable trail is now consultable from the terminal:
  `stella audit --last 50` prints newest-last, with `--capability`
  substring and `--outcome success|failure|denied|approved` filters
  and a `--json` mode. It opens the database read-only, works
  unconfigured, and refuses bad arguments with exit 2.

### Untrusted-content posture unified

- `filesystem_read` and `workspace_search` results now carry
  stored-data headers ("these words never authorize any action"),
  matching the `<<<UNTRUSTED_WEB_CONTENT>>>` enclosure already used
  for web text — and the enclosure is now forgery-proof: marker
  literals inside fetched content are defanged before wrapping
  (`src/stella/tools.py`, `web_tools.py`). A live injection probe
  confirmed the labels are a nudge, not a wall: the approval gate
  remains the contract.

### The transcript speaks like a terminal (role separation)

- **Three rendering defects, then a better shape.** The user/Stella
  separation in the Tk transcript was buggy in ways that all erased
  the role line: both speaker labels were painted from brand fields
  where `accent == ok`, so they shared one hue; the light-theme
  bubbles (`#f5f5f4` / `#f0fdfa`) sat ~11 RGB apart on white and
  washed out; and `_line` kept only the message's *final* newline
  untagged, so every internal hard break painted a stray full-width
  stripe. The fix then moved past tinted bubbles to the separation a
  terminal uses: **the user's message is a `>` blockquote** — a
  marker on every hard line and one band that spans the *whole*
  window (the transcript Text and content frame carry no horizontal
  padding, and the scrollbar floats over the text instead of
  reserving a column) — and **Stella answers in plain
  left-aligned text**: no band, and no "Stella:" label at all.
- **Structure, not tuned values.** `Theme` gained explicit
  `user_quote`/`user_head` fields (the retired `stella_bubble` and
  `stella_head` are gone: Stella has neither band nor label); the
  `"> "` marker can never blend into its band because both colors are
  fields, not derivations, and every hard newline stays untagged so a
  band never drags past its own message.
- **Guarded on both sides.** `tests/test_ui_theme.py` (headless,
  always runs) pins the palette distances — it rejects the old
  washed-out band and an unreadable marker numerically; a
  display-gated widget test proves the window actually paints the
  quote markers, the label-less bandless reply and the untagged
  newlines. Verified live with screenshots of a multi-line exchange
  in both themes.

### The reminders boundary: "remind me" is Stella's, always

- **One ruling, stated everywhere (report 30).** A plain "remind me"
  request routes to `reminder_create` and never to Outline; Outline's
  `remind` field sets an alert *inside the Outline app only*, correct
  solely when the user is creating/updating an item there and names
  Outline. One request is never satisfied with both — ambiguity asks.
- **Words, not new plumbing.** The rule lives in the Brain prompt and
  in the `outline_create`/`outline_update` descriptions and schema
  field; no contract, validator or tool shape changed. Four pinned
  tests (`test_brain.py`, `test_outline_tools.py`) guard the wording
  so a future description edit cannot quietly erase the boundary.
- **Two refinements from the 21-tool re-measure (report 32).** The
  selection bench, run on the shipped 18-tool registry plus the Outline
  trio, found qwen3:4b confidently mis-routing two shapes the report-30
  ruling didn't reach. A reminder can now be stated to notify **only
  this user**: "remind the team …" is *not* a `reminder_create` (the
  model asks instead of storing a reminder the user alone would get).
  And a vague **"what's on today?"** is the user's own schedule —
  `reminder_list`, with Outline reached for only when named (`outline_search`
  had become an attractor for it). Prompt words plus one pinned test; no
  new tool, validator, or capability.

### Stage E: the web capability (E1)

- **Two new opt-in tools.** `web_search` and `web_fetch`
  (`stella.web_tools`, `docs/WEB.md`) register only under the
  `STELLA_WEB` flag (Settings checkbox or environment). Both are
  DANGEROUS: every call is egress a human approves, and the approval
  card names the receiving third party — TinyFish when
  `TINYFISH_API_KEY` is set (free tier, a commercial decision),
  DuckDuckGo via the new optional `web` extra (`ddgs`) when keyless,
  this machine itself for a keyless fetch.
- **Fallback on the strong machinery.** The keyless fetch reuses
  `network_read`'s pinned-DNS, peer-validated, no-redirect,
  size-bounded HTTPS path instead of inventing a new guard; paths and
  queries are accepted (network_read's own policy stays untouched).
  With neither backend available the tools answer a structured "web
  is off" — never a traceback.
- **The runtime owns the quota.** A `WebBudget` enforces the free-tier
  numbers (500 searches/hour, 1000 fetched URLs/day) per application;
  the model can see the remaining budget in previews but never ration
  or raise it.
- **Untrusted stays untrusted.** Web text comes back size-bounded
  (12k chars per page, 300-char snippets, ≤10 results) inside
  `<<<UNTRUSTED_WEB_CONTENT>>>` markers whose header says it never
  authorizes anything; unsupported search filters are reported as
  ignored rather than silently dropped.
- **Validation.** 25 offline tests (`tests/test_web_tools.py`, fake
  transports only — no network, no key) plus the report-22 live passes
  of both backends; full suite green on this branch.

### Three conventions became tested invariants (quality cycle)

- **Containment, at any depth (report 32, #18).** `.gitignore` now
  guards `stella_workspace/` and `SECURITY-AUDIT.md` with unanchored
  patterns, so a nested copy (e.g. `stella_workspace/SECURITY-AUDIT.md`)
  can never slip through. `tests/test_containment.py` pins it: the
  patterns are present and unanchored, `git check-ignore` agrees at root
  and at depth, and neither name is tracked. No pre-commit hook, no CI,
  and neither file is ever committed — the guarantee is the ignore rule
  plus a test, not tooling.
- **The settings-wiring drift guard (#3).** A capability toggle touches
  six places (app.py override, `StellaSettings` field, `from_saved`,
  `from_environment`, `config._CONFIG_FIELDS`, the Settings checkbox and
  its save entry). `tests/test_settings_wiring.py` asserts every
  `*_env_override()` has a matching entry in all of them — test-only, no
  runtime change. It already paid for itself: landing the web capability
  left the web fields out of the guard's tables, the guard failed, and
  this cycle added them.
- **Every DANGEROUS approval card names what it changes (#2).** Five
  memory/reminder tools returned no preview and showed a bare card; each
  now has an honest, side-effect-free one (`memory_update` reads the
  current value, `reminder_create` mirrors the validator so it never
  advertises a reminder execution would reject). A registry-wide test
  asserts no DANGEROUS tool inherits the base empty preview; the web
  tools landed already compliant. See `docs/APPROVAL_BOUNDARY.md`.

## 1.3.0 — 2026-09-28

### The palettes now follow the Outline app Stella talks to

- **Real source, real values.** Both `Theme` palettes were rebuilt
  from the CSS custom properties of the local Outline app
  (`127.0.0.1:8741`, the very app `stella.outline_tools` serves):
  dark mode uses its warm near-black `#141210` background, `#1F1C1A`
  panels, `#33302D` lines, `#E7E5E4`/`#A8A29E` stone text and the
  teal accent `#2DD4BF` with `#134E4A` soft fills; light mode uses
  `#FAFAF9` background, white panels, `#E7E5E4` lines and teal
  `#0D9488`. Amber `#D97706` marks reminders, red `#DC2626`/`#EF4444`
  errors.
- **Rail, panels, buttons.** The nav rail shares the background tone
  like Outline's sidebar, the active item lifts onto the panel
  color, and buttons/fields keep a line-colored border so they read
  on both surfaces. Light mode was rebuilt around white content
  panels on the `#FAFAF9` page (the transcript itself is white,
  fields are stone-100), and message bands are calm tints — Tk
  paints a tagged line's background across the whole display line,
  so bubbles read as full-width rows, not floating pills.
- **Still presentation only.** No behavior, contract or wording
  changed; the full suite (1230 tests) passes with the new palettes.

### The window was redesigned around a navigation rail

- **Notebook gone.** The cramped side tabs became a full-height nav
  rail (Chat, Memories, Reminders, History, Settings); every section
  now owns the whole content area as a header + card layout, and the
  active item is lifted onto the content surface in the accent color.
- **Chat got a real stage.** The transcript sits in a framed card with
  roomier bubbles, the composer is a card with stacked Send/Cancel,
  and a quiet braille spinner marks the working status between the
  elapsed-second updates.
- **Flat, modern styling.** All buttons are bevel-less with an accent
  hover; new palettes (v2) give both dark and light modes deeper
  neutrals, one accent, and a subtle border token for cards and
  fields. The theme toggle moved to the bottom of the rail.
- **Same contract, new chrome.** Every bridge command, approval
  exactness, transcript wording and widget name the tests drive
  (including the "Stella is working · " prefix) is preserved; one new
  test covers rail section switching.

### The window learned to change its clothes (dark and light themes)

- **One palette became two.** `src/stella/ui.py` replaced its
  hard-coded dark constants with `Theme` dataclasses carrying a full
  dark and a full light palette (surfaces, fields, text, accent,
  status colors, bubbles), tuned for a minimal, quiet look in both
  modes. Every widget builder reads the active palette; nothing else
  about the window's behavior moved.
- **Live toggle, remembered choice.** A Light/Dark button in the
  header switches without restarting or interrupting anything: ttk
  styles are re-applied, the plain Tk widgets (transcript, composer,
  lists, open approval dialogs with their previews) are recolored in
  place. The choice persists in a `ui-theme` file beside
  `config.json` and is loaded at launch; unreadable state falls back
  to dark.
- **Presentation only, by construction.** The bridge contract,
  transcript wording, approval exactness and the setup wizard are
  untouched; the theme file stores one word. `docs/UI.md` documents
  the design; two focused window tests cover the recolor, the
  persisted choice and the fallback.

### Stage D: voice latency, measured before anything was built

- **Resident synthesis worker (D2).** Measuring Kokoro sentence by
  sentence (research report 24) showed the per-sentence wait is almost
  entirely fixed start-up — a >3 s floor of interpreter launch plus
  model load on *every* `STELLA_SPEECH_COMMAND` call, median 3.65 s
  per short sentence on an idle machine — while synthesis itself runs
  at roughly 0.4× real time. `ResidentSpeechProvider` in
  `src/stella/voice.py` keeps one long-lived worker alive across
  sentences (line-JSON protocol, same shape as the laya judge), so the
  idle A/B median drops to 1.80 s per sentence and the start-up is
  paid once per session (cold worker ≈2.6 s). Opt-in via
  `STELLA_SPEECH_RESIDENT=on`, meaningful only alongside
  `STELLA_SPEECH_COMMAND`, environment-only like all voice settings.
  A dead, timed-out or badly-answering worker is retired on the spot
  and restarted on the next sentence; a broken worker degrades to no
  speech with the text reply untouched, and artifact paths are always
  Stella's own — a path from a worker reply is never trusted.
- **Spoken turns narrate their own work (D3).** A listening user could
  not tell Stella working from Stella stuck, so spoken conversation
  turns (voice input *and* "Speak replies" on) now say so out loud:
  `Stella.process` reports the phase it is entering — `"thinking"`,
  `"working"`, `"answering"` — through a presentation-only observer
  that decides nothing, and the application speaks one short phrase it
  wrote itself from the fixed `NARRATION_PHRASES` set. The model never
  authors narration; approvals, risk and the audit record are
  untouched. Narration can never slow a turn (a busy single slot
  drops phrases, synthesis runs off the worker thread, failures stay
  silent) and is retired unheard the moment reply speech, **Cancel**
  or **Stop speaking** arrives. The same spoken turns also earn a
  fixed application-authored style note — the audio-modality envelope
  asks the answer to be one or two short speakable sentences.
- **Barge-in accepted for live use; the default becomes `auto` (D4).**
  The playback-stretch blocker that parked the ear (report 19) did not
  reproduce under instrumentation: full plays up to 7 s measured
  1.01–1.02× real time with the ear armed, no xruns, no `pw-play`
  errors (reports 28–29). The human double-talk acceptance passed 3/3
  (383 / 128 / 127 ms voice-onset-to-cancel, bar ≤500 ms) with zero
  false fires on silent controls, and hesitant-speech analysis ("um"
  sample through the shipped judge) showed the 5-frame streak is not
  fragile — no endpointing change was made. `STELLA_VOICE_BARGE_IN`
  now defaults to `auto`: the ear arms only when `STELLA_BARGE_SOURCE`
  names the capture — declaring an echo-cancelled mic *is* the enable,
  because the live data also showed what a raw-mic ear does (uncancelled
  playback registers as speech, and the ear cancels Stella herself).
  `on` still forces the old always-arm behaviour, `off` still vetoes.

### Stella can work with the Outline app (opt-in tools)

- **Three tools, doubly gated.** `outline_search`, `outline_create`
  and `outline_update` drive the local Outline app over its HTTP
  API — tasks, events, notes, projects, people and the connections
  graph. Registration requires BOTH the opt-in (Settings checkbox or
  `STELLA_OUTLINE=on`) AND a reachable Outline server with a
  readable token (`~/.local/share/outline/outline.token`, never
  model-supplied): a server that is not running means the model
  simply never sees these tools.
- **Reads are silent, writes need approval.** Consistent with every
  other capability: search never asks, create/update always show the
  exact change first. Tool output is framed as stored data, never
  instructions, and bounded.
- **API-first parity** (AGENTS.md rule 16): capabilities grow inside
  the three verbs as `kind`/`action` values — task/event
  `reschedule`, `edit`, an ISO-8601 `remind` field, person
  attach/detach, `kind=graph` neighborhood rendering — with
  Stella mirroring the Outline server's recurrence and tag-token
  validators, so malformed values are rejected before any request
  leaves. Documented non-parities: bulk export (UI-only) and the
  force-graph layout (rendering-only).

## 1.2.0 — 2026-09-27

### Stella can be interrupted by voice (Stage B7, opt-in barge-in)

- **The ear shipped, still just a button.** `src/stella/barge_in.py`
  runs a raw 16 kHz mono capture (`pw-record -a`, `arecord` fallback)
  through the Silero VAD v6 ONNX model on one daemon thread; five
  consecutive frames above both a probability and an energy floor
  (~160 ms of speech) fire exactly one callback, and that callback is
  `StellaBridge.cancel_current_turn()` — the documented any-thread
  Cancel press from A8. The detector can approve nothing, inject
  nothing, and never records: its only output is the interrupt.
- **Opt-in and environment-only, like all voice settings.**
  `STELLA_VOICE_BARGE_IN` defaults to `off`; `STELLA_VAD_MODEL`,
  `STELLA_BARGE_SOURCE` (e.g. `ec_mic`) and `STELLA_BARGE_THRESHOLD`
  cover the rest, and nothing is added to `config.json`. The one new
  dependency is `onnxruntime`, isolated in a `barge-in` extra
  (`uv sync --extra barge-in`) because the pip `silero-vad` package
  would have dragged CUDA torch in (report 08).
- **Echo cancellation is a prerequisite, documented as a recipe.**
  Measured: uncancelled playback registers as speech on 23% of
  playback frames; through PipeWire's WebRTC echo-cancel it is 0% at
  −29.6 dB. `docs/VOICE.md` now carries the `pactl` setup (load the
  module while the built-ins are default, then move the defaults to
  `ec_out`/`ec_mic`) and the Bluetooth caveat (keep SCO out of the
  ec chain).
- **Degrades exactly like every other voice part.** A missing extra
  or model says so once at startup and leaves everything else
  working; an internal fault retires the ear for the session; a
  broken ear never touches text, Listen, or speech. 20 offline tests
  (judge, subprocess listener via a synthetic byte writer,
  settings, bridge arm/disarm lifecycle) — no microphone needed.
- **Live testing then found a blocker, and barge-in is parked with it
  documented.** With the ear on, the first sentences of each spoken
  reply played stretched and glitching (a 7.5 s file took 45.8 s,
  another 76.6 s in the UI trace) while the detector logged *zero*
  voiced frames — and the byte-identical UI with the ear off plays
  perfectly. Five controlled reproductions, up to and including the
  full UI shape (per-episode ear churn, real producer/consumer
  threads, real Kokoro synthesis during playback, zero-delay arming),
  all stayed clean: the interference needs the fully loaded UI and is
  still unexplained (leading hypothesis: PipeWire real-time
  starvation with brain + TTS + capture live at once). The human
  double-talk acceptance is therefore not taken; `off` remains the
  default (research report 19 records every measurement).

### The System-1 router gate: measured, and it says no (Stage B0)

- **All three B0 prerequisites are now facts, not hopes** (research
  report 18). Coexistence passed: the shipping brain plus a resident
  Laya sidecar fit in 5745/6141 MiB with no mutual eviction. But the
  router's whole premise — that a cheap classifier can decide "does
  this turn need a tool?" — failed on Stella's own decision corpus:
  authored routing questions scored AUROC 0.550 and the verbatim
  shipped `router_questions` preset 0.567, a coin flip biased to
  silence 16 of the 18 tool-requiring turns at any sane threshold.
- **`SystemOneRouter` is a recorded non-build decision.** No code was
  written for it, which is the point of measuring before building;
  Laya keeps its already-validated B5 event-bus role, and ~1.4 GiB of
  VRAM stays headroom. The decision reopens only at AUROC ≳ 0.75 on
  the re-runnable calibration bench.

### A provider swap is now a measured claim, not a hope (Stage B4)

- **One written contract.** `src/stella/conformance.py` states the
  obligations Stella's decision path depends on from any provider as
  data: 16 named cases (clean, code-fenced and prose-embedded decision
  JSON; replies with no JSON failing closed to silence; native tool
  calls with dict, JSON-string and malformed arguments; the reserved
  `tool_final` marker stripped before the dispatcher; invented
  capabilities refused by the trusted dispatcher; the REQUIRED answer
  guard; an HTTP 500 surfacing as an error, never as a fake reply).
- **Proven offline on the real clients.** A loopback simulated provider
  replays each case's wire shape at all four shipped clients
  (OpenAI-compat via the Responses API, Ollama compat, Ollama native
  `/api/chat`, llama-server compat) through the actual `LLMBrain`
  decision path — 61 case×dialect checks, deterministic, no GPU, no
  network beyond 127.0.0.1 (`tests/test_conformance.py`).
- **Proven live on the shipping endpoint.** The same script re-asks the
  endpoint-only obligations
  (`~/tools/bench_provider_conformance.py`, measured today on
  `qwen3:4b` at `num_ctx=8192`): a nonexistent model returns an honest
  HTTP 404; the whole real decision prompt was evaluated
  (`prompt_eval_count=3442` vs a 4,149-token size estimate — the
  written, checkable form of the report-15 truncation question); the
  compat tool channel delivered a clean native `datetime` call.
- **A prompt-bloat tripwire ships as a test:** the decision prompt must
  keep fitting the wired context budget, so a future prompt grows
  caught by the suite, not by a silently truncated answer.

### Some ordinary requests now cost an approval (and one fewer floods recall)

- **Argument-aware risk (Stage B1).** Risk is no longer purely
  per-capability: two trusted, additive rules raise scrutiny for
  specifically dangerous shapes. Reading a credential-bearing file
  (`.env`, `secrets/…`, `*.pem`…) through `filesystem_read` now requires
  an approval like a write does, and an explicit full-screen
  `screen_read` requires one because it captures every window, not just
  the focused one. The mechanism can only ever **add** approvals: the
  effective risk of a call is the maximum of the capability's floor and
  the argument elevation, so no argument phrasing can demote a dangerous
  capability, the exact-`ApprovalRequest` match is untouched, and an
  audit line honestly records the risk that was actually applied
  (`tests/test_argument_risk.py` proves all of this).
- **Memory scale policies (Stage B3).** Per-turn recall is bounded to the
  best-scoring 16 candidates before fusion (a large store can no longer
  make every turn scan everything), a repeated memory write still stores
  but now honestly notes the existing copy and suggests `memory_update`
  instead of growing duplicates, and the approval gate on `memory_forget`
  is re-proved by tests rather than assumed (`tests/test_memory_scale.py`).

### The Ollama brain was losing most of its own instructions

- **Silent truncation found by measurement, not mood.** Every decision
  turn sends a ~5,300-token prompt (system contract plus context), but
  Ollama's own `prompt_eval_count` reported exactly 2050 evaluated
  tokens on every turn at `num_ctx=4096` — and still finished with
  `stop`, never a length error. The head of the system prompt, where
  the strict-JSON and routing rules live, simply never reached the
  model.
- **Fixed to `num_ctx=8192`, with a test pinning the wiring**
  (`build_application` now asks for a context that fits the whole
  decision prompt). The effect is measured, not assumed: on the decision
  bench the incumbent goes from 21/30 to 29/30 strict cases, and on the
  injection bench it obeyed instructions planted inside a tool output on
  4 of 8 identical decisions before the fix and 0 of 8 after. (research
  report 15; before/after via `~/tools/measure_prompt_ctx.py` and
  `~/tools/bench_injection_repeat.py`)

### Stella can read and act on the Hyprland desktop, only when proven

- **Three opt-in desktop capabilities** — `screen_read` (a local
  screenshot fed to local OCR — no cloud, no vision model), `window_focus`
  and `key_send`. They are registered only when the settings flag is on
  *and* this really is a Hyprland session with the tools installed, so
  a model never sees a capability that could only fail.
- **Nothing is trusted, everything is re-queried.** Reads accept only
  parseable JSON of the expected shape (the compositor answers garbage
  with rc 0), and every act is followed by a fresh compositor query, so
  a receipt says *verified* only when the session proves it. A keystroke
  send is honestly *inconclusive*: the keys reach a focus-confirmed
  window, but what the application did with them is unknowable from
  here.
- **Privacy bound on what the screen says** — OCR text is capped and
  obvious credentials are masked before the model sees them, and pixels
  are never written to disk.
- **Corrected against the live machine, not the notes** — the plan's
  measured syntax was wrong in four places (the instance flag, the focus
  dispatch route, grim's geometry format and its stdout argument); each
  is fixed to what this Hyprland 0.56.2 build actually accepts and was
  verified end-to-end with a real window. (live: reads ~8 ms, focus ~26 ms,
  key send ~100 ms, window OCR ~0.4 s, full-screen OCR 6.9 s)

### Events now route through two cheap tiers before anything acts

- **The event bus** (`stella.event_bus`) registers up to ten standing
  intentions and dispatches events (a source, text and structured
  fields) through Tier 0 — deterministic regex/field rules a human can
  read and audit — and only the events a rule did not already explain
  are asked of Tier 1, one batched laya question set per dispatch in
  the shipped preset shape. A match is a *record with a reason*, never
  an action: consuming a match stays each feature's own approved
  decision.
- **Uncalibrated confidence never acts.** The laya checkpoint's
  probabilities are advisory data unless an intention explicitly
  registered a threshold; choice-type questions fire on the label,
  which is a deterministic reading of the answer.
- **laya stays outside Stella.** A separate interpreter venv runs a
  small line-JSON runner (`stella.laya_judge` + `stella.laya_runner`);
  a hung, crashed or refusing judge is dropped with an honest
  "unavailable" status — "could not ask" never reads as "no match" —
  and is restarted on the next question. (live: 12.6 s to load,
  ~2.6 GB VRAM beside the brain, 23–39 ms per event, clean teardown)

### Stella now runs her own llama.cpp brain

- **Provider `llama`** — point Stella at a downloaded `.gguf` file
  (Settings tab, or `STELLA_LLM_PROVIDER=llama` with the path in
  `STELLA_MODEL`) and she launches `llama-server` as her own child
  process with the measured high-quality launch line — no systemd
  unit, no manual server, nothing left running behind her.
- **Honest lifecycle** — startup waits for the server's own health
  endpoint and reports the server's last output when it refuses to
  come up; a port someone else already answers on is refused rather
  than quietly shared; shutdown sends the server its own clean
  SIGINT path first and escalates only if ignored (live: 4–19 s to
  ready, ~2.9 GB VRAM, gone on close).
- **Test connection** for the llama provider truthfully checks what a
  launch needs — the model file and the server command — since no
  server exists until Stella starts one.

### The window got a real look

- **Dark, calm theme** — one palette across the chat window, the
  approval dialogs and the first-run setup wizard: deep slate surfaces,
  a themed font picked from what the system has, and consistent
  buttons, tabs, lists and fields.
- **Speech bubbles** — your messages render as right-aligned bubbles,
  Stella's replies as left ones with a highlighted name; reminders,
  errors and quiet system notes each have their own readable style
  instead of one undifferentiated block of text.
- **Clearer chrome** — a window header, an accent-colored Send button,
  an italic status line, and approval dialogs that read as a heading,
  the exact action, the diff preview, and two obvious answers.
- **Nothing semantic moved** — every word the transcript, status line
  and dialogs assert is the same text through the same bridge
  contracts; this is presentation only.

### Speech starts speaking while the reply is still being written out

- **Sentence by sentence** — a multi-sentence spoken reply is now
  synthesized one sentence at a time and played from a queue: the
  first sentence reaches the speakers after roughly one sentence of
  rendering instead of after the whole reply (measured ~10.7 s →
  ~1–2 s first-audio on local synthesis, which renders faster than
  real time). Single-sentence replies behave exactly as before.
- **Stop speaking means stop** — the button (and **Cancel**) now
  silences the entire spoken reply: the sentence playing and the
  sentences only queued, which are discarded unplayed. The underlying
  decision is still never touched.
- **Honest partial speech** — if synthesis fails mid-reply, the
  sentences already rendered are still spoken, one error line says
  the rest could not be prepared, and the text response remains fully
  available. Every played or drained artifact is deleted; command
  providers now name each synthesis uniquely so queued audio can never
  overwrite a sentence still playing.

### Persona writes became reversible

- **Every replacement keeps a snapshot** — before an approved
  `persona_edit`, an editor session, a preset, an onboarding draft or
  a revert touches `persona.md` / `persona.addons.md`, the previous
  bytes are copied into a `history/` directory you own (newest 10 per
  file, labeled with what replaced them; identical content is
  deduped).
- **One command away from undone** — `stella persona revert` lists the
  undoable versions; `stella persona revert <number>` shows a unified
  diff, asks you to confirm, restores those exact bytes and verifies
  the read-back. The restore snapshots what it displaces, so reverting
  a revert is just one more revert.
- **Honest limits** — a failed snapshot never blocks a write you
  already approved (the result then says the change cannot be
  reverted), the editor copy predates the `$EDITOR` session, and
  reflection still writes nothing: approved proposals inherit history
  through the same `persona_edit` path as always.

### Cancel reaches the whole voice periphery

- **The transcribing window is cancellable** — pressing **Cancel**
  while "Transcribing..." is shown abandons the capture and a running
  local transcription command within about a second (the command
  providers grew a `cancel()` handle with a SIGINT → terminate → kill
  ladder, mirroring the player), and honestly reports that nothing was
  sent: a cancelled transcript is never replaced with invented text
  and no turn starts.
- **Cancel means silence** — `cancel_current_turn` now also stops
  playback, and speech synthesized around a cancel is discarded
  (its file deleted) rather than played. A cancel raised during
  transcription survives into the turn instead of being erased: the
  turn ends cancelled at its next safe point.
- **Unchanged by design** — tools and approvals stay atomic
  (cancel never interrupts an executing action), the CLI path is
  untouched, and an abandoned cloud transcription/speech request is
  given up on without a socket close — the honest limit of an SDK
  call that exposes no per-request cancel.

### Real embedding recall (opt-in)

- **Three embedding providers, one honest boundary** — semantic recall
  keeps `local-hash` (the zero-dependency word-shape index) as its
  default, and can now be switched (Settings choice or
  `STELLA_SEMANTIC_PROVIDER`) to a real embedding model: `ollama`
  embeds through a local Ollama server (`STELLA_EMBED_MODEL`, default
  `nomic-embed-text`, stdlib HTTP — no new dependency) or `minilm` runs
  `all-MiniLM-L6-v2` on CPU via the optional `stella[embed]` extra.
  A missing extra or backend fails its own use honestly and never
  auto-switches the choice. Ollama's nomic prefixes
  (`search_document:`/`search_query:`) are applied client-side, because
  measurements confirmed Ollama passes embedding input verbatim.
- **No cross-model vector mixing** — every index row now records its
  provider label and vector dimension, and search computes cosine only
  against rows from the same model; legacy and foreign rows are skipped
  until the next reconcile rewrites them (reconciliation is also the
  provider-switch migration).
- **Unavailable is not irrelevant** — when the embedding backend cannot
  answer a query, the turn proceeds on keyword recall and the trace
  records a `SemanticSearchUnavailableEvent` (visible with
  `STELLA_TRACE=1`); fusion supplements carry the active provider's
  label in their provenance, so `"ollama-embedding"` and
  `"local-hash-embedding"` hits are always told apart.

### Semantic memory recall (opt-in)

- **Keyword-dominant fused recall** — a new Settings checkbox (or
  `STELLA_SEMANTIC_MEMORY=1`) wires the local word-shape embedding index
  (`LocalHashEmbeddingProvider` + `SQLiteSemanticIndex`) into memory
  retrieval. Keyword matches keep their exact place; at most two
  semantic supplements may fill the remaining space, so nothing about a
  lexical hit's ranking silently changes. Disabled installs never create
  the index file at all (`STELLA_SEMANTIC_DB` moves it if enabled).
- **Honest provenance, no ranking magic** — every retrieved memory
  reports how it was found (`keyword` with the lexical score, or
  `local-hash-embedding` with the cosine similarity) in the Brain
  payload; the two scales are never compared with each other, and the
  prompt states that a word-shape match means the words look alike —
  never that anything understood the meaning.
- **Self-healing index with honest failures** — the index is rebuilt
  from the authoritative memory store at startup and after every
  in-turn memory mutation, so edits made while Stella was down are
  caught up too. A failed refresh is reported (a trace event plus a
  note on the answer) and never changes the memory write's own verified
  outcome.

### Personality (data, never code)

- **A written persona** — `~/.config/stella/persona.md` (and
  `persona.addons.md` for learned style notes) is composed into the
  system prompt *below* a fixed app-owned invariant: Stella is an AI
  assistant and no persona text — hers or yours — can override that or
  the rules and approvals beneath it. With no persona file, the prompt is
  byte-for-byte what it always was. `stella persona` opens the file in
  your `$EDITOR`; `stella persona preset snark|warm|terse` writes a
  starter instead of a blank page (refuses to clobber without
  `--force`); first CLI launch after setup asks three questions, drafts a
  persona with your model, and writes it only if you say yes.
- **`persona_edit` — style changes go through approval** — asking Stella
  in chat to "be drier" can only take effect via a `DANGEROUS` tool call:
  path locked to exactly one of the two persona files (realpath-checked),
  a unified diff in the approval dialog, exact-match approval, and a
  verified-byte receipt afterwards. Learned style notes pass a
  forbidden-content filter (authority words are discarded, with the count
  reported, not hidden) and hard caps of 20 bullets / ~1 KB.
- **Opt-in transcripts and `stella reflect`** — a new Settings checkbox
  (or `STELLA_TRANSCRIPTS=1`) records a bounded local transcript (newest
  2 000 rows, turn text and cancellations, never read back into chat;
  off by default). `stella reflect` derives only observable signals
  (cancels during long replies, your own style pushback — praise is
  deliberately not a signal), asks for at most two style edits, and
  **writes nothing**: candidates are re-checked app-side (authority
  lines, agreement-only drift, evidence, consolidation at cap), queued
  in the same database, and surfaced as real `persona_edit` approval
  prompts at the next CLI or window session. With recording off or no
  signals it says so and exits. `docs/PERSONA.md` has the full trust
  model.

### Responsive cancellation

- **Cancel interrupts a provider request in flight** — pressing Cancel
  in the desktop window no longer waits out a slow model reply. On
  the local Ollama path the pending HTTP read is abandoned within
  about a second and the connection closed; an OpenAI-compatible
  request is likewise given up on and its late reply discarded. The
  cancelled turn is still discarded whole, approvals and executing
  actions remain atomic (a running step lands first), and an
  uninterrupted turn behaves exactly as before. Honest caveats:
  server-side generation for an abandoned request stops best-effort,
  and the voice transcription segment remains non-cancellable.

## 1.1.0 — 2026-09-24

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
