# System Information Capability

## Why this tool

Stella's first useful capability was `SystemInfoTool`, exposed through the
`system_info` capability name. It was chosen because it can answer simple
questions about the local machine without changing files, running commands,
accessing secrets, or calling an external service.

The tool supports three explicit requests:

```json
{"kind": "hostname"}
{"kind": "platform"}
{"kind": "cpu"}
```

It uses Python's standard-library `platform` and `os` functions. CPU output
contains the reported processor description when available and the CPU count.
The tool does not read environment variables, inspect arbitrary files, invoke
a shell, or execute model-provided code.

The default CLI now exposes `SystemInfoTool` alongside `DateTimeTool` and
`EchoTool` through the small application-owned `ToolDispatcher`. The tool
remains one exact capability in that collection; there is still no plugin
registry or dynamic discovery.

## Runtime flow

The normal CLI injects `SystemInfoTool` into Stella's `ToolDispatcher`. Its
flow is:

```text
LLMBrain proposes capability and arguments
  -> Stella compares capability with SystemInfoTool.name
  -> SystemInfoTool validates exact arguments
  -> SystemInfoTool executes a read-only query
  -> ToolResult reaches final LLM response generation
```

The capability must be exactly `system_info`. Stella rejects missing or
mismatched capabilities before argument validation or execution. The tool
rejects missing, extra, unsupported, or incorrectly typed arguments with the
existing deterministic invalid-argument result. Runtime failures are contained
as `System information unavailable.`.

This preserves the boundary that the LLM proposes, Stella validates and
authorizes the single application-approved capability, and the tool performs
only its bounded operation.

## Security boundaries

The capability is read-only and deliberately narrow. Its output is still
treated as tool-provided data when sent to the final LLM; the tool does not
grant that model any operating-system capability. No approval or sandboxing is
needed for this harmless implementation, but the tool must not be expanded to
accept arbitrary commands, paths, environment-variable names, or file queries.

## Intentionally not implemented

This capability does not add:

- memory or conversation persistence for system information;
- filesystem, shell, subprocess, network, or external-service access;
- a plugin registry or dynamic capability discovery;
- permissions, authentication, user approval, or sandboxing;
- autonomous loops, retries, or multi-step planning;
- arbitrary system inspection beyond hostname, platform, and CPU information.

## Verification

Deterministic tests cover valid execution, strict argument rejection, runtime
failure handling, capability mismatch, Stella orchestration, and delivery of
the `ToolResult` to final response generation. The real OpenAI-backed CLI
validation is recorded here after it is run with the configured model and a
dedicated temporary memory database.

The live validation used the real OpenAI-backed CLI with `--debug`, the
previously configured model `gpt-5.6`, and a dedicated temporary SQLite
database. The request asked for the current hostname. The Brain produced:

```json
{"kind":"tool","capability":"system_info","arguments":{"kind":"hostname"},"memory_write":null}
```

Stella accepted the exact capability, the tool returned the local hostname,
and the final natural-language response reported that hostname. This confirms
that the real model recognized the request, generated the supported argument
shape, passed Stella's capability check, and used the `ToolResult` in the
final response. The API key was inherited by the process and was not exposed
or recorded.
