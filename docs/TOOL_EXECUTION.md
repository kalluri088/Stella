# Stella Tool Execution & Dispatcher

One specification for how capabilities are composed, dispatched, validated,
approved, executed, and reported. It replaces the earlier separate
`TOOL_DISPATCHER.md`, `DATETIME_TOOL.md`, and `SYSTEM_INFO_TOOL.md` documents
(their content is preserved here in current form).

Related specs: `APPROVAL_BOUNDARY.md` (approval mechanics, previews, receipts),
`AUDIT_LOGGING.md` (what is recorded per attempt), `FILESYSTEM.md`,
`WORKSPACE_ASSISTANT.md`, `NETWORK_READ.md`, `REMINDERS.md`, `PERSONA.md`,
`OUTCOME_MEMORY.md`.

## ToolDispatcher — exact, application-owned, fail-closed

`ToolDispatcher` is deliberately only a collection and exact dispatcher: not a
plugin framework, registry service, agent planner, or permission system. The
application composition root (`src/stella/app.py`) constructs it with approved
`Tool` objects; the LLM cannot register tools, create tool objects, or alter
the dispatcher. Duplicate capability names raise a deterministic `ValueError`
rather than silently substituting.

At runtime:

```text
Brain proposes capability + arguments
  -> ToolDispatcher exact capability lookup
  -> Tool.validate_arguments()
  -> trusted RiskLevel classification (from the application-owned tool)
  -> (DANGEROUS) exact application ApprovalRequest/ToolApproval handshake
  -> Tool.execute()
  -> ToolResult -> observation fed back into the bounded tool loop
```

Unknown, missing, or mismatched capabilities return
`ToolResult(False, capability_not_registered_output(name))` without validation or
execution; Stella never falls back to another tool. Tool exceptions become the
deterministic `Tool execution failed.` result so the failure is visible to the
final response path without leaking stack traces, and Stella does not retry.

### A wrapper is not an unknown capability — and the rejection is not a fact about her

Measured on her router brain (#278, 2026-10-16, 112 production turns across three
passes): a candidate sometimes proposes a call whose function name is the generic
`tool` instead of a capability. Two shapes arrived, both from the same candidate:

```
tool:{"capability": "workspace_list", "arguments": {"dir": ""}}   # named inside
tool:{"path": "notes.txt"}                                        # not named at all
```

Taken literally each is an unknown capability: refused in ~0.3 ms, and the next
candidate — a fresh draw, with no idea that the refusal was about a *name* — said
the rejection back in first person. *"the workspace listing capability is
unavailable on this machine"*, *"the system_info tool for CPU information is
unavailable"*, *"I can't read notes.txt — the filesystem read tool isn't available
right now"*. **Of the 7 wrapper rejections observed across the three passes and
report 107, 5 ended with her claiming a capability she has does not exist on this
machine**, and before the change that happened on 2 of the 20 turns that could
simply have been answered. Nothing had failed: in the first shape the right
capability had been named correctly and thrown away, and in the second the runtime
had described its own refusal as an unavailability and been quoted back.

So the change has two halves, and each is the smallest thing that answers the part
it covers.

**1. Read the name out of the envelope.** `LLMBrain._decision_from_tool_call`
unwraps the first shape **only when the call's own name is not registered and the
name inside it is**, and strips a nested `tool_final` exactly as it strips a
top-level one. The second shape is deliberately not guessed at: `path` is an argument of
`filesystem_read`, `filesystem_write` **and** `filesystem_delete`, so inferring the
capability from the argument keys would let a confused proposal pick which of her
tools runs — the thing rule 3 forbids and #258 already refused to do, now with a
safety reason rather than only a tidiness one. What is unchanged, and is why this
is not a new permission:

- The inner name goes through the same exact lookup, argument validation,
  `RiskLevel` classification, approval handshake and audit as a directly-named
  proposal. Every capability reachable through a wrapper is reachable by naming it
  plainly; the dispatcher's authority surface does not move.
