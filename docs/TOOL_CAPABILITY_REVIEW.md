# Stella Tool Capability and Dispatch Review

## Scope

This review examines the next security layer after strict per-tool argument
validation. It is based on:

- `docs/SECURITY_REVIEW.md`
- `docs/TOOL_EXECUTION.md`
- `docs/TOOL_EXECUTION_REVIEW.md`
- `src/stella/tools.py`
- `src/stella/brain.py`
- `src/stella/stella.py`
- `tests/test_tools.py`
- `tests/test_brain.py`
- `tests/test_stella.py`

The review's recommended capability boundary has now been implemented and
extended to the three current safe tools. No plugin registry, permissions
framework, or external tool was added.

## Observed facts

### 1. How tool selection works today

`Stella` receives an application-owned `ToolDispatcher` in its constructor.
For backward compatibility, a single `Tool` can still be supplied and is
wrapped in a one-item dispatcher. When the Brain returns `DecisionKind.TOOL`,
the dispatcher looks up the exact capability, validates arguments, and calls
that tool:

```text
Brain returns TOOL
  -> ToolDispatcher exact capability lookup
  -> selected Tool validates arguments
  -> selected Tool executes
  -> ToolResult
  -> final LLM response generation
```

`Decision` now has a structured `capability` identifier. `LLMBrain` parses the
identifier as untrusted data and its prompt describes the current
`system_info` capability. Stella compares it exactly with the injected tool's application-
approved `name` before validating arguments or executing the tool.

The normal CLI injects `DateTimeTool`, `SystemInfoTool`, and `EchoTool` into
one dispatcher. Tests can inject one or multiple approved tools. There is no
plugin loading or dynamic discovery.

### 2. Current trust assumptions

The injected `Tool` is treated as trusted application code. Its implementation
is selected by the composition layer, not by the LLM. The LLM controls only
the untrusted decision kind and argument data.

This is an application-owned finite allowlist: only tools explicitly passed to
the dispatcher can be invoked. Capability names are exact and duplicate names
are rejected deterministically.

The current design also assumes that the composition layer will not
accidentally inject a dangerous tool. There is no runtime declaration of the
intended risk, allowed operation, or authorization policy.

### 3. If the model requests an unintended tool

The protocol now carries a requested capability identifier. The dispatcher
routes it only to an exact registered name. Therefore:

- The model cannot cause lookup of an arbitrary Python object or unregistered
  tool.
- If the application accidentally injects a more powerful tool, every valid
  `TOOL` decision can reach that tool after argument validation.
- If the model claims a different tool in `content`, Stella does not interpret
  that claim as a capability identifier.
- Missing, unknown, or mismatched capability identifiers produce a
  deterministic capability-unavailable result and do not call the tool.

Argument validation protects the tool's input shape. It does not prove that
the application intended to expose that capability.

## Risks and design pressure

### Implicit capability exposure

The dispatcher makes the capability allowlist explicit, but the prompt remains
guidance rather than authorization. Stella's exact runtime lookup is still
the trusted access-control boundary.

As soon as a tool has external side effects, a generic `TOOL` decision is too
coarse. The application needs to distinguish “the model proposed an action”
from “this exact application-approved capability may run.”

### Model-selected capabilities

The LLM may propose a capability name, but it must not authorize that
capability. A model-generated name is untrusted input. Stella or another
trusted application policy must compare it against the capabilities explicitly
made available by the composition layer before dispatch.

### Multiple tools without premature infrastructure

The current design now supports the three concrete safe tools without a
general registry. Adding a plugin loader or dynamic discovery system would
still add policy and lifecycle complexity without a concrete requirement.

The current dispatcher is the smallest application-owned finite set of named
capability bindings. It does not provide plugin discovery, dynamic imports, or
autonomous planning.

## Implemented boundary

The current application-owned dispatch boundary is:

```text
Brain proposes Decision.capability
  -> Stella compares it with injected_tool.name
  -> mismatch: ToolResult(False, "Tool capability unavailable.")
  -> match: per-tool argument validation
  -> Tool.execute()
```

This guarantees that a `TOOL` decision cannot dispatch through this Stella
instance unless its capability identifier exactly matches the application-
approved injected tool. It also guarantees that capability failure does not
fall back to another tool or reach `execute()`.

It does not provide authorization based on user identity, user approval,
permissions, risk classification, sandboxing, resource limits, or protection
against prompt injection. The capability string remains model-controlled
input; only the exact comparison in trusted Stella code controls dispatch.

