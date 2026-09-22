# Stella Risk Classification and Approval Review

## Scope

This is a design review of the current Stella implementation before adding
tools that can modify the system or cause external side effects. It covers
the current `Tool`, `ToolDispatcher`, `Decision`, `Stella`, `ToolResult`,
memory, CLI, tests, and security documentation.

The risk/approval boundary described near the end of this document has now
been implemented in its smallest form. No dangerous production tool or
approval UI has been added.

## CURRENT IMPLEMENTATION

### Current execution boundary

The current flow is:

```text
LLM response
  -> LLMBrain parses Decision
  -> Stella receives capability and arguments
  -> ToolDispatcher performs exact lookup
  -> Tool.validate_arguments()
  -> trusted risk/approval boundary
  -> Tool.execute()
  -> ToolResult
  -> final LLM response
```

The LLM controls only untrusted proposal data: the decision kind, capability
string, arguments, content, and optional memory-write request. It does not
register tools, instantiate tools, or authorize itself.

Trusted application code selects the dispatcher contents. `ToolDispatcher`
performs exact capability lookup, rejects unavailable capabilities, rejects
duplicate names during registration, and invokes validation before execution.
`Stella` is the orchestration boundary that performs memory writes and asks
the dispatcher to execute a selected capability.

### Current risk posture

The active tools are:

- `datetime`: reads the host local clock;
- `system_info`: reads a small subset of hostname, platform, and CPU data;
- `echo`: returns a supplied string.
- `filesystem_read`: reads bounded UTF-8 text below the configured workspace.

They are read-only or deterministic and have no shell, network,
process-launch, credential, or external side effect. `filesystem_read` is
classified `SENSITIVE` but remains approval-free because its workspace scope
is explicit and it does not mutate data. They do not require user approval in
the current production MVP.

The current code has a small trusted `RiskLevel` classification and an
action-specific `ApprovalRequest`/`ToolApproval` boundary. There is no
approval UI, persistent approval state, identity, authorization framework,
sandbox, or audit framework. The current production tools remain safe or
workspace-scoped and do not require approval.

### Current validation responsibilities

`LLMBrain` validates the outer structured decision shape. Each tool validates
its own exact argument structure. The dispatcher makes capability availability
an application fact rather than a prompt claim. These checks establish data
shape and dispatch boundaries; they do not determine whether a future action
is appropriate, approved, or safe for a particular user.

## DESIGN PROPOSAL

### 1. Small risk model

Stella should eventually use a small three-level model:

- `SAFE`: bounded, read-only, local, and low impact;
- `SENSITIVE`: may expose information or access a resource without directly
  modifying it;
- `DANGEROUS`: modifies data, launches code or processes, communicates
  externally, or creates a meaningful, irreversible, or user-visible side
  effect.

This is sufficient as an initial policy vocabulary. It is not a claim that
all actions within one level are identical. The policy must still make the
final decision for the concrete action.

### 2. Tool risk versus action risk

Risk should not be only a static property of a tool name. A tool can expose
operations with different risk, and validated arguments can change the risk.
The smallest useful future model is therefore:

```text
tool/capability baseline
  + selected operation
  + validated arguments
  -> trusted action-risk evaluation
```

Examples:

| Capability | Operation or argument | Likely risk |
| --- | --- | --- |
| filesystem | read an approved public file | sensitive or safe, depending on policy |
| filesystem | read a credential file | sensitive/dangerous due to disclosure |
| filesystem | write or delete a file | dangerous |
| shell | fixed harmless diagnostic command | sensitive or dangerous, depending on policy |
| shell | destructive command or arbitrary command text | dangerous |
| network | fetch an approved public endpoint | sensitive |
| network | reach localhost/internal services or send data | dangerous or denied |
| messaging | draft without sending | safe/sensitive |
| messaging | send a message | dangerous or approval-required |

The tool should validate argument shape and normalize the action. A trusted
risk policy should then evaluate the normalized capability, operation, and
arguments. The LLM must not provide the risk level or override the result.

Risk evaluation should happen after capability lookup and tool validation but
before execution. This ensures that the policy sees the exact application
tool and validated arguments rather than an untrusted model representation.

### 3. Approval boundary

The intended future flow is:

```text
LLM proposes capability and arguments
  -> Brain parses proposal
  -> trusted dispatcher looks up capability
  -> tool validates and normalizes arguments
  -> trusted runtime evaluates action risk
  -> approval required: trusted application asks the user
  -> user approves or rejects
  -> tool executes only when policy and approval allow it
```

Approval belongs in trusted application orchestration around the dispatch
boundary, not in `LLMBrain` and not inside the tool's interpretation of model
prose. The tool must never execute before approval. The approval surface
should receive the exact capability, operation, arguments, target, and
meaningful side effect—not a general statement such as “allow tools.”

The approval mechanism may eventually be injected into `Stella` or a small
trusted runtime policy component. It should be an application decision point,
not a general-purpose model-facing abstraction.

#### Implemented smallest boundary

`ToolDispatcher` derives approval need from the trusted tool risk level:
`DANGEROUS` requires approval. `Stella` optionally calls an application-owned
approval provider with an exact `ApprovalRequest`. The dispatcher verifies
that the returned `ToolApproval` is approved and matches the current
capability and arguments exactly. Missing, rejected, malformed, or mismatched
approval fails closed before `Tool.execute()`.

The test-only approval-required action proves this path without introducing a
system-affecting production capability. `filesystem_read` remains
`SENSITIVE` and approval-free because it is explicitly workspace-scoped and
read-only.

### 4. The model cannot authorize itself

Fields such as:

```json
{"approved": true}
```

or:

```json
{"requires_approval": false}
```

