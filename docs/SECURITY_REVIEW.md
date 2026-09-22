# Stella Security Architecture Review

## Scope and conclusion

This review examines the current implementation before Stella gains tools that
can affect the operating system or external services. It is based on:

- `docs/ARCHITECTURE.md`
- `docs/ARCHITECTURE_REVIEW.md`
- `docs/MEMORY_IDENTITY_REVIEW.md`
- `docs/TOOL_EXECUTION_REVIEW.md`
- `docs/TOOL_EXECUTION.md`
- every module under `src/stella`
- every test under `tests`

No production code was changed for this review.

The current MVP has a useful security property: the model can propose data,
but it cannot directly execute Python, shell commands, or unrestricted
filesystem/network operations. `Stella` and its application-owned
`ToolDispatcher` are
the trusted boundary that decides whether to invoke one of the explicitly
approved tools. That boundary must remain in place when more powerful tools
are added.

The current MVP does not need a security framework. It does need to preserve
strict parsing, explicit tool availability, deliberate memory writes, and the
absence of external-action tools. Permissions, user approval, sandboxing, and
identity become necessary before introducing tools with real side effects.

## Observed facts

### Current data flow

The implemented flow is:

```text
CLI input
  -> Context
  -> Memory.retrieve()
  -> Brain.decide()
  -> optional Memory.store()
  -> answer LLM call or injected Tool.execute()
  -> optional final LLM call
  -> CLI output
```

`LLMBrain` sends the current input, conversation history, and retrieved
memories to an `LLMClient`. The client returns an untrusted string. The Brain
parses that string as JSON and creates a `Decision` or the safe
`DO_NOTHING` fallback.

`Stella` receives the resulting `Decision`. It is the only current component
that calls `Memory.store()` or dispatches a tool through `ToolDispatcher`. The
Brain does not write memory or execute tools, and the LLM does not receive a
Python callback or an operating-system interface.

The current tools are `DateTimeTool`, `SystemInfoTool`, `EchoTool`, the
workspace-scoped `FileSystemReadTool`, and the dangerous create-only
`FileSystemWriteTool` and single-file `FileSystemDeleteTool`, plus the
approval-required, bounded public-HTTPS `NetworkReadTool`. The filesystem
tools have no shell or arbitrary filesystem scope, and the network tool has no
general proxy, credential, redirect, or private-address access. The normal CLI
injects all seven into the application-owned `ToolDispatcher`; the dispatcher
is a finite exact-name collection, not a plugin registry.

### Trusted application code versus model-controlled data

The following are application-controlled implementation boundaries:

- `Stella` orchestration and its branch logic.
- `Brain`, `LLMClient`, `Memory`, and `Tool` interfaces.
- The concrete OpenAI adapter, SQLite adapter, CLI composition, and injected
  tool objects.
- The parser's decision-kind check and malformed-output fallback.

The following must be treated as untrusted data, even when represented by
typed Python objects after parsing:

- the raw response from an LLM;
- `Decision.content` and `Decision.arguments` proposed by the LLM;
- a proposed `MemoryWriteRequest` and its text;
- user input and conversation history;
- retrieved memory content;
- `ToolResult.output` from any future tool;
- final natural-language response text returned by the LLM.

The types make data easier to carry through the system; they do not establish
trust, authorization, ownership, or safety.

### Current validation and failure behavior

`LLMBrain` requires a JSON object and one of the four known decision kinds. It
checks that `content` is a string when present, `arguments` is a dictionary
when present, and `memory_write.content` is a non-empty string when present.
Malformed or unusable output becomes `DecisionKind.DO_NOTHING` with no
arguments or memory write.

This is structural validation, not a complete tool-argument validator for
every possible tool. The `Tool` interface now requires a per-tool
`validate_arguments()` check, and `ToolDispatcher` runs it before
`Tool.execute()`. Each current tool also validates direct execution:
`EchoTool` requires exactly `{"message": <string>}`, `SystemInfoTool`
requires one supported `kind`, and `DateTimeTool` requires one supported
operation. Missing, extra, wrong-type, non-dictionary, or unsupported values
produce the deterministic failed result `Invalid tool arguments.`. Invalid
arguments therefore do not reach the tool body. An explicit
`ToolResult(success=False, ...)` is also passed to the final response path,
and unexpected exceptions remain converted to `Tool execution failed.`. No
retry or second tool execution is performed.

This guarantees only the declared shape for the current tools. It does not
provide permissions, authorization beyond exact action approval for dangerous
filesystem actions, sandboxing, resource limits, secret filtering,
prompt-injection protection, or safety validation for future side-effecting
tools.

The current implementation contains no `subprocess` or shell execution. Its
filesystem tools are limited to the configured workspace, and its network tool
is limited to approval-gated public HTTPS `text/plain` reads with address,
redirect, credential, header, and response bounds. The OpenAI client makes the
intended model API request, but application code does not treat model output as
executable code.

