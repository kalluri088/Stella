# Research: could Stella run Outline for you?

Research only. Nothing in this file is implemented, and no server, service or
unit was started while it was written — every fact below came from reading
files or from read-only status queries.

## The question

Stella talks to the notes workspace over its HTTP API and treats that API as
the whole capability surface (AGENTS.md rule 16). That has one consequence
worth examining: **every Outline capability Stella has is conditioned on a
server that somebody else already started.** `build_outline_tools`
(`src/stella/outline_tools.py:1453`) builds a client from the environment,
probes `/healthz` with a quarter-second budget, and if the probe does not
answer `{"ok": true}` it returns an empty list. Not a disabled tool — no tool
at all. The same gate arms the reminder pump, so the due-alert sweep that
survived the removal of Stella's own reminder store
(`docs/REMINDERS.md`) is equally dormant.

On this machine, right now, that means the entire Outline surface is dark:

- nothing listens on `127.0.0.1:8741` (verified with `ss -ltnp` and a refused
  `curl` to `/healthz`);
- no `outline.service` unit is installed under `~/.config/systemd/user/` (only
  `voxtype.service` is there) and none in `/etc/systemd/system/`;
- `~/.local/share/outline/` does not exist, so there is no `outline.db`, no
  `outline.token`, and no first-run data directory yet.

So "let Stella run Outline" is not a convenience feature under discussion; it
is the difference between a documented capability that exists and one that is
reachable. A "remind me to X at 18:00" today resolves to an honest refusal on
this laptop, which is the correct behaviour but not a useful one.

The question this document answers is narrow: **what would it take, and should
it be Stella that starts the server?**

## What "run Outline" has to mean before it can mean anything

Four separable abilities, which the design has to keep apart:

1. **Start** — bring the API up so the capabilities exist.
2. **Stop** — take it down again (or decide never to own that).
3. **Status** — know whether it is up, already starting, or down on purpose.
4. **First run** — the data directory, the token file, and the fact that the
   very first boot creates all three.

Only the first is interesting. The others are where a careless design breaks
something.

## The facts about the thing being started

From `/home/yaswanth/Projects/Outline` (a sibling repository, not a Stella
dependency):

- **What it is.** A FastAPI app served by uvicorn, packaged as
  `outline-server` with the console script pointing at
  `outline_server.__main__:run` (`backend/pyproject.toml:18`). It is installed
  **editably into its own virtualenv**:
  `backend/.venv/bin/outline-server` exists, and its python is a uv-managed
  CPython 3.13. Importing `outline_server` from any *other* interpreter fails;
  a starter must use that venv (working directory alone does not substitute).
- **How it is meant to be started.** `deploy/outline.service` is a
  **systemd --user** unit whose real lines are
  `WorkingDirectory=__ROOT__` and `ExecStart=__VENV_PY__ -m outline_server`,
  with `Restart=on-failure` and `RestartSec=2`. The `__ROOT__` / `__VENV_PY__`
  placeholders are substituted by `scripts/install_units.py`, which installs
  into `~/.config/systemd/user` and runs
  `systemctl --user enable --now outline.service`. A macOS launchd plist
  (`RunAtLoad` + `KeepAlive`) and a Windows logon task are the same idea on
  other platforms. **None of these are installed here.**
- **Address and defaults.** Host defaults to `127.0.0.1`, port to **8741**
  (`backend/outline_server/config.py`), overridable with `OUTLINE_HOST` /
  `OUTLINE_PORT`. Stella's client mirrors the same default
  (`src/stella/outline_tools.py:58`), so no configuration is needed to agree
  on an address.
- **Data.** SQLite at `<data_dir>/outline.db` in WAL mode, `data_dir` from
  `OUTLINE_DATA_DIR` else the platform user-data dir
  (`~/.local/share/outline`). The server creates it on boot.
