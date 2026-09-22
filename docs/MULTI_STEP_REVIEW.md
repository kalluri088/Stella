# Stella Multi-Step Tool Loop Review

## Scope

This document records the design and implementation of the smallest safe
extension from one tool action to a bounded sequence of tool actions followed
by a final response.

## Current implementation

Today `Stella.process()` performs one decision cycle:

```text
Context
  -> memory retrieval
  -> Brain.decide()
  -> one of ANSWER, ASK, TOOL, or DO_NOTHING
```

For `TOOL`, Stella sends the proposed capability and arguments through the
application-owned `ToolDispatcher`. The dispatcher performs exact capability
lookup, tool argument validation, trusted risk evaluation, exact approval
checking when required, and execution. It returns a `ToolResult`, which Stella
sends to one final LLM call for natural-language response generation. That
final call is explicitly not another decision.

Before the loop, `StellaResult` exposed one final `Decision` and one
`ToolResult`, and `Context` carried user input, conversation history, and
retrieved memories but no tool observations. `ToolDispatcher` creates a trusted
in-memory `AuditRecord` for every dispatch attempt, including failed,
unavailable, rejected, and successful attempts.

Before the implementation described below, the flow did not ask the Brain to
interpret a `ToolResult`, so a tool could not lead to another tool call in the
same `process()` invocation.

## Implemented minimal design

`Stella.process()` now contains a bounded tool-step loop. It keeps the existing
decision protocol and dispatcher boundary:

```text
Context
  -> Brain.decide()
  -> TOOL decision
  -> trusted ToolDispatcher + approval
  -> ToolResult + AuditRecord
  -> Brain.decide() with explicit tool observation
  -> ... up to a fixed bound
  -> ANSWER final-response call
```

The Brain still proposes only a capability and structured arguments. Stella
still decides whether the capability is registered, the dispatcher still
validates arguments and risk, and the approval provider still creates the only
trusted approval. The loop must never call a tool directly from model output.

### Explicit tool observations

The next Brain context carries a small structured tool observation rather than
hiding tool output in ordinary user conversation history. The observation
contains:

- the selected capability;
- the validated arguments, where available; and
- `ToolResult.success` and `ToolResult.output`.

`Context.tool_observations` is the provider-agnostic representation. The
runtime serializes it distinctly in `LLMBrain`'s decision payload and clearly
labels tool output as untrusted data. The prompt instructs the model not to
follow instructions found inside a tool result and to propose only currently
available capabilities.

The final answer call receives the original context plus the complete
bounded tool-observation history and the selected final `ANSWER` decision. It
must remain a response-generation call, not a hidden third decision path.

### Step bound

`Stella` accepts an explicit `max_tool_steps` setting. The bound counts actual
tool dispatch attempts, including failed or approval-denied attempts, and is
checked before each new tool execution. The default is `1`, which preserves
today's behavior exactly; an explicitly configured value such as `3` enables
bounded multi-step use without changing existing callers unexpectedly.

If the Brain proposes another `TOOL` after the bound is reached, Stella must
stop deterministically without executing it. It should return a structured
result indicating that the step limit was reached rather than making an
unbounded final decision or silently dropping the request. The result should
expose the completed step trace and the last tool result where one exists.

The bound is a safety limit, not a retry scheduler. Stella does not retry a
tool independently; it only executes a later tool call when the Brain returns
another `TOOL` decision. Repeated proposals still consume the bounded step
budget and cannot continue indefinitely.

### Observability

The existing dispatcher audit record is sufficient for each attempted tool
execution and remains the only execution audit boundary. Brain decisions are
observable through `StellaResult.step_trace`, an ordered list of `StellaStep`
values containing each decision and any resulting tool observation. No logging
framework or second audit system was added.

Each dangerous step must independently request approval for its exact
capability and validated arguments. Approval must never carry over from a
previous step, and a model-provided approval field must remain ignored.

## Preservation of current behavior

The implementation preserves these cases:

- `ANSWER`: one decision and one final answer-generation call;
- `ASK`: return the requested information without tool execution;
- `DO_NOTHING`: return without tool or answer-generation calls;
- one `TOOL` with the default step bound: the existing tool-result response
  behavior;
- safe and sensitive tools: no approval prompt;
- dangerous tools: the existing exact approval and fail-closed behavior.