- A call whose own name **is** registered keeps it, so model text cannot re-point a
  real call by nesting a different capability in its arguments
  (`tests/test_brain.py::test_a_registered_call_is_never_redirected_by_a_nested_name`).
- A wrapper naming something she does not have is still dispatched as the wrapper
  name and still refused, so #258's guards own it unchanged.
- `do_nothing` arriving as a function name is deliberately **not** mapped onto a
  real `DO_NOTHING` decision. It would be faithful to the model's intent and would
  end the turn with silence, which #244 exists to prevent. It reached the dispatcher
  7 times across the three passes and every turn then continued: one `list-dir` went
  rejected → `workspace_list` → the right answer in 3.4 s. So the current path costs
  one hosted round trip and produces words. Recorded as intentionally not done.

**2. Say the rejection so that it cannot be read as a missing capability.** The
first half leaves the un-guessable shape reachable, and its outcome was still the
same lie. The wording is not cosmetic because the decision prompt *requires* the
model to carry the observation's reason into its answer — "a `kind=answer` response
MUST state plainly that the action did not succeed and give the reason from that
observation" — so the runtime's sentence is her sentence, verbatim, to the owner.
`capability_not_registered_output()` is now the single source of that sentence, and
it says only what is true of the proposal:

> `No capability named "tool" is registered here, so nothing ran and nothing
> changed. That says nothing about the capabilities you were given this turn, which
> are all still available: name one of them exactly as it was given, or answer
> without a tool.`

The refusal of an unregistered name no longer contains the word *unavailable* at
all, and a test holds that
(`tests/test_tools.py::test_rejecting_a_name_never_reports_a_capability_as_unavailable`).
The quoted name is the proposal, which is untrusted input (rule 6), and this string
is both fed back into the next request and rendered as the turn's outcome line — so
it is flattened to printable text on one line and cut to `QUOTED_CAPABILITY_LIMIT`
(80) rather than echoed whole. The per-tool failure strings (`Date/time unavailable.`,
`System information unavailable.`) are a different claim — about a value this
machine did not return, not about a capability — and are unchanged. Auditing is
untouched: an unregistered name still records no arguments and still executes
nothing.

**What the two halves measured.** Turns that could have been answered and ended in
the false claim went **2 of 20 → 1 of 16 → 0 of 20** across the three passes; every
wrapper that named its capability (5 of 5 in the fixed tree) now dispatches as that
capability, and both of the final pass's un-named rejections were followed by a
correctly-named call and a true answer. Small numbers on a drifting free-tier pool —
the mechanism is the finding, the rates are the indication. Report 108 holds the
turn-by-turn evidence.

### One step, one capability: what happens to the rest of a reply

