# Context and Memory Review

## Scope

This review covers the current MVP implementation in `src/stella/` and its
tests. It does not change production code, add embeddings, or redesign memory.
The review follows one request through Brain decisions, memory access, tool
execution, final response generation, and the next CLI turn.

## Executive conclusion

Stella has enough context for the current narrow MVP: one user request, a
bounded set of tool steps, explicit memory writes, lexical memory lookup, and a
user-facing answer. The important information normally reaches the Brain and
the final response LLM, and the existing tests prove the main handoffs.

It is not yet a robust general conversational context system. The smallest
useful improvements are:

1. Bound and shape context before sending it to the Brain or response LLM.
2. Preserve a compact structured record of tool steps in the next-turn
   conversation state, or explicitly accept that tool provenance ends with the
   current turn.
3. Make lexical memory retrieval return a bounded, ranked set and expose its
   limitations clearly. This is a better MVP safeguard than adding embeddings.
4. Decide and test what happens when multiple Brain steps propose memory
   writes; today only the first proposal is stored.

The first item is now implemented as a small deterministic context boundary.
It limits growth and prevents older history, many observations, or large tool
outputs from crowding out the current request. Embeddings and automatic memory
capture remain out of scope.

## What reaches the Brain

`Stella.process()` retrieves memory using only `context.user_input`, then
constructs a fresh `Context` for every Brain decision. The Brain receives:

- the current `user_input`;
- the complete `conversation_history` supplied by the caller;
- matching `retrieved_memories` as plain content strings; and
- `tool_observations` accumulated in the current process, including capability,
  arguments, success, and output.

`LLMBrain` also receives a system prompt containing the available tool
descriptions and safety/decision rules. It does not receive the memory query,
memory IDs, match scores, timestamps, provenance, or a write confirmation.
It also does not receive the `StellaResult`, prior decisions, or the internal
`step_trace` as a separate field.

For final response generation, the LLM receives the current input, complete
history, retrieved memory contents, and the selected decision. After a
multi-step sequence it also receives all structured observations. In the
single-tool path it receives the direct `tool_result`; this is sufficient for
the current one-tool MVP.

## Context that is unnecessary or duplicated

- The current input is present as the top-level field while the history can
  also contain earlier copies of the same user turn, depending on the caller.
- A tool observation contains the same result information represented by the
  current `tool_result` in the single-step final-response payload. This is
  harmless but redundant.
- The Brain gets all matching memory contents without a relevance score or
  distinction between a strong match and a weak lexical match.
- Full conversation history is sent on every turn, even when older turns no
  longer affect the current decision.

These are not correctness failures at current test sizes, but they directly
increase token use and make important current information less prominent.

## Missing context

The Brain cannot distinguish why a memory was selected, how recent it is, or
which memories conflict. It has no explicit context budget or truncation
policy. It also has no structured indication that a proposed memory write was
successfully stored.

Tool observations identify the capability and arguments, but not a stable step
number, duration, or timestamp. Those fields are not required for the current
MVP, but step numbering would make multi-step reasoning easier to follow and
debug without copying the entire trace.

There is also no persistent conversation history. The CLI owns an in-memory
history list, so a new process starts with no prior transcript. That is
separate from SQLite memory by design: remembered facts persist, ordinary
conversation does not.

### Implemented context limits

The context boundary uses fixed limits in `src/stella/context.py`:

- 20 conversation messages: retain the newest messages in their original
  order. This represents roughly ten normal user/assistant turns and preserves
  the current conversational focus without attempting semantic relevance.
- 8 tool observations: always retain the newest observation, prefer the newest
  failed observations, then fill remaining slots from newest to oldest. The
  selected observations are restored to execution order, so multi-step context
  remains readable and failures are not discarded merely because they are
  older.
- 4,000 characters per tool output: retain a deterministic prefix and append
  an explicit truncation marker. The same limit applies to the direct
  single-tool final-response payload.