### Memory and output exposure

Memory is deliberately written only when a parsed decision contains an
explicit `MemoryWriteRequest`. Conversation messages, responses, and
retrieved memories are not automatically stored. SQLite persists only the
explicit memory text in a minimal table.

Retrieved memory content is inserted into the Brain's JSON prompt and, for an
answer or tool response, into the final-response prompt. The current memory
model has no provenance, owner, trust label, secret classification, expiry,
or access-control field. The identity review correctly records that the
current local database assumes one owner.

The CLI's optional `--debug` output prints the structured decision, including
tool arguments and proposed memory content. It is useful for diagnosis but
must be treated as potentially sensitive output. The normal CLI does not
print this inspection line.

## Security risks

### Prompt injection

User input, conversation history, and memory text can contain instructions
aimed at the model. Because these values are included in prompts, a malicious
user or malicious stored memory may persuade the model to choose an
unexpected action, propose a memory write, disclose information, or ignore the
intended structured protocol.

The current parser limits the resulting data shape, but it cannot determine
whether the model's decision was semantically manipulated. The final answer
prompt also includes retrieved memories and tool results, so those values may
attempt to influence response generation.

### Malicious or misleading tool output

Today `EchoTool` output is controlled and harmless. A future tool may return
content from a file, network response, command, or external system. That
content could contain prompt-injection instructions, false claims, secrets, or
destructive recommendations. Passing it to a final LLM call does not make it
trusted evidence.

Tool output also reaches the CLI and, in the current CLI, may be added to the
next turn's in-memory assistant history. Future implementations must not let
tool output silently become an instruction or an automatic memory write.

### Tool arguments and arbitrary actions

The current arguments dictionary is only partially validated. With a more
powerful tool, malformed types, unexpected keys, oversized values, dangerous
paths, URLs, commands, or option combinations could cause harm if the tool
trusts them.

The LLM must never be treated as an authorization decision-maker. A model
claim that an action is safe, necessary, or approved is still model output.

### Filesystem, shell, and network risks

Filesystem tools could expose private files, follow symlinks outside an
allowed directory, overwrite data, or be abused through path traversal.
Shell tools could become arbitrary command execution, environment-variable
exfiltration, or destructive activity. Network tools could contact internal
services, send credentials or private data, or perform external side effects.

These risks are not active in the current MVP because those tools do not
exist. They become security boundaries immediately when such a tool is
introduced.

### Secrets and privacy

The OpenAI API key is read from the environment by the provider adapter and is
not intentionally included in prompts or normal output. However, users can
place secrets in input or memory, and the current architecture sends relevant
input, history, and memory to the configured LLM provider. `--debug` can also
print model-proposed content and arguments.

The current SQLite database is local and unencrypted. Anyone who can read the
configured database file can read explicitly stored memories. Shared use is
not isolated; the identity review recommends postponing multi-user use until
ownership and authentication exist.

### Excessive permissions and future autonomy

There are currently no tool permissions, approval checks, background jobs, or
autonomous loops. That is a safety feature at this stage. Adding retries,
loops, scheduling, or automatic follow-up decisions would increase the impact
of a single prompt injection or model mistake and must not be assumed to be a
small extension of the current loop.

## Security boundaries required before powerful tools

### Validation

Validation should occur at several explicit layers:

1. `LLMBrain` should continue to validate the outer decision protocol and
   reject malformed output safely.
2. Each tool boundary should validate its own arguments against a strict,
   explicit schema before any side effect. Validation should check required
   fields, types, allowed keys, lengths, and resource limits.
3. Stella or a dedicated application policy component should validate whether
   the requested action is allowed in the current session and context.
4. The execution environment should enforce operating-system constraints even
   if application validation is bypassed or contains a bug.

Validation should reject unsafe input; it should not silently repair a command,
path, or permission request into a different action.

The first part of this recommendation is now implemented for the current
tool: per-tool validation runs before execution, while authorization and
operating-system controls remain future layers.

### Authorization and permissions

Authorization belongs in trusted application code at the orchestration or
composition boundary, before `Tool.execute()`. It must not live in the Brain,
the prompt, or a tool's interpretation of model prose.

The policy should identify the concrete tool and exact arguments or target,
apply least privilege, and default to deny for new or dangerous capabilities.
When identity is introduced, authorization must use the authenticated
application identity rather than a user ID supplied by the prompt. Memory
ownership and tool authorization should be related but not conflated.

### Sandboxing and operating-system separation

Sandboxing belongs around the code that performs the side effect, not inside
the LLM prompt and not only in a parser. Filesystem, shell, and network
capabilities should run with a separate low-privilege OS identity or process
and with narrowly scoped resources. Depending on the tool, that may require a
container or isolated process, restricted filesystem roots, disabled or
allowlisted network access, resource limits, and execution timeouts.