A native reply may carry more than one tool call. The runtime dispatches the
first and stops there, because one step is one capability and one observation
(#264).

What it does **not** do is run the others. Executing a second proposal on the
model's behalf would take an action the model might well have cancelled once it
saw the first result — for anything outbound or irreversible that means an
approval demanded and spent for a call that should never have been made, and it
burns turn budget the model did not ask to spend. The runtime authorizes and
executes; it does not complete a half-written intention.

What it does instead is *say so*, in two places:

- the trace event for that decision carries `dropped_capabilities`, and
  `stella --trace` renders them as `[not run: workspace_list]`, so the owner
  sees that half an ask went unhandled. Names only, like the argument keys
  beside them — a call that never ran has no arguments to publish.
- the next decision's request payload gains an `unhandled_proposals` object
  built by the runtime, naming each proposal with its arguments, and stating
  that none of them ran and that re-asking is the model's decision.

That report is deliberately **not** shaped like an observation. It has no
`success` and no `output` key, so nothing can read as the result of a call that
was never dispatched; and unlike `tool_observations`, whose output the runtime
truncates and quotes as untrusted data, this is a report *about the model's own
proposal*. It rides in the per-turn request payload, never in the system rules,
so a reply that happened to carry two calls does not move the byte-stable
prefix the provider caches.

The list is capped (`MAX_DROPPED_PROPOSALS`, 4) because a reply is
model-shaped data; when a reply carried more than the list shows, the report
also says `unhandled_count`, so being capped is never being hidden. The report
is made once: the next reply that carries a single call clears it, so a
proposal is mentioned rather than repeated for the rest of the turn.

The loop is bounded by two turn budgets the owner sets in Settings —
`max_tool_steps` tool calls (default 8) and a wall-clock bound
(default 90 s) consulted only between steps (`ARCHITECTURE.md`, *Turn
budgets*): the Brain may iterate tool → observation → decision within those
bounds, and hitting either produces a deterministic honest stop naming what
ran, never an unbounded loop. Each dangerous action inside the loop still
requires its own exact approval. See `ARCHITECTURE.md`
for the orchestration details.

## Registered capabilities (as composed in app.py)

| Capability | Risk | Owning spec |
|---|---|---|
| `datetime` | SAFE | this doc, below |
| `system_info` | SAFE | this doc, below |
| `filesystem_read` | SENSITIVE | `FILESYSTEM.md` |
| `filesystem_write` / `filesystem_edit` / `filesystem_delete` | DANGEROUS | `FILESYSTEM.md` |
| `workspace_list` / `workspace_find` / `workspace_search` | SENSITIVE (inherited from read) | `WORKSPACE_ASSISTANT.md` |
| `network_read` | DANGEROUS | `NETWORK_READ.md` |
| `web_search` / `web_fetch` | DANGEROUS (only registered when the web capability is enabled) | `WEB.md` |
| `memory_list` | SENSITIVE | `OUTCOME_MEMORY.md` |
| `memory_write` | SENSITIVE — a fact the owner just said stores without asking; the same call proposed after tool observations is gated by the runtime's provenance guard (#246) | `OUTCOME_MEMORY.md` |
| `memory_update` / `memory_forget` | DANGEROUS | `OUTCOME_MEMORY.md` |
| `persona_edit` | DANGEROUS (two files only) | `PERSONA.md` |

`EchoTool` exists in `tools.py` but is **deliberately not registered**: a
registered echo capability lets a confused model "succeed" by echoing the
user's own text, which reads as a fake assistant response (found during
dogfooding; see the comment at the composition root). It remains a test
fixture only.

## Decision-prompt routing rules

`LLMBrain` receives the same dispatcher as Stella and passes approved tool
metadata (name, description, argument schema) to the provider, which carries
it on the wire as its own `tools` array. A client that cannot do that — the
base `LLMClient` protocol, and every test double built on it — is instead
handed the same metadata serialized as a "Currently available tools:" block
in the prompt; `LLMClient.declares_tools_natively` is the client's promise
about which of the two it is, and `LLMBrain` never sends both. The prompt
explains when each safe capability is relevant, but that text is not
authorization — the exact runtime lookup is. The model must not answer from
general knowledge when the result depends on live runtime or workspace state:
current time/date routes to `datetime`, machine information to `system_info`,
file reads to `filesystem_read`/`workspace_*`; it must not claim a file is
missing without letting the selected capability run, must not invent arguments,
and if the needed capability is unavailable or arguments are incomplete it
must ask or do nothing rather than guess. A `ToolResult` exists only after
trusted dispatch returns it; model prose cannot fabricate one. The routing
gate is the last text in the actual system message either way, and it names
"the capabilities available to you this turn" rather than pointing at a list
that may not be there.

### Why the registry travels once

Backlog item #229 was filed as "load capability descriptions lazily instead
of every turn", under latency. It is closed as "stop transmitting them
twice", and no discovery layer was built.

