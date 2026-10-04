# Stella Shell Commands

Stella can now run a program for you: install a dependency, build
something, drive a local tool by its command line. This is the single
`shell_run` capability, added as the assistant's `Bash`/`sandbox` equivalent.
It is, by design, the most heavily fenced tool the runtime has. Read this
before switching it on.

## What it is

`shell_run` runs **one** command string through `/bin/sh -c` (or `cmd /c` on
Windows) and returns its combined stdout+stderr and exit status. Where
bubblewrap is present the command first runs inside a filesystem jail (see
"What sandbox means here"); where it is not, the command runs confined to the
Stella workspace as its starting directory, and the tool says so. There is
exactly one verb — there is no `shell_list`, `shell_write`, or `shell_delete`;
whatever those would do is already covered by the dedicated filesystem tools,
which are easier to approve because they name one file each.

## The four fences

1. **On in the desktop app; off for headless unless you turn it on.**
   `shell_tools_enabled` defaults to **on** for the saved/desktop configuration,
   so the *Shell commands* checkbox in Settings starts ticked; the bare and
   environment (`STELLA_MODEL`, `stella voice`) paths keep it **off**, and while
   off the model never sees the capability — it is not registered, so
   `DO_NOTHING` is the only shell-shaped answer it can give. Turn it back off
   with the checkbox (saved) or for one launch with `STELLA_SHELL_TOOLS=off`;
   arm a headless run with `STELLA_SHELL_TOOLS=on` (env wins over the saved box,
   in both directions). Offering the tool never runs anything — fence 2 still
   asks for the literal command every single time.
2. **Every single use asks.** The capability's floor is
   `RiskLevel.DANGEROUS`, which is what makes the dispatcher stop and request a
   trusted approval before anything runs. You see the **literal command** and a
   plain-language summary of it in the approval prompt, and the working
   directory note. The model cannot pre-approve, batch-approve, or answer for
   you.