- **Authentication.** The server generates or reads a 64-hex token at
  `<data_dir>/outline.token` (`os.urandom(32)`, created `O_EXCL`, mode `0600`)
  and requires it as `X-Outline-Token` on every `/api/v1/*` route, compared
  with `hmac.compare_digest`. `GET /healthz` is deliberately unauthenticated.
  Stella already reads exactly that file (or `OUTLINE_TOKEN`), so a server
  Stella starts needs **no** secret passed by Stella — the token arrives on
  disk through the shared data-directory convention, which is the single most
  important fact in this document: **starting the server creates no
  credential problem.**
- **Idempotence and its trap.** A second copy takes an exclusive `flock` on
  `<data_dir>/outline.lock`, fails to acquire it, prints "Outline server is
  already running on http://…" — and **exits 0**. So a return code cannot
  distinguish "I started it" from "someone else already had it". The lock file
  doubles as the liveness signal: `scripts/restore_db.py` refuses to restore
  while the lock is held.
- **No on-demand machinery exists.** No socket activation, no watchdog, no
  autostart-on-request path. Today the only ways it comes up are the ones the
  user runs by hand.

## What Stella already knows how to do

Stella is not new to supervising a child process; it has exactly one precedent
worth copying, and it is a close match.

`src/stella/llama_server.py` manages the local model server:

- a **fixed port** from configuration (`STELLA_LLAMA_SERVER_PORT`, default
  8080), never a scanned one;