Two measurements decided that. The first: prefill is cached and costs about
0.07 s, so loading the registry lazily could at best recover the ~0.9 s of a
*cold* prefix — machinery buying a tenth of a second on a warm turn. The
second: the same `ToolDispatcher.describe()` output went out every turn as
prompt prose *and* as the provider's `tools` array — 1,198 tokens of
duplicate (2,172 with the OS, shell and browser families registered), which
is how a recall turn reached 8,151 tokens against `num_ctx=8192` and died
with `exceed_context_size_error`. The problem was capacity, and one source
of truth per transport fixes it; a lazy-discovery layer would have left the
duplicate in place and added a component the architecture rules already
exclude.

The A/B that justified shipping it — 17 tool-dependent turns, two passes,
both arms from one tree with only the client's promise flipped, against real
`qwen3:4b` — is recorded in `MODELS.md` under *The routing A/B that let this
ship*.

## The two SAFE informational tools

`DateTimeTool` (`datetime`) accepts exactly one argument `kind` from a fixed
allowlist: `date`, `time`, `datetime`, `weekday`. Missing, extra, unsupported,
or incorrectly typed arguments return the deterministic
`Invalid tool arguments.` before any date/time inspection — and the tool
performs the same check when called directly, so callers never rely on Stella
to enforce the contract. Unexpected runtime failures return
`Date/time unavailable.`. It uses only the standard library, reads no
environment variables, never shells out, and reports the **host process's**
local timezone with a numeric UTC offset; Stella has no trustworthy user
timezone source and does not infer one from prompts, memory, hostname, or
location. A future user timezone setting needs a clear source and ownership
rule first.

`SystemInfoTool` (`system_info`) accepts exactly `{"kind":
"hostname|platform|cpu"}` using the standard-library `platform`/`os`
functions; CPU output is the reported processor description plus core count.
Runtime failures contain as `System information unavailable.`. It must never
be expanded to accept arbitrary commands, environment-variable names, paths,
or file queries — that would turn a SAFE tool into an OS surface.

Both are read-only; their output is still treated as untrusted tool data in
the final response call. Real-model validation at their milestones confirmed
the full Brain → exact-capability check → argument validation → execution →
final-response path (e.g. `What time is it?` → `{"kind":"time"}` → local time
with offset); logs of those runs live in git history, not here.

## Terminal tools and the `tool_final` fast path

A `kind=tool` decision may carry the reserved marker `"tool_final": true`
(when calling a tool directly, the same signal is a reserved argument the
runtime strips before execution). It is the model *proposing* that this one
observation is the whole answer; whether anything may be rendered from it
verbatim is a runtime property: `Tool.terminal`, defined in the
application-owned tool class, default `False`. A model field named
`terminal` is not read and grants nothing.

When the marker is set, the call is the turn's first and only tool step, the
observation succeeded, and the tool declares itself terminal
(`datetime`, `system_info`), the response is built directly
from that observation — for terminal tools rendered verbatim — and the
middle re-decision call is skipped. A failed observation, a non-terminal
tool, or a multi-step plan keeps the honest synthesis path unchanged.
Approval, risk classification, memory-write gating, tool-output limits,
step trace and audit records all run before the fast path and are never
affected by it: `tool_final` can make a turn cheaper, never more powerful.
`Tool.terminal` is the one place tool output reaches the user without model
synthesis, so it is an opt-in trust decision: only capabilities whose
successful output is fully constructed by trusted code — a value from a
fixed allowlist (`datetime`, `system_info`) — may set it. A tool that could
echo fetched or file-sourced text stays on the synthesized path.

