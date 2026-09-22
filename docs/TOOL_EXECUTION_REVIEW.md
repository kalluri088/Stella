# Tool Execution Review

## Scope

This review covers the current tool model and its execution path. It is based
on:

- `src/stella/tools.py`
- `src/stella/stella.py`
- `src/stella/brain.py`
- `tests/test_tools.py`
- tool and orchestration tests in `tests/test_stella.py` and `tests/test_brain.py`
- `docs/ARCHITECTURE.md`
- `docs/ARCHITECTURE_REVIEW.md`
- `docs/MEMORY_IDENTITY_REVIEW.md`

No production code was changed for this review.

## Observed behavior

### 1. What happens for a `TOOL` decision

`Stella.process()` first retrieves memories and asks the Brain for a
`Decision`. It performs any explicit memory write requested by that decision.
If the decision kind is `TOOL`, Stella then executes the one tool supplied to
its constructor:

```text
Context
  -> Memory.retrieve()
  -> Brain.decide()
  -> optional explicit Memory.store()
  -> injected Tool.execute(arguments)
  -> ToolResult
  -> LLM final-response call
  -> StellaResult
```

After execution, the LLM receives the original interaction context, selected
tool decision, and `ToolResult` to produce final response text. This is one
response-generation call, not a second decision. There is no automatic loop.

### 2. How Stella selects the tool

It does not select a tool by name. `Stella` has one `tool` attribute and calls
that object for every `TOOL` decision. The `Decision` contains no tool-name
field. Therefore the injected tool is selected at composition time, not by the
Brain's decision.

The normal CLI injects `EchoTool`. A test can inject another `Tool`
implementation, but there is still only one available tool for a Stella
instance.

### 3. Tool arguments

`Decision.arguments` is typed as `dict[str, object] | None`. The LLM decision
parser accepts a JSON object for `arguments` and rejects non-dictionary values.
When executing a tool, Stella passes `decision.arguments or {}` directly to
`Tool.execute()`.

The `Tool` interface requires:

```text
name -> str
description -> str
execute(arguments: dict[str, object]) -> ToolResult
```

`ToolResult` is an immutable `{success: bool, output: str}` value. The current
`EchoTool` reads the `message` argument and returns it as successful output.

### 4. Missing tools

There is no missing-tool path in the current implementation. Because Stella
always has one injected `Tool`, a parsed `TOOL` decision always invokes that
object. A requested name cannot be missing because no requested name exists.

The decision prompt now describes the one available `EchoTool` and exposes its
expected argument shape, `{"message":"string"}`. The protocol still has no
tool identifier or available-tool set, so a model cannot request a specific
alternative tool.

### 5. Tool failures

There are two current failure behaviors:

- A tool can return `ToolResult(success=False, output=...)`; Stella returns
  that result in `StellaResult.tool_result`.
- If `Tool.execute()` raises an exception, Stella converts it to the
  deterministic failed result `Tool execution failed.` and sends that result
  through the final response path.

The interface therefore supports an explicit failure result, while Stella
provides a minimal deterministic exception boundary. There is still no retry
behavior or broader error framework. `EchoTool` itself can raise, for example,
if its required `message` argument is absent.

### 6. Whether tool results influence the final response

Tool results influence the structured return value and final response. Stella
sends the result to the LLM, and the CLI displays the resulting natural-
language response. The original `ToolResult` remains available as
`result.tool_result`.

In the CLI, the displayed tool output is added to the next turn's in-memory
conversation history as an assistant message. That can make it available to a
later Brain decision. The current tool branch now performs exactly one final
response-generation call after execution.

### 7. Multiple tools later

The current design does not support multiple tools cleanly. The limitations
are concrete:

- `Stella` accepts one `Tool`, not a collection or registry.
- `Decision` has no tool name or identifier.
- The LLM protocol has no available-tool descriptions.
- There is no lookup, unknown-tool result, permission check, or dispatch rule.

Multiple tools could be added later without changing the low-level
`Tool.execute()` shape, but the Brain/Decision protocol and Stella's
composition/dispatch boundary would need an explicit design.

### 8. Sufficiency of the current `Tool` interface

The interface is sufficient for the current MVP's narrow goal: represent one
bounded operation with metadata, accept structured arguments, and return a
deterministic result. It keeps tools provider-agnostic and prevents the LLM
from directly executing arbitrary code or shell commands.

It is not sufficient for a broader tool system. It does not define argument
schemas, tool identity in a decision, cancellation, timeouts, exceptions,
permissions, side-effect descriptions, or lifecycle behavior. Those omissions
are acceptable while the project has only `EchoTool` and one injected tool.

## Recommendations

### Smallest useful next step

Prove one bounded tool end to end with an explicit tool contract, using the
existing `EchoTool` first. The smallest meaningful proof should establish that
the Brain's structured tool decision reaches the intended tool with the
intended arguments and that the resulting `ToolResult` reaches the user-facing
CLI output. The existing unit test proves the core deterministic orchestration
path, but real-model validation is still needed to confirm reliable tool
selection and argument generation.

The tool-result response loop and the single `EchoTool` argument schema are
implemented and covered with deterministic tests. The next focused validation
should establish whether a real model reliably produces the current tool
decision and `message` argument. A registry or multiple-tool set should be
addressed only if a second concrete tool justifies it.

Tool selection by name and a collection of tools should wait until there is a
second concrete tool to justify that boundary.

### Tool failures

For the current MVP, do not add retries, permissions, or a general exception
framework. Expected failures are returned as `ToolResult(success=False, ...)`,
and unexpected exceptions are converted by Stella to one deterministic failed
result before final response generation.

## Keep out of scope

The current MVP should not yet add:

- a tool registry or plugin system;
- multiple-tool planning or autonomous tool loops;
- arbitrary code, shell, or subprocess execution;
- external APIs or real-world side-effecting tools;
- permissions, authentication, or user-specific tool policy;
- retries, background jobs, streaming, cancellation, or timeouts;
- automatic tool-result memory writes;
- additional LLM calls beyond the one final response call for a tool result;
- an agent framework.

The current boundary is intentionally small: the Brain proposes a tool action,
Stella invokes the explicitly injected bounded tool, and the result is returned
without allowing the tool or LLM to choose further actions.
