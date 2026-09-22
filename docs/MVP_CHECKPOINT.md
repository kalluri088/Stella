# Stella MVP Checkpoint

## Goal

The original MVP is the smallest proof of:

```text
Context + Memory + Tools -> Decision -> Action -> Outcome -> Memory
```

The distinguishing claim is that Stella uses context, remembered facts, and
trusted actions to decide what to do, then learns from meaningful outcomes.
This checkpoint compares the current implementation with that claim. No
production code was changed for this review.

## Overall assessment

Stella now proves the original MVP in a narrow, concrete scenario. It can
retrieve a stored fact, give it to a Brain, choose among four actions, execute
a bounded trusted tool, observe the result, use the observation in a later
decision, persist one explicit outcome memory, and use that memory in a fresh
process. SQLite persistence across separate processes has been validated with
real `gpt-5.6` calls.

The core loop now has a minimal outcome-to-memory path. A tool outcome can
affect the current response and subsequent same-request decisions, and a Brain
may propose one explicit memory write when a successful non-empty result
contains a durable fact or lesson. Trusted application code still decides
whether that proposal is eligible for persistence. This is constrained outcome
learning, not automatic storage of tool results or a learning model.

The proof remains intentionally narrow, but it now includes an independent
source. A real validation used `filesystem_read` to obtain a preference from a
workspace file that was not included in the request, then persisted and reused
that fact in a fresh process. The earlier EchoTool validation still proves the
basic handoff mechanics; the filesystem case proves that the outcome can add
information not supplied directly by the user.

| Area | Working now | Foundation only | Missing for the MVP |
| --- | --- | --- | --- |
| Context | Current input, bounded recent history, retrieved memories, structured observations | Lexical selection and fixed limits rather than semantic relevance | No context gap blocks the MVP proof |
| Memory | Explicit writes, SQLite persistence, lexical retrieval affecting decisions, gated outcome writes | Keyword overlap, no ranking/provenance/conflict handling, one write per interaction | Broader relevance and conflict policy |
| Brain/decisions | Structured `answer`, `ask`, `tool`, `do_nothing`; safe parsing fallback; post-success write proposal | LLM proposal protocol and deterministic test Brain; meaningfulness remains model-proposed | Richer importance/confidence policy is not required for the MVP |
| Tools/security | Trusted dispatcher, validation, risk, approval, audit, bounded tools | Small synchronous capability set | No security gap blocks the MVP proof |
| Multi-step | Bounded sequential decisions with observations and traces | Default is one step; extra steps add model calls | No more loop capability is needed before proving learning |
| Outcome -> memory | Successful non-empty tool outcomes can gate one explicit write; fresh-process retrieval changes later behavior for EchoTool and filesystem_read | Meaningfulness is still proposed by the Brain and guarded only by a minimal runtime gate | Broader reliability and richer conflict handling |
| Personality | Memory-aware answers, clarification, tool use, safe no-op behavior | No stable values, style, or learned behavior beyond stored text | A separate personality layer is not next |
| Events/proactivity | None; CLI is request-driven | Synchronous bounded processing only | Events, scheduling, and proactive work |
| Latency | Local orchestration is negligible; ordinary answers use one model call | Tool answers use two calls; multi-step latency is additive | No latency feature is required for the core proof |

## Area-by-area review

### Context

**Working now.** `Context` carries current input, conversation history,
retrieved memory items, and structured `ToolObservation` values. Stella
retrieves memory before asking the Brain to decide. Multi-step decisions see
earlier tool results. The current deterministic boundary keeps the newest 20
conversation messages, at most 8 observations, and at most 4,000 characters
per tool output; recent failures and the newest observation are prioritized.

**Foundation only.** History is an in-process CLI transcript. Memory and tool
selection are lexical/recency-based, without semantic relevance scores,
provenance, or conflict handling.

**Missing for the MVP.** Nothing essential in Context blocks the demonstrated
closed loop. Do not expand it into an event, identity, or general
summarization model.

### Memory

**Working now.** `Memory` has explicit `store` and `retrieve` operations.
In-memory storage supports deterministic tests and SQLite persists facts across
instances. Retrieved memory reaches the Brain and final response, so it can
change behavior. Ordinary conversation, tool output, and retrieved memories
are not silently stored.

**Foundation only.** Keyword overlap does not understand synonyms,
inflections, paraphrases, recency, or competing facts. Results are not ranked
or scoped beyond the current local single-user arrangement. Only the first
explicit write proposal is stored per interaction.

**Missing for the MVP.** The minimal path is now present. Broader relevance,
conflict, provenance, and semantic retrieval policies remain future work. The
independent-source proof uses one controlled UTF-8 workspace file.

### Brain and decision-making

**Working now.** `DecisionKind` represents the four required choices.
`LLMBrain` returns a structured decision, requires explicit structured memory
writes, and falls back to `do_nothing` for malformed protocol data. The Brain
does not execute tools or write memory. `SimpleBrain` tests orchestration
deterministically.

**Foundation only.** The Brain is primarily a single-turn proposal boundary.
It has no explicit confidence, utility, outcome-importance, or learning
concept. The multi-step loop asks for another decision after an observation;
the outcome-memory proposal is still ordinary structured Brain output rather
than a separate evaluator.

**Missing for the MVP.** Nothing essential remains in this narrow area. A
future Brain may evaluate importance more richly, but the current protocol can
already propose an existing `MemoryWriteRequest` after a successful outcome.

### Tools and security

**Working now.** Tool proposals pass through the application-owned dispatcher:
exact capability lookup, argument validation, trusted risk classification,
approval for dangerous actions, execution, and audit recording. Filesystem and
network boundaries are narrow and resource-limited. Tool output is untrusted
model input, not authorization. The model cannot set risk, grant approval, or
bypass the dispatcher.

