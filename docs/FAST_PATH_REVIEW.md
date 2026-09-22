# Fast-Path Router Review

## Scope

This is a design review only. No router or production behavior was added.
The question is whether Stella should handle a small class of deterministic
requests locally instead of sending them through `LLMBrain`.

## Current normal routing

The current CLI sends every non-exit input through the same path:

```text
user input
  -> Context
  -> Memory.retrieve()
  -> LLMBrain
  -> structured Decision
  -> trusted Stella/ToolDispatcher path
  -> ToolResult, if applicable
  -> final response
```

For an ordinary `LLMBrain` answer, the current single-call path generally
returns the model's structured answer directly. For a tool request, Stella
still performs trusted lookup, validation, risk/approval checks, execution,
and then a final LLM synthesis call. Therefore, merely routing a phrase to a
tool would not remove the provider call unless the fast path also supplied a
trusted local response.

## Candidate requests

### Current date and time

This is the strongest candidate. `DateTimeTool` is local, read-only,
deterministic, and has a small fixed argument schema. A narrowly recognized
request could select one of `date`, `time`, `datetime`, or `weekday`, execute
the existing tool through trusted application code, and format the result
locally.

The timezone limitation remains important: the tool reports the host's local
timezone. A fast path must not infer a user timezone or claim that the result
is the user's location.

### Hostname and platform

`SystemInfoTool` is also a reasonable candidate. Its supported operations are
fixed and read-only, and it does not expose environment variables or secrets.
However, natural requests for system information vary more than exact time/date
requests. A small allowlist of clear phrases could work, but broader matching
would quickly become a brittle intent parser.

### Echo

Echo is deterministic and safe, but extracting the message from natural
language requires deciding what text is intended to be echoed. Exact syntax
such as an explicit `echo:` prefix could be handled locally, but that is a
command convention rather than general conversational understanding. The
latency benefit does not justify introducing such a convention into the normal
CLI without a product need.

### Filesystem capabilities

`filesystem_read` should not be part of the first fast path. Even though it is
workspace-scoped and read-only, path extraction, user intent, sensitive
workspace content, and final response wording need more care. `filesystem_write`
is dangerous and must continue through the Brain proposal, trusted validation,
approval, audit, and execution path. A fast path must never bypass those
boundaries.

## Latency benefit

The existing live latency measurements show ordinary model calls taking roughly
one to two seconds, while local tool execution is negligible. A local answer
for an exact date/time request could therefore remove almost the entire model
round trip. In a local benchmark, 1,000 direct executions averaged:

- `DateTimeTool`: approximately 0.0103 ms per execution;
- `SystemInfoTool`: approximately 0.0011 ms per execution.

These values exclude input handling and memory lookup, but show that the tool
itself is not the bottleneck. A true fast path could feel substantially faster
than normal LLM routing. Routing only to the existing tool while retaining
final LLM synthesis would not provide that benefit.

## Reliability and false positives

A router has a useful reliability property for exact supported requests: it
can produce a known capability and validated fixed arguments without model
parsing. Its main risk is false positives. For example, a question mentioning
“time” may ask about duration, historical time, or a time stored in memory,
not the current clock. A question mentioning “hostname” may ask for a concept
or quote rather than the local machine.

The more natural-language variants the router accepts, the more rule-based
intent logic it needs and the more likely it is to answer the wrong question
without asking the LLM. That would be worse than a slower but correct answer.

The router should therefore use conservative exact or near-exact patterns,
return no match for ambiguity, and leave unmatched input to `LLMBrain`. It
should not attempt broad keyword scoring, entity extraction, or a second
hidden decision system.

## Security implications

A local router would be trusted application code, not model output. It must
still use the existing tool object and validation boundary. It must never:

- create or register a tool;
- accept a capability or risk level from user text as authorization;
- bypass `ToolDispatcher` for any capability that can access user data or
  change the system;
- turn arbitrary natural-language text into filesystem paths or write
  arguments; or
- weaken approval, audit, or step-limit behavior.

The safest initial scope is limited to the existing `SAFE` capabilities with
fixed arguments. `filesystem_read` is `SENSITIVE`, and `filesystem_write` is
`DANGEROUS`; neither should be included in a generic first router.

## Recommended design

Do not add a general fast-path router yet. If the latency benefit becomes a
product requirement, add a small explicit local handler for current date/time
requests first:

1. recognize only a short, documented set of unambiguous phrases;
2. return no match for anything uncertain;
3. construct the fixed `datetime` operation in trusted code;
4. run the existing `DateTimeTool` validation and execution;
5. format its `ToolResult` locally with a simple factual response; and
6. leave memory retrieval/writing and all other requests on the normal Stella
   path unless their semantics are explicitly designed.

This should be a narrow request classifier, not a general command parser or
replacement for `LLMBrain`. A later system-info fast path could be considered
separately after observing real false-positive behavior.

## What should remain unchanged

- `LLMBrain` remains responsible for ambiguous natural-language decisions.
- `ToolDispatcher` remains the trusted capability lookup boundary.
- `Tool.validate_arguments()` remains mandatory.
- Risk, approval, audit, memory, and multi-step rules are not bypassed.
- No fast path should handle filesystem writes, shell commands, network access,
  process launching, or other side-effecting actions.

## Validation

- Source and tests reviewed without production changes.
- Local tool benchmark recorded above.
- Pytest: to be run after this document is added.
- Ruff: to be run after this document is added.
