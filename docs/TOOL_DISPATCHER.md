# Stella Tool Dispatcher

## Why it was introduced

Stella now has four explicitly composed capabilities: `datetime`,
`system_info`, `echo`, and workspace-scoped `filesystem_read`. The original
one-injected-tool design made each capability work, but it
could expose only one of them at a time. A small application-owned collection
is now justified by that concrete requirement.

`ToolDispatcher` is deliberately only a collection and exact dispatcher. It
is not a plugin framework, registry service, agent planner, or permission
system.

## Lookup and execution

The application constructs the dispatcher with approved `Tool` objects:

```text
ToolDispatcher([DateTimeTool(), SystemInfoTool(), EchoTool(), FileSystemReadTool(...)])
```

Each tool's `name` is its capability identifier. Registration stores the
object under that exact name. Duplicate names raise a deterministic
`ValueError`; the later tool is not silently substituted.

At runtime:

```text
Brain proposes capability and arguments
  -> ToolDispatcher exact capability lookup
  -> Tool.validate_arguments()
  -> trusted RiskLevel classification
  -> Tool.execute()
  -> ToolResult
```

Unknown, missing, or mismatched capabilities return
`ToolResult(False, "Tool capability unavailable.")` without validation or
execution. Valid capabilities still pass through the tool's own strict
argument validator. Tool exceptions become the existing deterministic
`Tool execution failed.` result.

The LLM cannot register tools, create tool objects, or alter the dispatcher.
Registration occurs only in trusted application composition code.

## Decision prompt

`LLMBrain` receives the same dispatcher as Stella and serializes its approved
tool metadata into the decision prompt. The model can see each capability,
description, and argument schema:

- `datetime`: date/time operations using `{"kind":"date|time|datetime|weekday"}`
- `system_info`: hostname/platform/CPU operations using
  `{"kind":"hostname|platform|cpu"}`
- `echo`: message echo using `{"message":"string"}`
- `filesystem_read`: a relative UTF-8 text-file path below the configured
  workspace using `{"path":"relative UTF-8 text-file path"}`

The prompt explains when each safe capability is relevant, but that text is
not authorization. Stella's exact runtime lookup is the authorization
boundary.

The decision contract also requires tool selection when the requested result
depends on live runtime or workspace state. Current time/date requests route
to `datetime`, local machine information routes to `system_info`, and file
reads route to `filesystem_read`. The model must not answer these requests
from general knowledge or claim that a file is missing without first allowing
the selected capability to run. A tool result exists only after trusted
runtime dispatch returns it; model prose cannot fabricate one. If the required
capability is unavailable or its arguments are incomplete, the model must ask
or do nothing rather than guess.

Focused regression tests cover current time, hostname, and workspace-file
requests and verify that each produces a structured `TOOL` decision with the
expected capability and arguments. This strengthens routing only; dispatcher
lookup, validation, risk, approval, and audit boundaries are unchanged.

The routing gate is repeated after the serialized tool inventory in the actual
system message. This placement is intentional: the model sees the available
capabilities and the mandatory choice rule together immediately before the
user context. The provider boundary forwards that system message unchanged;
the regression test covers the exact message conversion used by the CLI.

## Current composition

The normal CLI injects all four current tools into one dispatcher. Stella
still executes at most one tool per interaction and performs no retries or
follow-up tool selection.

Existing callers that inject one `Tool` directly remain supported by Stella;
it wraps that tool in a one-item dispatcher. This keeps the change narrow while
allowing the application composition root to use the explicit collection.

## Security boundaries and limits

The dispatcher guarantees only that execution is routed to an application-
registered exact capability and that tool-level validation happens first. It
also exposes trusted risk classification; the model cannot set or override
it. `filesystem_read` is `SENSITIVE` but currently executes without
interactive approval because it is workspace-scoped and read-only. The
dispatcher requires an exact application-produced approval for
`DANGEROUS` tools and fails closed when it is missing, rejected, malformed, or
for a different action. It does not provide authentication, user permissions,
an approval UI, sandboxing, resource limits, plugin isolation, or protection
from prompt injection.

The current tools are read-only or deterministic. `filesystem_read` accesses
only the configured workspace and does not access arbitrary filesystem paths,
shells, networks, or secrets.

## Real-world validation

With the real OpenAI-backed CLI and model `gpt-5.6`, one session tested the
three original approved capabilities. Filesystem real-world validation is
recorded separately with the filesystem capability documentation.

- `What time is it?` produced capability `datetime` with
  `{"kind":"time"}`. The final response reported the current local time
  with its UTC offset.
- A platform question produced capability `system_info` with
  `{"kind":"platform"}`. The final response used the returned platform
  information.
- An exact-marker echo request produced capability `echo` with the expected
  `{"message":"..."}` argument. The final response returned the marker.

The debug decisions confirmed exact capability selection, and each final
response used the corresponding `ToolResult`. No API key was exposed or
recorded.

## Intentionally not implemented

- plugin loading or dynamic discovery;
- model-controlled registration;
- multiple-step execution or autonomous loops;
- retries, approval UI, permissions, authentication, or sandboxing;
- filesystem writes/deletes, shell, network, or other external-action tools;
- a general policy or authorization framework.