**Foundation only.** The capability set and synchronous execution model are
intentionally small. There is no plugin lifecycle, cancellation, background
job, or broad integration layer.

**Missing for the MVP.** No additional security or tool architecture is needed
to prove the core idea. More tools add surface area before the current loop is
shown to learn.

### Multi-step behavior

**Working now.** `max_tool_steps` is an explicit bound. Each tool result,
including failures, becomes structured input for the next Brain decision.
`StellaResult.step_trace` exposes the sequence, and a proposal after the limit
stops deterministically.

**Foundation only.** The default is one tool step, and additional steps add
model round trips. There is no retry, rollback, parallel execution, or
background continuation.

**Missing for the MVP.** None of those larger agent behaviors is necessary.
The bounded loop is enough to show that an outcome informs a later decision
and preserves a selected lesson.

### Outcome -> memory

**Working now.** `ToolResult` reaches final response generation and, when the
bound allows it, the next Brain decision. Explicit `memory_write` proposals
are stored and reported as `MemoryWriteResult`. The runtime accepts a proposed
write from the outcome path
only when the trusted `ToolResult` is successful and has non-empty output. A
failure, empty result, or successful result without an explicit proposal does
not create memory. At most one write is persisted per interaction, preserving
the existing explicit write mechanism.

**Foundation only.** “Meaningful” is still represented by the Brain's explicit
proposal plus the minimal runtime success/non-empty gate. There is no separate
learning model, semantic evaluator, or automatic extraction of facts from raw
output. Results remain request-local in `StellaResult` and the step trace
unless explicitly written.

**Missing for the MVP.** Nothing essential remains for the constrained proof.
The current limitation is confidence in generality, not a missing mechanism:
the model proposes meaningfulness and the runtime enforces only success,
non-empty output, and one-write gates.

### Personality through behavior

**Working now.** Stella differs from an answer-only chatbot when it asks for
missing information, chooses a tool, declines to act, uses a remembered fact,
or safely stops. These distinctions come from behavior rather than a persona
prompt.

**Foundation only.** There is no durable model of priorities, tone, habits, or
learned preferences beyond stored memory text.

**Missing for the MVP.** A separate personality system is not required.
Consistent behavior from context, selective memory, and outcomes matters more
than adding a persona layer now.

### Events and proactivity

**Working now.** Nothing proactive is implemented. Each CLI interaction is
initiated by the user and runs synchronously.

**Foundation only.** The bounded coordinator could later be invoked by another
application boundary, but there is no event abstraction or scheduler.

**Missing for the MVP.** Events, reminders, scheduling, background execution,
and proactive suggestions are outside the smallest proof.

### Latency

**Working now.** Local orchestration, memory lookup, validation, approval
checks, and local tools are negligible compared with model calls. Ordinary
`LLMBrain` answers normally use one provider call; `ASK` uses one decision
call. The bounded design avoids unbounded latency.

**Foundation only.** Tool-result responses require a decision plus final
synthesis, and multi-step workflows add a decision call per step. Earlier
representative live measurements were approximately 1.2 seconds for ASK,
2.6 seconds for a single tool, 4.1 seconds for an ordinary answer before the
single-call answer path, and 7.0 seconds for a two-step tool workflow. These
are indicative provider measurements, not service guarantees.

**Missing for the MVP.** A latency budget and broader optimization plan are
not required to establish the core behavior. Streaming, speculative calls,
caching, parallel tools, and fast-path routing should wait.

## MVP status: demonstrated, but deliberately narrow

**Selective outcome learning: turning a meaningful action outcome into an
explicit, durable memory write.**

Context, retrieval, decisions, tools, outcomes, and bounded multi-step
feedback now connect to a gated explicit write. The real two-process proof
demonstrates both “the outcome affected this response” and “the outcome
changed what Stella can know later.” The EchoTool case proves the handoff
mechanics, and the filesystem-read case proves that the outcome can add
information not supplied directly by the user. Broader outcome-learning
reliability remains unclaimed beyond these focused cases.

## Independent-source milestone: completed

The same existing flow was validated with a controlled workspace note and the
existing `filesystem_read` capability. The file contained a durable preference
that was not included in the user request. The validation verified that:

1. the trusted tool returned the independent fact;
2. the next Brain decision proposed one explicit memory write;
3. the runtime persisted it and the focused tests continued to reject
   failures/irrelevant outcomes; and
4. a fresh Stella process retrieved it and changed the later response.

This was a validation milestone, not a new architecture or feature expansion.
It establishes that the current outcome proposal is useful beyond an echoed
user fact.

## Highest-value next milestone

Stabilize the current MVP with a small repeatable acceptance matrix covering
one independent successful read, one failed read, one irrelevant successful
read, fresh-process retrieval, and response impact. Do not add capabilities or
redesign memory until this behavior reveals a concrete defect.

## What to stop building for now

Pause work on embeddings/vector search, automatic capture of conversation or
tool output, personality/identity systems, events and background proactivity,
unbounded agents/retries/parallel tools, larger plugin registries, broader
external integrations, streaming/speculative execution/caching, and multi-user
memory migration design.

These may become valid later requirements, but they do not increase confidence
in the current MVP. Keep the implementation stable while repeating the
acceptance matrix; do not expand the feature surface just to make the
checkpoint sound more complete.

## Verification

The current implementation and tests cover context assembly and limits,
lexical memory persistence and retrieval, structured decisions, trusted tool
execution and security boundaries, tool observations, bounded multi-step
execution, gated successful-outcome memory writes, fresh-process retrieval,
and answer-flow latency behavior. Real `gpt-5.6` validation has proven both
the two-process EchoTool case and the independent filesystem-read case. The
full pytest and Ruff checks were run for this checkpoint.
