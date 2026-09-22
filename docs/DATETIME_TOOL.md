# Date and Time Capability

## What was added

`DateTimeTool` is Stella's read-only `datetime` capability. It supports only
these exact argument objects:

```json
{"kind":"date"}
{"kind":"time"}
{"kind":"datetime"}
{"kind":"weekday"}
```

It returns a `ToolResult` containing the current date, time, local datetime, or
weekday. Unsupported, missing, extra, or incorrectly typed arguments return
the existing deterministic `Invalid tool arguments.` result. Unexpected
runtime failures return `Date/time unavailable.`.

## Why this capability

Current date/time is a small, genuinely useful assistant capability. It is
read-only, deterministic at execution time, and does not require a network,
secret, file, shell, or external service. It fits the existing
Brain → Stella capability check → Tool validation → Tool execution flow.

## Timezone behavior

The tool calls Python's `datetime.now().astimezone()`, so it uses the local
timezone configured for the host process and includes a numeric UTC offset for
the `time` and `datetime` operations. Stella currently has no authenticated or
trustworthy user timezone source. It therefore does not pretend to know a
remote user's timezone or infer one from prompts, memory, hostname, or
location. If the host is configured for UTC, the result is UTC; otherwise it
uses the host's configured local timezone.

This is an explicit MVP limitation. A future user timezone setting should be
introduced only with a clear source and ownership rule.

## Architecture and active capability

The default CLI now injects a `ToolDispatcher` containing `DateTimeTool`,
`SystemInfoTool`, and `EchoTool`. `datetime` is therefore one of three
application-approved capabilities. The dispatcher is a small exact-name
collection, not a plugin registry or dynamic discovery system.

The LLM may propose `datetime`, but Stella authorizes it only by exact
comparison with the injected tool's name. The model cannot authorize or select
an executable object directly. The final LLM call receives the resulting
`ToolResult` and generates the user-facing response.

## Security boundaries

The tool reads only standard-library date/time state. It does not read
environment variables, access secrets, execute shell commands, inspect files,
make network requests, or perform external side effects. Its argument schema
is a fixed allowlist. Tool output remains untrusted data when supplied to the
final LLM response-generation call.

No permissions, authentication, approval workflow, sandboxing, autonomous
loop, retry behavior, or general environment-context abstraction was added.

## Validation

Focused tests cover all four valid operations, invalid and malformed
arguments, runtime failure handling, capability dispatch, Stella orchestration,
and delivery of the `ToolResult` to final response synthesis.

Real-world validation with the configured OpenAI-backed CLI and model
`gpt-5.6` used the request `What time is it?`. The model selected:

```json
{"kind":"tool","capability":"datetime","arguments":{"kind":"time"},"memory_write":null}
```

Stella authorized the capability, the tool returned the current host-local
time with its numeric offset, and the final response was:

```text
It’s 5:51:20 PM (UTC+05:30).
```

This confirms the complete Brain → capability check → argument validation →
tool execution → final response path. The API key was not exposed or
recorded.
