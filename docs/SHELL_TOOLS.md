# Stella Shell Commands

Stella can now run a program for you: install a dependency, build
something, drive a local tool by its command line. This is the single
`shell_run` capability, added as the assistant's `Bash`/`sandbox` equivalent.
It is, by design, the most heavily fenced tool the runtime has. Read this
before switching it on.

## What it is

`shell_run` runs **one** command string through `/bin/sh -c` (or `cmd /c` on
Windows), inside the Stella workspace, and returns the command's combined
stdout+stderr and its exit status. There is exactly one verb — there is no
`shell_list`, `shell_write`, or `shell_delete`; whatever those would do is
already covered by the dedicated filesystem tools, which are easier to approve
because they name one file each.

## The four fences

1. **Off until you switch it on.** `shell_tools_enabled` defaults to **False**.
   While off the model never sees the capability — it is not registered, so
   `DO_NOTHING` is the only shell-shaped answer it can give. Turn it on with
   the *Shell commands* checkbox in Settings, or for one launch with
   `STELLA_SHELL_TOOLS=on` (env wins over the saved box, in both directions).
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

## What "sandbox" honestly means here

This is a confined **starting** directory, a hard **resource bound**, and a
**human who approves the exact command each time** — model proposes, runtime
authorizes. It is **not** kernel-level isolation. The command runs as *you*, so
it can still `cd` out of the workspace and touch anything your own account can
read or write, unless something else (permissions, a container) stops it. The
guard is your eyes on the literal command, not a filesystem jail.

If you want true isolation, run Stella itself inside a container or a VM;
with that in place `shell_run` becomes a convenience behind your existing
boundary. Without it, `shell_run` is still safe in the only way this design can
promise: nothing runs that you did not approve by name, one command at a time.

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

## How to enable it

Settings → the *Shell commands (run programs in your workspace)* checkbox →
Apply. Or one launch: `STELLA_SHELL_TOOLS=on stella`. Either way each command
still asks. If you would rather not keep it enabled, `STELLA_SHELL_TOOLS=off`
forces it off for that launch no matter what the box says.

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
- No new hard dependency: `shell_run` is pure standard library (`subprocess`,
  `selectors`, `signal`, `os`).

## Validation

`tests/test_shell_tools.py` proves, through an injected fake runner, every
decision (validation, workspace confinement, the success/exit/timeout/
truncation notes, untrusted-marker wrapping and forgery, the approval gate, and
the config round-trip) without spawning a process; and then runs the real
bounded reader against a few tiny, workspace-confined commands to prove the
byte cap and the wall-clock group-kill actually work. `tests/test_settings_wiring.py`
drift-guards that all six wiring touchpoints for the flag are present.
