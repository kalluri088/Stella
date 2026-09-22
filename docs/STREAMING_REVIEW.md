# Streaming Review

## Scope

This is an investigation only. No production streaming behavior was added.
The review covers `LLMClient`, `OpenAILLMClient`, `LLMBrain`, `Stella`, tool
dispatch, memory writes, and the bounded multi-step loop.

## Current implementation

`LLMClient.chat()` accepts messages and returns one complete `str`. The
interface has no iterator, callback, event, or asynchronous streaming method.
`OpenAILLMClient.chat()` calls the OpenAI Chat Completions API without
`stream=True` and waits for the complete response before returning it.

The current normal `LLMBrain` answer path is one buffered model call. The model
returns structured JSON containing the selected decision and final answer
content. Stella parses the complete JSON before it can return the answer.
Tool decisions are also structured JSON. Stella must parse the complete
decision before trusted capability lookup, argument validation, risk checks,
approval, and execution can happen.

## Temporary live measurements

The configured `gpt-5.6` model was queried through the underlying OpenAI SDK
with `stream=True`. This was a temporary measurement harness; it did not
change `OpenAILLMClient` or the provider-agnostic interface.

TTFT means elapsed time from starting the streaming request until the first
non-empty text delta. Total means elapsed time until the stream ended.

| Prompt | TTFT | Stream total | Text length |
|---|---:|---:|---:|
| Bounded loops explanation | 1.664 s | 2.111 s | 193 |
| Capital and landmark | 1.658 s | 1.896 s | 78 |
| Memory retrieval versus writing | 1.425 s | 2.001 s | 206 |

Measured means for this small sample:

- mean TTFT: approximately 1.582 s;
- mean streamed total: approximately 2.003 s.

Matching buffered requests for the same prompts took approximately 2.123 s,
1.308 s, and 2.861 s. These are separate requests, so model scheduling and
network variation make the comparison indicative rather than a benchmark.
The streamed text was complete and usable in all three samples.

## Architectural impact

### Structured Brain decisions

The Brain cannot safely expose partial structured output as a decision. JSON
may be incomplete, malformed, or not yet contain the fields required for
`DecisionKind`, capability, arguments, and `memory_write`. A streaming client
could buffer the decision stream and parse only after completion, but that
would preserve correctness without improving visible TTFT for the decision.

### Tool calls

Stella must not execute a tool from a partial model response. Capability
lookup, strict argument validation, risk classification, approval, and audit
must all happen after a complete parsed decision. Streaming a decision can
therefore reduce transport buffering only; it cannot move tool execution
earlier or let the LLM bypass the trusted dispatcher.

### Multi-step execution

Each step depends on the previous complete `ToolResult`. The runtime must
finish one decision, execute the tool through the existing path, feed the
structured observation back to the Brain, and then begin the next request.
Streaming does not make these steps parallel or remove their sequential
dependency.

### Memory writes

Memory writes are performed only after a complete parsed decision contains an
explicit `MemoryWriteRequest`. Partial JSON must never trigger a write.
Streaming would require buffering and final validation before the existing
write boundary could run.

### Final responses

Final text after a tool result is conceptually the easiest place to stream:
the tool has already executed and the runtime has a complete `ToolResult`. The
current single-call ordinary answer path is less direct because its final text
is carried inside the structured decision JSON. Displaying its partial content
would require an incremental JSON parser and careful handling of escaped text,
malformed output, and completion errors.

## What would need to change

The smallest safe future design would add a separate optional streaming
capability rather than changing `chat()`'s return type. Possible shapes are a
provider-agnostic iterator of text chunks or a dedicated final-response
method. The implementation would need:

1. an OpenAI adapter that converts provider stream events into stable text
   chunks;
2. buffered structured-decision handling, with no tool or memory action before
   complete parsing and trusted validation;
3. a Stella/CLI path that displays only approved final-response text chunks;
4. deterministic handling of stream errors, empty streams, cancellation, and
   incomplete JSON; and
5. tests proving that streaming does not alter decisions, tool authorization,
   approval, audit records, memory writes, or step limits.

The provider-agnostic interface should not expose OpenAI-specific event types.
The existing `chat()` method should remain available for decision calls and
buffered compatibility unless a deliberate interface change is justified.

## Recommendation

Do not add streaming yet. The current measurement shows that TTFT is around
1.6 seconds for ordinary text, but the main product and security boundaries
are still structured decision parsing and trusted tool execution. Streaming
would provide the clearest benefit for final natural-language response text,
especially after a tool result, but it would add an interface and CLI path
before the MVP has established requirements for cancellation, partial output,
and error recovery.

Revisit streaming after the final-response boundary is separated clearly from
structured Brain decisions, or when interactive response latency becomes a
confirmed product requirement. At that point, stream only final response text;
keep Brain decisions, memory writes, capability validation, approvals, tool
execution, and multi-step transitions buffered and trusted-runtime controlled.

## Validation

- Temporary live stream probe with `gpt-5.6`: completed for three ordinary
  prompts; TTFT and total timings recorded above.
- Pytest: 175 passed.
- Ruff: all checks passed.
