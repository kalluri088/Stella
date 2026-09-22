# Current Tool Execution Flow

Stella now has a minimal single-tool loop. The Brain still decides whether a
tool action is appropriate, while Stella remains responsible for execution and
the final response request.

## Implemented flow

For a `TOOL` decision, the runtime path is:

```text
Context
  -> Memory retrieval
  -> Brain decision with capability
  -> Stella capability check
  -> injected Tool.execute(arguments)
  -> ToolResult
  -> LLM final-response call
  -> StellaResult and CLI output
```

The structured decision includes a `capability` identifier. The current
application-approved capabilities are the exact names registered in the
injected `ToolDispatcher`: `datetime`, `system_info`, `echo`,
`filesystem_read`, `filesystem_write`, `filesystem_delete`, and `network_read`.
Stella looks
up the requested capability by exact name. Missing, unknown, or mismatched
capabilities return:

```text
ToolResult(success=False, output="Tool capability unavailable.")
```

The tool is not validated or executed on that path, and Stella does not fall
back to another tool. The LLM's description or natural-language claim about a
tool is not authorization.

After the capability matches, Stella asks the one `Tool` supplied to its constructor to validate the
structured `Decision.arguments` dictionary, or `{}` when arguments are
absent. Only valid arguments are passed to `Tool.execute()`. It then
makes one LLM call for final natural-language response generation. The final
call receives:

- the current user input;
- conversation history;
- retrieved memories;
- the selected `TOOL` decision and its arguments; and
- the `ToolResult` containing `success` and `output`.

The final-response call is not another decision. Its system instruction tells
the LLM not to choose an action, execute a tool, or return decision JSON.

The decision prompt exposes the currently available tools and their argument
schemas:

```json
`datetime`: `{"kind":"date|time|datetime|weekday"}`
`system_info`: `{"kind":"hostname|platform|cpu"}`
`echo`: `{"message":"string"}`
`filesystem_read`: `{"path":"relative UTF-8 text-file path"}`
`filesystem_write`: `{"path":"relative UTF-8 text-file path", "content":"UTF-8 string"}`
`filesystem_delete`: `{"path":"relative UTF-8 text-file path"}`

`network_read`: `{"url":"HTTPS URL without credentials, query, or fragment"}`
```

The model is instructed to use the listed capabilities only for their
described operations and not to invent arguments. Filesystem delete is limited
to one existing regular file, rejects wildcards, and requires trusted
approval. The dispatcher is a small application-owned collection, not a
plugin registry or dynamic discovery system.

Both successful and explicitly failed `ToolResult` values reach the final
response call. If the tool raises an unexpected `Exception`, Stella converts
it to the deterministic result:

```text
ToolResult(success=False, output="Tool execution failed.")
```

The exception is therefore visible to the final response path as a failed
operation without being silently discarded. Stella does not retry the tool.

`FileSystemDeleteTool` requires exactly one relative `path` argument. It
rejects absolute paths, parent traversal, wildcard characters, missing files,
directories, and symlinks. Its canonical resolved path must remain below the
configured workspace. It is `DANGEROUS`, so `ToolDispatcher` requires an
exact application approval matching the validated path before execution. It
does not recurse or expand globs. Failures return deterministic results such
as `File was not found.`, `File is not a regular file.`, or `File could not be
deleted.`.

`NetworkReadTool` requires exactly one HTTPS URL without userinfo, query
strings, fragments, or a non-default port. It performs a fixed GET with no
redirects, model-supplied headers, cookies, credentials, or proxy settings.
It rejects local and non-global resolved addresses, revalidates the connected
peer, accepts only UTF-8 `text/plain`, and bounds the response at 1 MiB with
fixed connection/read/total timeouts. It is `DANGEROUS` because it creates an
external connection, so exact trusted approval is required before execution.
Fetched text is untrusted data and is not automatically stored in memory.

`DateTimeTool` requires exactly one argument named `kind`, whose value must
be one of `date`, `time`, `datetime`, or `weekday`. Missing keys, extra keys,
unsupported values, non-dictionary arguments, and other malformed structures
produce:

```text
ToolResult(success=False, output="Invalid tool arguments.")
```

The invalid input is rejected before date/time inspection. `DateTimeTool` also
performs the same check when called directly, so direct callers do not rely on
Stella to enforce the contract. It uses only Python's standard date/time
library, does not read environment variables, and never invokes a shell or
arbitrary command. It uses the host process's local timezone; it does not
infer a user's timezone.

The capability check guarantees that this Stella instance can execute only its
injected tool under the exact capability name selected by the application. It
does not provide permissions, authentication, user approval, sandboxing, or
authorization for dangerous actions.

The final natural-language response is stored in `StellaResult.response`, and
the original `ToolResult` remains available in `StellaResult.tool_result`.
The CLI displays the final response when one is present.

## Preserved behavior

`ANSWER`, `ASK`, and `DO_NOTHING` retain their existing behavior. Tool
execution remains outside the Brain. The Brain proposes a decision; Stella
executes the injected tool and coordinates the response.

## Intentionally out of scope

The current implementation injects seven bounded tools through one
`ToolDispatcher`. It does not provide:

- a plugin registry or dynamic tool discovery;
- tool selection beyond exact lookup in the application-owned dispatcher;
- general schemas or validation rules for future tools beyond each tool's own
  validation contract;
