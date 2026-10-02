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
`ToolResult(False, "Tool capability unavailable.")` without validation or
execution; Stella never falls back to another tool. Tool exceptions become the
deterministic `Tool execution failed.` result so the failure is visible to the
final response path without leaking stack traces, and Stella does not retry.

The loop is bounded (`max_tool_steps`, CLI composes 2): the Brain may iterate
tool → observation → decision within that cap, and hitting the limit produces
a deterministic capped result, never an unbounded loop. Each dangerous action
inside the loop still requires its own exact approval. See `ARCHITECTURE.md`
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
| `memory_write` / `memory_update` / `memory_forget` | DANGEROUS | `OUTCOME_MEMORY.md` |
| `persona_edit` | DANGEROUS (two files only) | `PERSONA.md` |

`EchoTool` exists in `tools.py` but is **deliberately not registered**: a
registered echo capability lets a confused model "succeed" by echoing the
user's own text, which reads as a fake assistant response (found during
dogfooding; see the comment at the composition root). It remains a test
fixture only.

## Decision-prompt routing rules

`LLMBrain` receives the same dispatcher as Stella and serializes approved tool
metadata (name, description, argument schema) into the decision prompt. The
prompt explains when each safe capability is relevant, but that text is not
authorization — the exact runtime lookup is. The model must not answer from
general knowledge when the result depends on live runtime or workspace state:
current time/date routes to `datetime`, machine information to `system_info`,
file reads to `filesystem_read`/`workspace_*`; it must not claim a file is
missing without letting the selected capability run, must not invent arguments,
and if the needed capability is unavailable or arguments are incomplete it
must ask or do nothing rather than guess. A `ToolResult` exists only after
trusted dispatch returns it; model prose cannot fabricate one. The routing
gate is repeated after the serialized tool inventory in the actual system
message, so the model sees the capabilities and the mandatory-choice rule
together immediately before the user context.

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
