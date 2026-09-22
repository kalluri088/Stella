# Stella Approval Boundary

## Current implementation

Stella now has the smallest trusted approval boundary needed to test
action-specific authorization without adding a user interface or a dangerous
production capability.

`RiskLevel` is application-owned. A tool classified as `DANGEROUS` requires
approval before execution. The current tools remain unchanged:

- `datetime`, `system_info`, and `echo` are `SAFE` and execute without
  approval;
- `filesystem_read` is `SENSITIVE`, remains explicitly workspace-scoped, and
  executes without interactive approval in this MVP;
- `filesystem_write`, `filesystem_edit`, and `filesystem_delete` are
  `DANGEROUS` and require exact trusted approval;
- `network_read` is `DANGEROUS` and requires exact trusted approval before an
  external connection;
- tests also use a private approval-required tool classified as `DANGEROUS`.

The model cannot provide a risk level, `approved` flag, or
`requires_approval` value. Unknown fields in the decision protocol do not
authorize an action.

## Action-specific approval

When trusted runtime code determines that approval is required, Stella creates
an `ApprovalRequest` containing the exact capability and structured arguments.
An application-provided approval callback may return a `ToolApproval` for that
request.

The dispatcher executes only when all of these conditions hold:

1. The capability is registered by application code.
2. The tool's arguments pass validation.
3. The tool's trusted risk classification requires approval.
4. A `ToolApproval` exists, is marked approved, and contains exactly the same
   capability and arguments as the current action.

Missing approval returns `Approval required.`. A false approval returns
`Approval denied.`. A wrong type or approval for another capability or
argument set returns `Invalid approval.`. None of these paths execute the
tool.

Approval is checked after argument validation and before `Tool.execute()`.
Tool failures after approval remain ordinary deterministic `ToolResult`
failures; approval does not authorize retries or another action.

## Runtime flow

```text
LLM proposes capability + arguments
  -> exact trusted dispatcher lookup
  -> tool argument validation
  -> trusted risk classification
  -> Stella requests action-specific approval when required
  -> dispatcher verifies the exact approval
  -> tool execution
  -> trusted outcome verification inside the tool
  -> ToolResult (+ ActionReceipt when the tool mutated state)
```

The approval callback is application-controlled. It is not called for safe
tools, and there is no CLI prompt yet. If a required approval provider is
absent, execution fails closed.

## Validation results

Focused tests and a live `gpt-5.6` validation used a test-only `DANGEROUS`
capability named `approval_test`:

- without approval: `Approval required.`, zero tool executions;
- exact application approval: successful result, one tool execution;
- rejected approval: `Approval denied.`, zero tool executions;
- approval for different arguments: `Invalid approval.`, zero tool
  executions;
- a model response containing `approved: true`: still failed closed because
  the field is not part of the trusted runtime approval path;
- `echo` with no approval provider: executed successfully.

Validation also found and fixed a concrete defect: non-boolean truthy values
such as `"yes"` are now rejected as invalid approval rather than being
treated as approval.

## Intentionally not implemented

This boundary does not provide an approval UI, persistent approval storage,
identity, authentication, permissions, audit logging, risk analysis based on
complex arguments, sandboxing, or least-privilege execution. It is not a
general policy engine.

No shell, network, process, or other dangerous production tool has been added.
`filesystem_write` is create-only, `filesystem_edit` replaces the full
contents of one existing regular file, and `filesystem_delete` deletes only one
existing regular workspace file; all three use this boundary. Future approval
must be single-action and identity-aware before Stella supports multiple users
or broader meaningful external side effects.

## Minimal CLI approval interaction

The interactive CLI now supplies a small application-owned approval callback
when a `DANGEROUS` action reaches Stella and no other approval provider was
configured. It displays the exact capability and validated argument object:

```text
Approval required for action: capability='approval_test', arguments={"value": "x"}
```

It then accepts only `yes` or `approve` (case-insensitively, after trimming)
as approval. Those responses create `ToolApproval(request, approved=True)` in
the CLI application code. `no` and every other response create an explicit
rejection, so the dispatcher does not execute the action. The LLM's structured
response is never used as an approval.

Safe and sensitive tools do not prompt and continue to execute through their
existing trusted dispatch path. The CLI interaction is synchronous and
single-action; it is not a persistent approval store, permissions framework,
identity system, or approval UI for other clients.

## CLI validation results

Focused tests cover `yes`, `approve`, `no`, and arbitrary invalid input, exact
action display, and execution of a test-only dangerous tool only after an
approved response. The full suite passed with 137 tests, and Ruff passed.

A live `gpt-5.6` run used the test-only `approval_test` dangerous capability.
The model proposed:

```text
capability=approval_test, arguments={"value": "LIVE_CLI_APPROVAL"}
```

The CLI displayed that action, accepted `yes`, and the tool executed once.
The observed final response was `Live approval action executed successfully.`
No production dangerous capability was added, and no API secret was exposed
or recorded.

## Filesystem write approval

`filesystem_write` is the first production `DANGEROUS` capability. It accepts
only a relative workspace path and UTF-8 text content, rejects existing
destinations, and requires an approval whose `ApprovalRequest` exactly matches
both values. The CLI's approval prompt displays both validated arguments. The
LLM cannot create or bypass that approval.

`filesystem_delete` is also `DANGEROUS`. It accepts only one relative
workspace path, rejects traversal, wildcard characters, directories, missing
files, and symlinks, and requires an approval whose request exactly matches
the validated path. The CLI displays that path before accepting only an
explicit `yes` or `approve`. It never performs recursive deletion or glob
expansion.

## Verified outcomes

Approval authorizes execution only; it never proves the result. Every
filesystem mutation independently verifies the resulting state with trusted
application code before Stella reports success: create and edit re-read the
file and compare the exact expected bytes, and delete confirms the path is
absent. A mutation whose verification fails or is inconclusive is returned as
a failed `ToolResult` with an honest receipt status (`unverified` /
`inconclusive`) and reaches the model as a failure, never as a success.
Rejections and failures are likewise deterministic (`File is outside
workspace.`, `File was not found.`, `Invalid tool arguments.`,
`Approval required.`/`denied.`/`Invalid approval.`). Verification is
application flow inside `Tool.execute()`, not a model decision or tool step,
and neither a verified receipt nor file contents or tool output grant any
authority for later actions — each dangerous action still requires its own
exact approval.