## Recommendation

### Smallest useful capability boundary

Keep the current single injected tool for the present MVP. The capability
boundary is now explicit:

1. Give the selected tool a stable capability name. `Tool.name` already
   provides the beginning of this identity.
2. Add a structured tool identifier to the decision protocol rather than
   overloading free-form `Decision.content`.
3. Have Stella compare the requested identifier with the application-approved
   injected tool before validation or execution.
4. Treat a mismatch as an unavailable capability and do not call the tool.

These four rules are now implemented for the approved tool collection.

With three tools, this remains an exact dictionary lookup, not a plugin
registry. The application still chooses every injected tool. The model may
propose a name, but Stella verifies it against the capabilities the
application exposed.

An alternative is to keep the identifier entirely outside the LLM protocol
and treat every `TOOL` decision as a request for the one injected capability.
That preserves the current API, but it leaves the intended capability implicit
and provides no clean path to distinguish an unavailable request later. The
explicit identifier is the smallest change that makes the boundary visible
once capability dispatch becomes a security concern.

### Should availability be explicit?

Yes, at the trusted application boundary. The set of available capabilities
should be selected by Stella's composition layer and should not be inferred
from user text, memory, prompt content, or the model's natural-language
explanation.

For the current MVP, the dispatcher is the effective available set. No plugin
registry is warranted yet. The important rule is that availability remains an
application fact, not merely a prompt claim.

### Should the LLM choose tools directly?

The LLM may propose an action and, eventually, a capability identifier. It
must not choose or authorize an executable object directly. Stella should
validate the proposal against the application-approved capability set before
calling any tool.

The sequence should remain:

```text
LLM proposes identifier and arguments
  -> Brain parses structured data
  -> Stella checks capability availability/policy
  -> Tool validates arguments
  -> Stella executes the approved tool
```

This preserves the existing boundary: Brain interprets, Stella orchestrates,
and the Tool executes only after trusted application checks.

### Unavailable or unauthorized tools

An unavailable or unauthorized capability should fail closed:

- do not call any tool;
- do not fall back to another tool;
- do not reinterpret the request as shell, Python, or a network action;
- return a deterministic failed `ToolResult` or structured capability-failure
  result;
- optionally let the existing final-response path explain that the requested
  capability was unavailable.

The failure should not reveal a list of hidden capabilities or internal object
details. It should be distinguishable from an argument-validation failure for
testing and observability, while remaining safe for normal user output.

Authorization belongs in trusted application code at or immediately above
Stella's dispatch boundary. It should not live in the LLM prompt, Brain
heuristics, or tool prose. A future authorization decision must use an
authenticated application identity and policy, not a user ID or approval
claim generated by the model.

## Future requirements

Before adding a second or system-affecting tool, define and test:

- stable capability identifiers and collision rules;
- an application-owned allowlist or finite capability set;
- explicit separation between availability and authorization;
- fail-closed behavior for unknown, disabled, or unauthorized identifiers;
- per-tool argument validation after capability validation;
- capability risk classification and user approval for dangerous actions;
- audit/inspection behavior that does not expose secrets or hidden capability
  details;
- identity and memory isolation before shared multi-user capability policy.

For multiple harmless tools, a small mapping such as
`{name: tool}` may be sufficient. A full registry is justified only when
there is a concrete need for discovery, lifecycle management, or extension by
independent components.

For filesystem, shell, network, or other external-action tools, capability
dispatch is only one layer. Strict schemas, authorization, explicit approval,
least privilege, sandboxing, resource limits, and secret/egress controls are
still required.

## Explicitly out of scope now

This review does not recommend adding any of the following to the current MVP:

- plugin loading or a general registry;
- permissions UI, authentication, or multi-user authorization;
- filesystem, shell, subprocess, network, or other external tools;
- sandboxing or operating-system isolation;
- autonomous loops, retries, background execution, or tool planning;
- plugin discovery, dynamic imports, or model-controlled code loading;
- user approval workflows for the harmless `SystemInfoTool`.

## Recommendation summary

The current single injected `SystemInfoTool` is an explicit one-item capability
allowlist. Stella requires the model-proposed capability to match the
application-approved tool name exactly, then applies per-tool argument
validation. This is a small dispatch boundary, not a registry or permissions
framework. Approval, sandboxing, and the next security layers remain out of
scope.