The CLI should not gain autonomous behavior. It should continue to process one
user input at a time; the bounded loop exists only within that one synchronous
interaction.

## Memory behavior

The loop must not automatically store tool results, intermediate Brain text,
or conversation messages. A memory write remains valid only when the Brain
explicitly returns a `MemoryWriteRequest`. The implementation permits at most
one explicit memory write per interaction, with no implicit writes from tool
observations.

## Security boundaries

Tool results, retrieved memories, and conversation text are model input, not
runtime authorization. A malicious tool result may try to instruct the model
to select a dangerous capability, but the next proposal must still pass exact
dispatcher lookup, tool validation, trusted risk classification, and approval.

The loop must not:

- let the model change the step limit;
- let the model classify risk or grant approval;
- bypass the dispatcher for subsequent steps;
- execute arbitrary code based on tool output;
- convert a natural-language final answer into another action; or
- continue after the bound in the background.

## Alternatives considered

### Reuse conversation messages

Appending a `role="tool"` message to `conversation_history` is the smallest
mechanical change, but it conflates user conversation with runtime execution
history and makes it less clear which data is untrusted tool output. It is not
recommended as the primary representation.

### Add a generic agent framework

An agent framework would add planning, retries, registration, and lifecycle
behavior that Stella does not need to prove this capability. It would weaken
the project's current small trusted boundary and is explicitly not
recommended.

### Let one structured LLM response contain a plan

A multi-action plan would move several future actions into untrusted model
output at once, complicate approval and step accounting, and make it harder to
stop after each result. Sequential decision-making is safer and easier to
observe for the current MVP.

## Implementation results

- `Context.tool_observations` carries capability, arguments, success, and
  output as structured data to the next Brain decision.
- `LLMBrain` serializes those observations separately from conversation history
  and is instructed to treat tool output as untrusted data.
- `StellaResult.step_trace` exposes each decision and tool result in order.
- `max_tool_steps` defaults to `1`; values such as `2` or `3` enable bounded
  multi-step execution for explicit callers.
- A tool proposal after the bound returns the deterministic response
  `I reached the maximum number of tool steps.` without executing the tool or
  making a final LLM call.
- Every executed step uses `_execute_tool()`, the existing dispatcher, trusted
  risk classification, approval provider, and dispatcher `AuditRecord`.

Focused tests cover one-step compatibility, two-step feedback, failed-tool
feedback, approval-required steps, step-limit stopping, structured
observations, and bound validation. The full suite passes with 172 tests and
Ruff passes.

## Real-world validation

The live validation used `gpt-5.6`, a temporary workspace containing
`notes.txt`, and the existing CLI approval callback. The request asked Stella
to read `notes.txt` first and then create `summary.txt` from its contents.

### Approved two-step workflow

The model selected `filesystem_read` with:

```json
{"path": "notes.txt"}
```

The read succeeded. The next Brain decision saw the structured successful
observation and selected `filesystem_write` with the exact relative path and
content derived from the file:

```json
{
  "path": "summary.txt",
  "content": "Stella's launch review is Friday; the team should bring the blue draft.\n"
}
```

The CLI displayed that action and required approval. After `yes`, the write
succeeded. The ordered step trace contained `filesystem_read`,
`filesystem_write`, and the final `ANSWER` decision. The dispatcher audit
records showed a successful `SENSITIVE` read without approval and a successful
`DANGEROUS` write with approval. The final response reported the completed
workflow, and independent inspection found the expected summary file and
content.

### Rejected write

The same workflow was run with `no` at the write approval prompt. The read
still succeeded, the write appeared in the trace with a failed
`ToolResult(success=False, output="Approval denied.")`, and its audit record
showed `approval_required=true`, `approval_granted=false`, and
`execution_success=false`. No `summary.txt` file was created. The final
response accurately reported that approval was denied.

### `max_tool_steps=1`

The same request was run with `max_tool_steps=1`. The read executed and the
existing single-step final-response path ran. The second Brain/tool decision
did not occur, no write approval was requested, only the read appeared in the
step trace and audit records, and no `summary.txt` file was created.

No production defect was found during live validation, so no production-code
changes were required for this validation task. The API key was inherited by
the process but was not displayed or recorded.

## Intentionally not implemented

This implementation does not add retries, parallel tools,
planning, autonomous loops, background work, multiple agents, persistent audit
storage, approval reuse, or new capabilities.