The application boundary should remain responsible for deciding whether to
start that sandboxed operation. Sandboxing is defense in depth, not a
replacement for validation or authorization.

### User approval for dangerous actions

Before a destructive, external, or privacy-sensitive action, Stella should
pause and request approval from the user. Approval should identify the exact
tool, arguments, target, and meaningful side effect. It should not approve a
general future instruction, and it should not be inferred from the model's
claim that the user already consented.

The approval decision must be made by the user through a trusted application
surface. The application should not execute first and ask afterward. Approval
should not be silently reused by an autonomous loop, and the eventual design
should define what is shown, how cancellation works, and how the decision is
audited without recording secrets unnecessarily.

### The LLM-to-operating-system boundary

The boundary is:

```text
untrusted model proposal
  -> strict parser
  -> trusted application policy and approval
  -> validated tool adapter
  -> sandboxed, least-privilege execution
  -> constrained ToolResult
```

The LLM must never receive direct OS capabilities, a shell, Python `eval` or
`exec`, arbitrary callbacks, unrestricted file handles, or credentials. A
tool is an application-owned capability, not a command channel. Every side
effect must pass through an explicit tool contract and trusted policy.

## What is necessary for the current MVP

The following are sufficient and necessary to preserve now:

- Keep the currently approved tools side-effect-free and explicitly composed
  through `ToolDispatcher`.
- Keep malformed structured output on the deterministic `DO_NOTHING` path.
- Treat model responses, user text, memory text, and tool results as data, not
  executable instructions.
- Keep JSON parsing only; do not add `eval`, `exec`, shell interpolation, or
  dynamic imports based on model output.
- Preserve explicit memory writes. Never infer a write from natural-language
  claims, and never write every message automatically.
- Keep `Stella` as the only place that invokes the injected tool and writes
  memory.
- Avoid putting API keys or other secrets into debug output, tests, or prompts
  unless the user intentionally supplied them for a clearly understood reason.
- Treat the local SQLite file as sensitive and single-owner. Do not use the
  current database model for shared or authenticated multi-user use.
- Keep autonomous loops, background execution, and automatic retries absent.

These are mostly restrictions and explicit boundaries, not a request for new
security middleware. The current test suite should continue to verify the
safe malformed-output and tool-failure paths as the MVP evolves.

## Future requirements before external-action tools

Before adding a real tool, the project should define and test, in this order:

1. A strict per-tool argument schema, including rejection of extra keys,
   invalid types, oversized inputs, and malformed nested data.
2. An explicit tool capability/allowlist and a trusted dispatch policy. The
   current dispatcher provides this for the approved safe tools; any
   side-effecting tool needs a policy specific to its risk.
3. Risk classification and a user-approval path for destructive, external,
   privacy-sensitive, or irreversible actions.
4. Least-privilege execution and sandboxing appropriate to the capability:
   scoped filesystem access, path canonicalization and traversal protection,
   shell avoidance or tightly constrained subprocesses, network policy,
   timeouts, and resource limits.
5. Secret handling and egress controls so credentials are not exposed to
   prompts, tools, logs, or unapproved network destinations.
6. A provenance and trust policy for tool output and memory content, with
   prompt boundaries that treat both as untrusted material rather than
   instructions.
7. Identity, authentication, and memory isolation before one SQLite database
   can serve multiple users.
8. Adversarial tests for prompt injection, malformed arguments, path
   traversal, destructive targets, secret leakage, network restrictions, and
   approval bypass.

The exact mechanisms should be selected for each concrete tool. A shell tool,
for example, needs different controls from a read-only weather API, but both
must pass through the same trusted application boundary.

## Explicitly out of scope now

This review does not implement or recommend adding these to the current MVP:

- filesystem write/delete beyond the current narrow workspace operations,
  shell, subprocess, or broader network/external-action tools;
- sandboxing, containers, seccomp, OS-level isolation, or a security runner;
- permissions, authorization middleware, authentication, or accounts;
- a plugin system, dynamic discovery, or multiple-tool planning;
- automatic approval, unattended execution, autonomous loops, scheduling,
  retries, or background jobs;
- encrypted memory, secret-management infrastructure, or database migration;
- semantic memory trust scoring, automatic memory filtering, or automatic
  memory writes;
- an agent framework or broad security abstraction layer.

## Recommended implementation order

The next security-related implementation should be risk classification and a
trusted approval design before any dangerous action. Strict per-tool
validation and trusted capability dispatch are already present for the current
safe tools. Add sandboxing and least privilege before enabling filesystem,
shell, or network effects. Add identity and memory isolation before shared
multi-user storage.

Until then, the most important rule is simple: the model proposes, trusted
application code validates and authorizes, and only a bounded tool may perform
an action.