These limits apply only when the corresponding input exceeds the limit. Below
the limits, existing ordering and payload content are preserved. They bound
context sent to the Brain and response LLM; they do not alter `ToolResult`,
tool execution, dispatcher validation, audit records, or stored memory.

## Memory retrieval and writes

Retrieval is deterministic keyword overlap. Stop words are removed and a
multi-term query needs two shared terms (or the one available term). This fixed
the original full-query substring failure and is enough for simple questions
such as a stored favorite-color fact.

Current limitations are material but bounded:

- synonyms, inflections, spelling variations, and paraphrases do not match;
- all matching rows are returned in insertion order, with no ranking or limit;
- lexical collisions can return unrelated memories;
- a long-term database can therefore produce a large prompt and competing
  facts; and
- a query with only common/stop words retrieves nothing.

Writes are explicit: only a non-null `Decision.memory_write` is stored. Normal
conversation, retrieved memories, tool results, and final responses are not
automatically written. This protects the MVP from uncontrolled memory growth.

One subtle multi-step behavior should be made intentional: `Stella` stores the
first non-null memory-write proposal and ignores later proposals in the same
`process()` call. This prevents duplicate writes but can silently lose a valid
second fact. The current `StellaResult` reports only the first write. For the
MVP, either document “at most one memory write per request” and test it, or
reject/handle a later proposal explicitly. Do not add automatic writes as a
shortcut.

## Tool observations and multi-step context flow

With the default `max_tool_steps=1`, the path is:

```text
Context -> retrieve memory -> Brain decision -> tool -> ToolResult
        -> final-response LLM
```

The final-response LLM sees the tool result and the original context, so the
tool result can affect the displayed response. With a larger step bound, each
successful or failed tool result becomes a structured observation for the next
Brain decision. The observations are retained for the rest of that request,
and the final answer payload includes them all. The step bound prevents an
unbounded loop.

Important information can still be lost across turns. The CLI appends only the
user input and displayed response to its history. It does not append the
structured decision, tool arguments, tool success/failure, or raw tool output.
Consequently, a later turn can see a natural-language summary but cannot
reliably inspect the prior tool fact as structured state. A new process loses
even that transcript, while SQLite memory retains only explicitly requested
facts.

Within one multi-step call, observations are now bounded to eight and each
observation output is bounded to 4,000 characters. The newest observation and
newest failures receive deterministic priority. This prevents a long bounded
interaction or a large tool response from displacing all current context,
while retaining the original full `ToolResult` in the returned result object.

## MVP assessment

The current context is sufficient for the original MVP if the MVP means:

- one bounded synchronous request flow;
- one local user/session;
- explicit fact memory rather than transcript memory;
- lexical retrieval for simple wording; and
- bounded tool execution whose result is used immediately.

It is insufficient for reliable long-running conversation, large memory
collections, synonym-heavy recall, or cross-turn tool-aware planning. Those are
later capabilities, not reasons to add embeddings now.

## Remaining recommended sequence

1. Add a small retrieval result limit and deterministic ranking/tie-break rule
   while retaining keyword overlap. Add collision and large-memory tests.
2. Make the one-memory-write-per-process rule explicit in the contract and
   test the multi-step case, or return a clear structured outcome for ignored
   proposals.
3. If cross-turn tool-aware behavior becomes a requirement, append a compact,
   structured tool event to conversation history. Do not persist raw tool
   output as memory automatically.

The implemented boundary is the smallest high-leverage change because it
protects every current path—Brain decisions, final answers, memory use, and
multi-step tools—without changing the memory model or introducing a new
retrieval technology.

## Verification

The focused implementation tests cover Brain payload assembly, memory handoff,
explicit writes, SQLite persistence across instances, tool-result response
generation, failed tools, multi-step observations, recent-history selection,
failure-aware observation selection, and output truncation. The full pytest
and Ruff checks were run after this update; their results are reported with
this change.