- retries, permissions, timeouts, or background execution;
- autonomous tool loops or follow-up decisions;
- external APIs, shell commands, subprocesses, or other side effects; or
- automatic memory writes from tool inputs or results.

The tool loop is deliberately one execution followed by one final response
generation call.

## Previous real-world SystemInfoTool validation

The real OpenAI-backed CLI was run with `--debug`, a dedicated temporary
SQLite database, and the previously configured model `gpt-5.6`. The request
asked for the current hostname. The model produced the structured decision:

```json
{"kind":"tool","capability":"system_info","arguments":{"kind":"hostname"},"memory_write":null}
```

Stella authorized the exact `system_info` capability, `SystemInfoTool`
executed its read-only hostname query, and the final response reported the
returned hostname. This validates model recognition, argument generation,
capability dispatch, tool execution, and final-response use of the
`ToolResult`. The API key was not displayed or recorded.

## Current real-world DateTimeTool validation

The real OpenAI-backed CLI was run with `--debug`, a dedicated temporary
SQLite database, and the previously configured model `gpt-5.6`. The request
was: `What time is it?`. The model produced:

```json
{"kind":"tool","capability":"datetime","arguments":{"kind":"time"},"memory_write":null}
```

Stella authorized `datetime`, `DateTimeTool` returned the current host-local
time with its numeric UTC offset, and the observed final response was:

```text
It’s 5:51:20 PM (UTC+05:30).
```

The final response therefore used the `ToolResult`. The API key was not
displayed or recorded.

## Real-world multi-capability dispatcher validation

Using the real OpenAI-backed CLI with `--debug`, model `gpt-5.6`, and one
session containing all three safe tools:

```text
What time is it?
Decision: {"arguments": {"kind": "time"}, "capability": "datetime", ...}

What operating system and platform is this machine running?
Decision: {"arguments": {"kind": "platform"}, "capability": "system_info", ...}

Please use the echo capability to echo exactly this marker: DISPATCHER_ECHO_7K2.
Decision: {"arguments": {"message": "DISPATCHER_ECHO_7K2."}, "capability": "echo", ...}
```

All three tools executed successfully. The final responses used the matching
`ToolResult`: local time with its offset, platform information, and the exact
echo marker respectively. No API key was displayed or recorded.

## Real-world validation

Pre-validation checks:

- `uv run pytest`: 56 passed
- `uv run ruff check .`: all checks passed

The validation used the real OpenAI-backed CLI with `--debug`, the existing
`EchoTool`, and a dedicated temporary SQLite database. The API key was
inherited by the process but was not displayed or recorded. The model was
`gpt-5.6`.

Successful scenario:

```text
Use the EchoTool with its required message argument to echo exactly this marker: ECHO_TOOL_REAL_WORLD_7F3K9.
```

The debug decision was:

```text
{"arguments": {"message": "ECHO_TOOL_REAL_WORLD_7F3K9."}, "content": "EchoTool", "kind": "tool", "memory_write": null}
```

The observed final CLI response was:

```text
ECHO_TOOL_REAL_WORLD_7F3K9.
```

This confirms that the real model selected `TOOL`, Stella executed the
existing EchoTool, the tool returned the marker as its `ToolResult.output`,
and the final LLM response reflected that result. The tool result therefore
actually influenced the final response.

Failure-path observation:

```text
Please use the EchoTool to echo this exact marker: ECHO_TOOL_REAL_WORLD_7F3K9.
```

The real model selected `TOOL` but generated the arguments
`{"text": "ECHO_TOOL_REAL_WORLD_7F3K9."}`. EchoTool requires the `message`
argument, so Stella caught the resulting exception and the final response was
the deterministic failure text:

```text
Tool execution failed.
```

The earlier failure exposed a protocol limitation: the LLM decision prompt did
not provide EchoTool's argument schema, so an underspecified natural-language
request produced the wrong argument key. The prompt now exposes the required
`{"message":"string"}` shape. A new real-model validation is still required
to confirm that this clarification reliably produces the expected arguments.

The follow-up validation attempt in the current environment was blocked before
the CLI started because `STELLA_MODEL` was unset. `OPENAI_API_KEY` was present,
but no model was guessed and no API request was made. The successful real-model
observations above therefore predate this schema clarification; the revised
prompt still needs a new real OpenAI run.

## Latest real-world validation after schema clarification

The existing OpenAI authentication was available without displaying the key,
and the CLI was run with `STELLA_MODEL=gpt-5.6`. A fresh dedicated temporary
SQLite database was used. The request was intentionally phrased without
manually supplying the argument key:

```text
Please use the EchoTool to echo this exact marker: ECHO_TOOL_SCHEMA_CLARIFIED_9Q2LM.
```

The real `--debug` output showed:

```text
Decision: {"arguments": {"message": "ECHO_TOOL_SCHEMA_CLARIFIED_9Q2LM."}, "content": null, "kind": "tool", "memory_write": null}
```

EchoTool received the documented `message` argument and returned the marker.
The final CLI response was:

```text
ECHO_TOOL_SCHEMA_CLARIFIED_9Q2LM.
```

End-to-end result: **succeeded**. The real model selected `TOOL`, generated
the required `{"message": "string"}` shape, EchoTool returned the expected
marker, and the final LLM response reflected that ToolResult. No production
code was modified and the API key was not exposed.

Post-validation checks:

- `uv run pytest`: 57 passed
- `uv run ruff check .`: all checks passed