3. **It starts in your workspace, and it is bounded.** The working directory is
   the Stella workspace; stdin is closed so the command can never wait on a
   terminal and hang Stella; output is read under a **64 KB** cap (past that
   the result says so, and the extra bytes are discarded, not buffered, so a
   chatty command cannot grow Stella's memory); and a **120 s** wall-clock
   timeout takes the whole process group down — a shell that forked a build
   that forked a compiler all die together, not just the shell.
4. **Its output is data, not instruction.** A command's own stdout is wrapped
   in the same `<<<UNTRUSTED_WEB_CONTENT>>>` markers as web fetches and
   defanged against marker forgery. A build log that echoed adversarial text
   pulled off the network can never masquerade as a new instruction back to the
   runtime.

## What "sandbox" means here — a real jail when bubblewrap is present

When `bwrap` (bubblewrap — the sandbox Flatpak runs under, present by default
on this Arch/systemd desktop) is available, `shell_run` executes the command
**inside a filesystem jail**, not merely from a confined directory. What the
jail does:

- **Read-only host, one writable exception.** The whole filesystem is mounted
  read-only inside the box; only the Stella workspace (and a private `/tmp`
  scratch) are writable. A mistaken or malicious approved command cannot edit
  or delete a file outside the workspace — the host root is literally a
  read-only mount.
- **Your home is hidden.** `/home` is replaced by an empty tmpfs and only the
  workspace is re-bound back, so `~/.ssh`, browser profiles, other projects
  and your dotfiles are simply absent. A private, writable `HOME`
  (`/home/stella`) is provided so tools that insist on one still run, isolated.
- **Session sockets are out of reach.** `/run` is masked by an empty tmpfs
  too. Mounting the host read-only still lets a command *connect* to the
  D-Bus and Wayland sockets parked under `/run`, so without this mask a jailed
  command could talk to your real session bus or compositor; now it can
  neither — desktop actions go through the dedicated, separately approved
  desktop adapters instead.
- **Privilege is dropped.** The command loses every supplementary group
  (docker, kvm, libvirt, wheel…), so it cannot reach the daemon groups that
  would let it escalate beyond the ordinary account.
- **Isolated namespaces.** A new user, PID, mount, UTS and IPC namespace: the
  command cannot see or signal processes outside the box, and
  `--die-with-parent` guarantees it goes away with Stella.
- **Network, per command.** Outbound networking is allowed by default (an
  approved command is expected to be able to `git pull`), and a command can
  request `network: false` to run with no network at all. The approval card
  says which either way.

This is **defense in depth**, not a claim that a command is safe to run
unapproved — the human approval of the literal command is still the authority
(rules 3 and 10). And it is **not** a promise against a determined kernel
exploit: for that, run Stella itself in a container or VM. The jail shrinks the
blast radius *given* that a command runs; approval decides whether it runs.

**When bubblewrap is absent**, the jail cannot be applied. Stella then falls
back to the confined-starting-directory behaviour described in the four fences
and **says so honestly** — both on the approval card ("the jail is NOT
active") and in the result note ("ran WITHOUT the filesystem jail"). It never
claims a jail it does not have. To fall back deliberately even when bwrap is
present, set `STELLA_SHELL_SANDBOX=off` for the launch.

## Under the headless voice path (`stella voice`)

The same approval fires with no screen, read aloud and **fail-closed**: Stella
speaks a bounded summary (the capability and a short preview line — **never**
the full argument body, so a large file write is not read to the room), then
listens for your answer. Only an unambiguous "yes" (or yeah / sure / go / do
it) approves. "No", a garbled answer, or silence in that window all deny. The
voice path grants no extra authority — it is the identical `run_turn` and the
identical `ToolDispatcher` check as a typed turn.

## Bounds (trusted, not model-adjustable)

| Bound | Value | Effect when hit |
| --- | --- | --- |
| Command length | 8,000 chars | rejected at validation, before any approval |
| Working directory | Stella workspace | command's `cwd` |
| stdout+stderr captured | 64,000 bytes | result announces truncation |
| Wall clock | 120 seconds | whole group SIGINT→SIGTERM→SIGKILL |
| Preview shown on approval | 60 lines / 4,000 chars | announced as truncated |
| Filesystem (jail active) | host read-only except workspace + `/tmp` | write outside the workspace fails in-box |
| ulimit safety nets (jail) | 120 s CPU · 1024 procs · 128 MiB file | a runaway stops before the timeout |

The filesystem jail and ulimit rows apply only when bubblewrap is present;
without it the first five fences still hold, and the result says the jail was
unavailable.

## How to enable it

It is **already on in the desktop app** — the *Shell commands (run programs in
your workspace)* checkbox in Settings starts ticked. A `STELLA_MODEL` or
`stella voice` (headless) run keeps it off; arm one for a single launch with
`STELLA_SHELL_TOOLS=on stella`. Either way each command still asks. To switch
the desktop default back off, untick the box (persisted), or use
`STELLA_SHELL_TOOLS=off`, which forces it off for that launch no matter what the
box says.

The bubblewrap jail is **on whenever the shell capability is on and `bwrap` is
present** — the safe default. To run confined-only even when bwrap is available,
`STELLA_SHELL_SANDBOX=off` for the launch. There is no config field for the
jail: it is an execution policy, controlled by that one env knob, never a value
that reaches the model.

## Capability map for the requested tool set

The ask was for the standard agent tools — Bash, Read, Write, Edit, Glob,
Grep, WebFetch, WebSearch, ImageSearch, ImageGen, DeliverArtifacts. Against
Stella's tool families:

| Requested | Stella has it as |
| --- | --- |
| Bash | **`shell_run`** (this document) — new |
| Read | `filesystem_read` |
| Write | `filesystem_write` |
| Edit | `filesystem_edit` |
| Glob | `workspace_find` |
| Grep | `workspace_search` |
| WebFetch | `network_read` (and `web_fetch` when web is on) |
| WebSearch | `web_search` (when web is on) |
| DeliverArtifacts | the filesystem tools write the file and `shell_run` can move it; Stella names the workspace path back to you. No separate tool — the workspace *is* the outbox. |

**Intentionally not added yet:** `ImageGen` and `ImageSearch`. Both need a
third-party API key and spend real credits per call, and both send prompts off
the machine. Stella's doctrine is that a model-dependent or network-dependent
behavior is not "done" until it has been validated against the real thing; I
cannot verify a live image API without your key and without spending, so I did
not ship a half-tested tool that quietly returns fake or wrong images. If you
want them, give me the provider and key and I will add them behind the same
per-use approval as the web tools.

## Security invariants this change preserves

- The command **string** is an approval-surface value only; it is never written
  to `config.json`. Only the on/off **bool** is persisted.
- No secret value is ever printed in a result, summary, or notice. Output is
  untrusted-wrapped and defanged before it reaches the model.
- The kill path is bounded (≈ seconds) and uses the existing
  `stella.childproc` parent-death guarantee, so a killed command cannot outlive
  Stella if Stella itself is SIGKILLed.
- **No new dependency, and nothing downloaded.** `shell_run` is pure standard
  library (`subprocess`, `selectors`, `signal`, `os`). The jail adds none
  either: `stella/sandbox.py` only assembles a `bwrap` command line for a
  binary the system already ships, and if that binary is missing the tool falls
  back to the confined path and says so — it never requires or installs
  anything.
- The jail's command line carries the user's command as a **trailing positional
  argument** and re-execs it via `/bin/sh -c "$1" sh`; the command string is
  never pasted into the wrapper, so quotes, `$`, and `;` in a command cannot
  rewrite the sandbox invocation.

## Validation

`tests/test_shell_tools.py` proves, through an injected fake runner and a
scripted fake sandbox, every decision (validation incl. the `network` flag,
jail-vs-confined selection, the fallback note when bubblewrap is absent, the
workspace confinement, the success/exit/timeout/truncation notes,
untrusted-marker wrapping and forgery, the approval gate, and the config
round-trip) without depending on this host's tooling; then runs the real
bounded reader against a few tiny, workspace-confined commands to prove the
byte cap and the wall-clock group-kill actually work; and, only on a host that
has bubblewrap, runs a **real jailed command** to prove the host root is
read-only and the real home is hidden inside the box.

`tests/test_sandbox.py` proves the argv assembly itself — read-only root with
the workspace as the single writable bind, the `/home` mask ordered before the
workspace re-bind, the network toggle, the ulimit nets, and that the command is
a trailing positional and not interpolated into the wrapper — plus a real
sub-`bwrap` echo when the binary is present. `tests/test_settings_wiring.py`
drift-guards that all six wiring touchpoints for the shell flag are present.