must be ignored or rejected as authorization data. They are claims supplied
by the same untrusted model that proposed the action. A prompt injection or a
model error could set them to bypass safety controls.

The structured `Decision` should continue to represent a proposal only. The
runtime must derive risk, approval requirements, and authorization from
application policy, validated arguments, and eventually authenticated user
identity. No model-produced field can grant permission.

### 5. Approval semantics

The future runtime should behave deterministically:

| Situation | Required behavior |
| --- | --- |
| safe action | execute after normal capability and argument checks |
| dangerous action | stop before execution and request approval |
| user approves | record the approval for this exact action, then execute |
| user rejects | do not execute; return a clear rejected result |
| approval unavailable | fail closed; do not execute |
| malformed approval state | fail closed; do not guess or reuse approval |
| tool fails after approval | return the tool failure; do not retry implicitly |

Approval should be specific to the action and, unless a future policy says
otherwise, single-use. It must not silently authorize a later action with
different arguments. Rejection or unavailable approval must not fall back to
another tool or reinterpret the request as a shell command.

### 6. Separation of argument risk and validation

Responsibilities should remain separate:

- `Tool.validate_arguments()` checks whether the structured input matches the
  tool's contract: required fields, types, allowed values, extra keys, and
  basic limits.
- A trusted risk policy evaluates what the validated action means in context:
  target paths, destinations, command allowlists, data sensitivity, and
  side-effect level.
- Approval policy determines whether a user must explicitly authorize that
  evaluated action.
- The execution environment enforces least privilege and sandbox limits even
  if application code contains a bug.

Validation must not silently rewrite a dangerous request into a safer-looking
one. Risk policy must not assume that a valid schema implies authorization.

## FUTURE REQUIREMENTS

### Recommended implementation order

1. Define a concrete capability's strict argument and operation schema.
2. Add a small trusted risk classification/evaluation boundary that sees the
   exact validated action.
3. Add explicit approval for actions classified as dangerous or otherwise
   privacy-sensitive, external, destructive, or irreversible.
4. Add capability-specific controls: filesystem roots and path handling,
   command allowlists, or network/egress policy as applicable.
5. Run side-effecting operations with least privilege, resource limits,
   timeouts, and sandbox/process isolation where the threat requires it.
6. Add minimal audit records for risk decisions, approval, rejection, and
   execution outcome without recording secrets unnecessarily.
7. Add authenticated identity and bind memory ownership, authorization, and
   approval to that identity before multi-user support.

Risk classification and approval come before a dangerous tool because they
define whether execution is allowed. Capability-specific validation and
scoping must be designed before the tool exists. Least privilege and
sandboxing protect the operating system if application controls fail. Audit
logging makes the boundary inspectable, while identity is required before
approval or memory ownership can be attributed to more than one user.

### System-affecting categories

Before a filesystem tool, define an allowed root, canonicalization,
traversal/symlink rules, sensitive-file policy, size limits, and approval for
writes and deletes.

Before a shell or process tool, avoid arbitrary shell strings; use an explicit
executable/argument allowlist, a restricted environment and working
directory, time/resource limits, low privilege, sandboxing, and approval.

Before a network tool, define destination and egress policy, internal and
metadata-address restrictions, redirect handling, credential isolation,
response limits, and approval for sending data or causing external effects.

Before a messaging or other external-action tool, define exact recipients and
payload boundaries, secret/PII handling, approval immediately before send,
and a clear execution result.

### Multi-user implications

Approval and authorization eventually require authenticated identity. A user
ID in a prompt, memory item, or LLM response is not authentication. The
runtime will need to bind the approval request, tool policy, and memory
database access to the authenticated user, with isolation between users.

The current single-owner SQLite model should not be extended to shared users
by merely adding a user field without defining authentication, migration,
ownership, and access enforcement.

### Eventual abstraction impact

The first minimal changes are now implemented. When a dangerous capability is introduced,
the likely impact is:

- `Tool`: may need explicit operation metadata, normalized-action output, or
  a risk-relevant description; it should not be responsible for user approval.
- `ToolDispatcher`: remains the exact trusted lookup boundary and now reads
  risk and verifies action-specific approval after validation.
- `Decision`: should continue to carry only the LLM proposal. Do not add
  trusted approval fields to it.
- `Stella`: will coordinate policy evaluation, approval, and execution, or
  delegate those steps to a small trusted runtime component.
- `ToolResult`: may remain the execution result, while rejection or pending
  approval may eventually need a separate structured orchestration result so
  “not authorized” is not confused with “tool failed.”

The existing `ToolResult(success, output)` is adequate for current tools and
does not need to be expanded speculatively.

## INTENTIONALLY NOT IMPLEMENTED

This review does not add:

- a risk enum, risk engine, policy engine, or authorization framework;
- approval UI, persistent approval storage, or identity-aware approval state;
- filesystem, shell, process, network, messaging, or other dangerous tools;
- sandboxing, containers, seccomp, or OS isolation;
- authentication, identity, multi-user memory isolation, or accounts;
- audit logging;
- autonomous loops, retries, background execution, or multi-agent behavior.

The current `datetime`, `system_info`, and `echo` capabilities remain
approval-free and unchanged.

## Recommendation

Adopt the three-level `SAFE` / `SENSITIVE` / `DANGEROUS` vocabulary as a
design concept, but evaluate risk on the concrete validated action rather than
on a capability name alone. Put evaluation and approval in trusted runtime
code between tool validation and execution. Keep model output limited to a
proposal and fail closed for missing, malformed, rejected, or unavailable
approval.

The next implementation should be a narrowly scoped trusted risk/approval
boundary designed around one concrete capability—not a general permissions
framework. Until that boundary exists, the recommended next capability is
another harmless read-only capability or no new tool at all.