- `start()` **refuses to launch** if that port already serves a healthy server,
  raising a friendly error that names the remedy ("Stop that server or set
  STELLA_LLAMA_SERVER_PORT") — it does not take over, and does not kill;
- readiness by **polling the service's own health endpoint** every 0.5 s, with
  a generous ceiling (240 s) and early-exit detection through `poll()` plus the
  tail of a temp log it owns;
- shutdown as a **signal ladder**: SIGINT, wait, terminate, wait, kill, then
  delete its log;
- lifetime ownership stated in the code: the server starts *after* everything
  else exists, "so a failure anywhere below can never leave a server running",
  and `StellaApplication.close()` stops it;
- and a boot backstop, `childproc.sweep_orphaned_children()`, which kills
  children still marked `STELLA_CHILD_PID` from a previous crash.

Everything a supervised process needs in this codebase goes through
`childproc.guarded_popen` (`src/stella/childproc.py:71`): an **argv list, never
a shell**, a marked environment, and `PR_SET_PDEATHSIG` so the child dies with
Stella rather than becoming an orphan. The same pattern carries the recorder,
the player, the transcription and speech commands, barge-in capture, and the
judge runner. Launching Outline would be the seventh instance of an existing,
tested shape — not a new category.

## Where it collides with the standing rules

Three texts bear directly on this, and they are the reason the answer is not
"obviously yes":

- `docs/ARCHITECTURE.md:694` lists, under what is deliberately **not**
  implemented: "Plugin registries, permissions, external APIs, subprocesses,
  or shell execution", and the neighbouring bullet rules out schedulers,
  daemons and heartbeats of Stella's own.
- `docs/APPROVAL_BOUNDARY.md:101`: "No shell, network, process, or other
  dangerous production tool has been added."
- Rule 4, "tools are capabilities, not permissions", and rule 10, "dangerous
  actions require trusted approval".

The reading that matters is what those lines actually forbid. They forbid a
*model-reachable* process surface — a verb the Brain can call with arguments
it typed. They do not forbid the application layer from starting the
peripherals it needs to have the capabilities it already advertises: Stella
already launches `pw-record`, `espeak`, a whisper command and a model server,
and nobody calls that a shell. The line in this codebase is not "no
subprocesses"; it is "**no subprocess the model can ask for**".

That single distinction sorts every option below.

## The options

**A. Status quo — Stella never starts it.** Cheapest and already honest: no
server, no Outline tools, and a plain refusal where a reminder is asked for.
The cost is that a documented feature is dead unless the user remembers to
launch a server they configured once, on a machine where the unit is not even
installed. It also makes rule 16 (API-first parity) theoretical: the API is
the capability surface only while something serves it.

**B. Application-layer supervision, model-invisible (the llama-server shape).**
`build_application()` gains an optional step: if the user opted in by
environment (`STELLA_OUTLINE_MANAGED=on`, plus a path to the Outline checkout's
venv), and the configured address answers nothing, start
`<venv>/python -m outline_server` through `guarded_popen`, poll `/healthz`
until it does, and *then* run the existing `build_outline_tools` gate, which
will now pass. `StellaApplication.close()` stops it, or deliberately does not
(see "Who owns the stop" below). No new tool, no new verb, no change to the
three-tool count, no approval dialog — because the model never asked, the
runtime did, on a switch the user set.
This fits every standing rule, reuses a tested pattern, and keeps the failure
path identical to today's (no server → no tools → honest refusal). Its costs
are real but bounded: Stella's start-up gains seconds, Stella becomes a place a
long-lived service can be born, and the health probe currently runs **once**
during tool construction, so ordering matters (start → wait → build).

**C. Delegate to the service manager: `systemctl --user start outline`.**
Same trigger as B, one argv-list command, and afterwards the unit's own
`Restart=on-failure` and systemd's process supervision do the owning. Stella
would neither parent the server nor kill it, which solves the lifetime question
by refusing it. Costs: the unit must be installed first
(`Outline/scripts/install_units.py` does that, and is not run here), the
command must exist and the user manager must be running, and it is a
dependency on an init system Stella otherwise never touches — the portability
work in the `port/*` branches has been careful to keep Stella's surface the
same on every desktop. B works everywhere; C works where systemd --user lives.

**D. A model-visible `outline_server` tool with start/stop/status actions.**
The only option that lets a sentence like "restart my notes server" work. It
is also the one the rules most clearly reject: a DANGEROUS tool (rule 10) that
would need an approval card per press, on a name that argument-injection can
point at arbitrary paths if it accepts anything but a fixed argv —
and `docs/TOOL_EXECUTION.md` already says the same thing about `system_info`:
never let a SAFE tool grow into an OS surface. It would also break rule 16's
"exactly three tools" discipline for the Outline family, which is about
workspace content, not about the machine's process table. **Not
recommendable**; if it ever happens it happens as a *fixed* pair of application
commands behind an approval, not as a general action verb.

**E. On-demand / socket-activated start.** systemd socket activation or a tiny
Stella-side proxy that starts the server on the first request and lets it idle
out. Solves "running but unused" and adds a component neither repository has
ever had. Nothing today justifies it.

## Recommendation

**B, with C as the preferred form wherever a user service manager exists** —
and decided by configuration, not by a runtime probe that guesses.

The reasons are the ones the architecture already cares about:

- it preserves rule 3 exactly: the model proposes workspace content, the
  runtime owns the machinery;
- it adds **no capability** the model can reach, so no risk classification,
  approval flow, or audit vocabulary changes, and rule 16's three-tool count
  holds;
- every failure mode degrades to the behaviour that is *already specified*
  (no healthy server → no tools → an honest "I cannot schedule that"), which
  means it cannot make a wrong answer more confident;
- the parent dies with Stella (`PR_SET_PDEATHSIG`) so a crashed Stella cannot
  leak a server, and `sweep_orphaned_children()` is the existing backstop;
- the token problem does not exist, because the credential is a file both
  programs already agree on by convention, and Stella never has to see a
  secret move through an argv list or an environment block it assembles.

## What must be solved before any of it is built

Not open *questions* — open *requirements*, each with the fact that creates it:

1. **The already-running case must be treated as success, not failure.** A
   second copy exits **0** after refusing the lock, so the return code is
   meaningless. The starter must judge by the lock and by `/healthz`, exactly
   as `llama_server.start()` judges by its health endpoint.
2. **Never kill what you did not start.** If a server answers before Stella
   acts, Stella leaves it alone — the same refusal `llama_server` makes.
   "Stop Outline" is therefore not a thing Stella should do casually, and the
   lock file is how it knows whether it is the owner.
3. **Who owns the stop.** A notes server that dies when the assistant window
   closes is a worse user experience than one that stays up, and it would
   orphan a browser tab already talking to it. The honest choices are "Stella
   stops only what it started, on clean shutdown" or "Stella never stops it"
   (option C). This is a product decision for the user, not a technical one.
4. **Ordering against the one-shot health gate.** `build_outline_tools` probes
   once at construction; today that is correct and cheap. If a managed start is
   added, the start must precede that construction, and the readiness wait must
   be bounded and *visible* — a window that looks frozen for 20 s because a
   Python venv is importing FastAPI is the failure mode users report as
   "broken".
5. **The path problem.** Stella must be told where the Outline checkout lives
   (`…/backend/.venv/bin/python`); it cannot discover a sibling repository by
   guessing, and shipping the server inside Stella would break rule 16's
   two-thin-clients model.
6. **Port discipline.** 8741 is the shared default and must stay fixed-by-config
   rather than auto-scanned — the same rule the llama-server port follows, and
   the reason benchmark ports are never allowed to drift in this repo either.
7. **First boot creates real state.** `~/.local/share/outline/` does not exist
   here yet, so a managed start would create the data directory, an empty
   database and a fresh token. That is the server's own documented behaviour,
   but it means Stella would, for the first time, be responsible for a
   user's *data directory appearing*. Any implementation has to make that
   explicit at start-up (one line in the log: what it created and where), and
   must never delete or migrate it — the reminder store's own history
   (`docs/REMINDERS.md`) is the precedent for leaving data alone.
8. **Backup stays outside.** The 03:30 backup timer (`deploy/outline-backup.*`)
   belongs to the OS scheduler. Stella taking over backups would be exactly the
   "schedulers and daemons" line in `docs/ARCHITECTURE.md:694`.
9. **WAL and concurrency.** SQLite in WAL mode with a second process is fine for
   this workload, but a starter should not also start a *restore*:
   `restore_db.py` deliberately refuses while the lock is held, and that
   invariant is worth respecting rather than working around.
10. **The porting dimension.** The `port/*` work has been making Stella's
    behaviour uniform across Hyprland/X11/KDE/GNOME/Sway. A managed start that
    quietly only works under systemd --user would be a new kind of
    platform-specific promise; option B (plain argv child) is uniform, which is
    a second argument for B over C as the default.

## What would not change, whatever gets decided

The three Outline tools and their risk levels. The opt-in double gate
(`_client_from_environment` and `_healthz`). The reminder pump's claim-once
semantics. The honest-refusal rule for "remind me" with no workspace. The
absence of any shell, any model-supplied command line, and any tool that takes
a program name as an argument.

## Validation performed

All read-only, on 2026-10-02: `ss -ltnp` and `curl` against
`127.0.0.1:8741/healthz` (refused — no listener); `systemctl --user status
outline` and `systemctl status outline` (unit not found); `ls` of
`~/.config/systemd/user/`, `/etc/systemd/system/`,
`/var/lib/systemd/linger/` and `~/.local/share/outline` (no unit, no linger
file, no data directory); and direct reading of
`Outline/backend/outline_server/{__main__,config,auth,db,api/meta}.py`,
`Outline/backend/pyproject.toml`, `Outline/deploy/*`,
`Outline/scripts/{install_units,backup_db,restore_db}.py`, plus Stella's
`src/stella/{outline_tools,llama_server,childproc}.py`, `AGENTS.md` and the
`docs/` rules quoted above. Nothing was started, stopped, enabled, installed or
edited outside this document.