Measured on the owner's configured brain, the marker is never volunteered: `auto`
set `tool_final` on **0 of 16** tool turns (report 107), so in production today
this fast path is a local-brain path — it runs when a brain chooses the signal and
saves the second hosted round trip when it does. The runtime does not decide
finality itself, and that is deliberate rather than unfinished. `Tool.terminal`
states *this output may be shown as written*; it does not state *this call is the
whole request*. Treating the registry's declaration as the second statement would
end "What time is it? Remind me in ten minutes." on the time and never propose the
reminder — a turn measured 2.5× faster (3,968 → 1,570 ms median) that has lost a
capability, and the rendered string would be a bare field value (`Friday`) that the
voice path then speaks. Only the model knows whether it intends another step after
this observation, so only the model may say that it does not; the skip stays the
offer the runtime accepts when the proposal arrives.

**That reasoning has now been tested from the other side, and it holds (#279).** Two
facts had to be separated: whether the zero was a *channel* problem — the decision
prompt invites `"tool_final": true` as a reserved argument, while every capability is
declared with `additionalProperties: false` and all its arguments `required`, so the
native call cannot express the invited argument — and whether candidates use the
signal *correctly* when they can. A two-arm live screen answered both. Adding the
optional boolean to the two terminal schemas and nothing else raised the marker rate
from **0 of 16** turns to **4 of 16**, so the schema gap explains the zero but not a
low rate: twelve turns ignored the invitation even written into the declaration. And
of the four markers, **the two set on a two-part request were both wrong** — "what
time is it, and how many CPUs does this machine have?" finalised on the time and never
asked the CPU count, "what time is it? then read notes.txt" finalised on the time and
never read the file. Two of eight exposed two-part turns lost half of the owner's
request; zero of eight shipped-arm two-part turns did. One further use came on a call
the dispatcher had just *refused*, where the marker spent the turn's re-decision — the
step report 108 measured as its chance to recover — on skipping it.

So the schema is not changed, for the reason the paragraph above already gives, now
with the measurement behind it: giving the model a legal way to claim finality is not
the same as being able to rely on the claim, and the failure is silent in exactly the
direction speed metrics reward — a turn that stops doing half the work is a fast turn.
The three variants that would still ship the win were each declined: the runtime
guessing how many parts an English request has (rules 13 and 15), accepting the marker
only from routes that use it well (a hosted router's behaviour would become a security
property of this machine, on a pool that redraws per request), and widening every
schema's `additionalProperties` to allow reserved arguments. What is true and free
stays as it was: the fast path runs when a brain volunteers the marker and means it.
Report 109 records the screen, its four markers and its limits.

## Verification & receipts (mutating tools)

Every filesystem, memory, persona, and network mutation verifies its
resulting state with trusted application code before reporting success and
returns a bounded `ActionReceipt(action, status, size_bytes)` on its
`ToolResult`. Statuses are `verified`, `unverified`, `inconclusive`, `failed`,
`missing`, `invalid`. A success claim always means the resulting state was
verified; contradictory or uninspectable results fail honestly
(`unverified`/`inconclusive`). Verification happens inside the tool's own
`execute()` as a controlled application flow — never a separate model decision
or recursive tool step — and an unverified receipt reaches the model as a
failed `ToolResult`. A verified receipt grants no authority for any later
action, and file/page contents never enter the receipt or trace.

## Security boundaries and what the dispatcher does NOT provide

The dispatcher guarantees only that execution routes to an application-
registered exact capability, that tool-level validation happens first, and
that trusted risk classification cannot be set or overridden by the model
(a model field such as `approved: true` is ignored). `SENSITIVE` tools execute
without interactive approval where the application has scoped them safely
(`filesystem_read`/`workspace_*` are workspace-scoped and read-only; this is an
application decision, not a model decision). `DANGEROUS` tools require an exact
application-produced approval and fail closed when it is missing, rejected,
malformed, or for a different action — see `APPROVAL_BOUNDARY.md`.

Not provided by design: authentication, user permissions, plugin loading or
dynamic discovery, model-controlled registration, sandboxing, resource limits
or timeouts, retries, background execution, autonomous loops beyond the bound,
general policy engines, and protection from prompt injection in tool output —
tool results are untrusted data at every step.
