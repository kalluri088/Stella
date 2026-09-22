# Stella Vision

This is Stella's north-star document. It describes the identity and direction
of the project, then distinguishes that direction from the deliberately small
part that exists today.

## What Stella is

Stella is a context-and-action system that uses memory and trusted tools to
decide what should happen next. Its purpose is not merely to produce fluent
text. Stella should understand the current situation, take a bounded action
when one is useful, observe what happened, and retain meaningful lessons for
later decisions.

The essential idea is a controlled loop:

```text
Context + Memory + Tools
            |
         Decision
            |
 Answer / Ask / Tool / Do nothing
            |
          Action
            |
         Outcome
            |
          Memory
```

The loop is intentionally explicit. Context informs the Brain; the Brain
proposes a decision; trusted runtime code performs any action; the outcome is
available for evaluation; and only a deliberate memory decision makes
something durable.

## What Stella is not

Stella is not:

- a normal chatbot whose primary job is to answer every prompt;
- a Jarvis clone whose identity depends on theatrics, voice, or unrestricted
  automation;
- an unbounded autonomous agent, planner, or multi-agent framework;
- a system that silently records every conversation, tool result, or model
  response;
- a model that directly executes code, shell commands, network requests, or
  other side effects;
- a generic tool/plugin marketplace;
- a replacement for explicit application security, authorization, approval,
  or audit boundaries; or
- a semantic-memory product by default.

Stella may eventually become more capable, but additional capability must
serve the core loop rather than replace it with a larger framework.

## The original MVP goal

The original MVP goal is to build the smallest version that proves Stella
feels fundamentally different from a normal chatbot/Jarvis clone.

The proof is behavioral: Stella should use context and remembered information
to decide whether to answer, ask for clarification, use a tool, or do
nothing. When it acts, the result should affect what it says and, when the
result contains a meaningful durable lesson, what it can know later.

The MVP is complete only when the project demonstrates the meaningful
`Outcome -> Memory` connection, not merely when it has several tools or a
longer prompt.

## Core interaction and decision loop

1. Receive the current request and the available conversation context.
2. Retrieve relevant remembered facts.
3. Give the Brain the current context, memory, and available trusted
   capabilities.
4. Let the Brain choose exactly one of four kinds of next step:
   `answer`, `ask`, `tool`, or `do_nothing`.
5. If the choice is a tool, let trusted runtime code validate, authorize, and
   execute it.
6. Treat the result as an untrusted observation that can inform the next
   bounded decision or the final response.
7. Evaluate whether the outcome contains a durable fact or lesson worth
   remembering.
8. Persist only an explicit, deliberate memory write.

The loop is synchronous and bounded in the MVP. A later decision may follow a
tool result, but Stella must not continue indefinitely or turn a final answer
into an implicit new action.

## Principles

### Explicit over implicit

Context, decisions, tool observations, memory writes, and outcomes should have
clear representations and boundaries. Stella should not infer persistence from
words such as “I will remember that,” and it should not silently turn ordinary
conversation into memory.

### Trusted runtime over model authority

The Brain proposes. Application code decides whether a capability exists,
validates arguments, applies risk and approval rules, executes the tool, and
records the outcome. Model output is never authorization.

### Memory is selective

Memory is for meaningful durable facts and lessons, not a transcript dump.
Retrieval and writing are separate decisions. A result should be stored because
it matters later, not merely because it was produced.

### Bounded behavior

Context, tool observations, and multi-step execution need deterministic
limits. A bounded system is easier to understand, test, secure, and trust than
an agent that can continue without a clear stop condition.

### Provider-agnostic foundations

The core concepts—context, Brain, memory, tool, dispatcher, decision, and
outcome—should remain understandable independently of a particular model
provider. Provider integrations belong at the boundary.

### Evidence before expansion

A capability is part of Stella's identity only when its behavior is proved by
focused tests and, where relevant, an end-to-end validation. More abstraction,
tools, or autonomy should not be added to compensate for an unproven core
loop.

### Local-first and safe by default

The current direction favors local persistence, narrow capabilities, explicit
approval for risky actions, untrusted tool-result handling, and no hidden
background work. Safety boundaries are part of Stella's behavior, not an
optional wrapper around it.

## What makes Stella different

A normal chatbot primarily maps a request to a response. A Jarvis-style clone
often emphasizes persona and a growing collection of actions. Stella's
distinctive behavior is the relationship between decision, action, outcome,
and selective memory:

- it can decide that answering is not the right next step;
- it can ask for the information required to act responsibly;
- it can recognize when context and memory are insufficient, and ask a useful
  clarification instead of guessing;
- it can apply one relevant, explicit behavioral preference consistently without
  turning that preference into a persona system;
- it can use a bounded, trusted capability rather than merely describe one;
- it can interpret the result before responding;
- it can preserve a meaningful lesson and use it in a later decision; and
- it can choose not to act when no useful or authorized action exists.

The difference is therefore not “more tools” or “more personality.” It is a
reliable behavioral loop in which remembered experience changes future action.

## Original vision versus current implementation

The original vision includes the complete loop, including selective learning
from meaningful outcomes. The current implementation is a deliberately small
foundation:

- Context preserves the compatible `user_input` field and also carries a
  bounded provider-neutral `InputEnvelope` of typed parts. Parts support text,
  future audio, image, video, and environment observations with provenance,
  bounded content or references, and metadata. Memory retrieval considers the
  current request together with bounded conversation history so follow-up
  decisions can use the situation and the remembered fact together.
- Memory supports explicit in-memory and SQLite storage and deterministic
  keyword-overlap retrieval.
- The Brain supports structured `answer`, `ask`, `tool`, and `do_nothing`
  decisions with safe fallback behavior.
- Trusted tools are dispatched through validation, risk, approval, and audit
  boundaries.
- Tool results can affect the immediate response and later decisions in a
  bounded multi-step interaction.
- The Brain is instructed to ask for required missing or ambiguous action
  details rather than inventing them; retrieved memory only fills a gap when
  it is relevant to the current request.
- A directly relevant explicit preference can guide response style or a
  decision consistently; the current request takes precedence, and the
  preference is not exposed as a personality layer.
- A bounded one-shot due-task event evaluation can increase awareness without
  increasing authority: trusted scoped delegation may permit an `INFORM`
  result, missing permission produces `ASK`, and irrelevant events produce
  `DO_NOTHING`. A trusted application handoff supplies stable event identity
  and suppresses duplicate IDs. A trusted caller may present the resulting
  `INFORM` or `ASK` message; Stella does not deliver notices or execute
  actions.
- A one-shot audio input can cross a provider-neutral transcription boundary
  into the same typed text and reasoning path; transcription is observation,
  not permission or a new action authority.
- A one-shot image input can cross a provider-neutral vision boundary into the
  same typed text and reasoning path; the original image is preserved and the
  derived description is marked with model provenance. Image understanding is
  observation, not permission or a new action authority.
- An existing final response can cross a provider-neutral speech boundary for
  one-shot audio rendering. Speech is presentation after the decision loop;
  it cannot alter decisions, permissions, memory, or tool authority.
- A finite, explicitly supplied video clip can cross a provider-neutral
  analysis boundary as bounded temporal observations. The clip reference is
  preserved and derived observations remain untrusted model provenance; this
  does not imply consent for continuous observation or any action authority.
- Interface adapters can normalize richer inputs into the same bounded context
  contract; modality and provenance describe observations but never grant tool,
  memory, delegation, or approval authority.
- The CLI is request-driven and synchronous; conversation history is not
  persistent.
- Explicit user-requested memory writes persist, and a successful non-empty
  tool outcome can produce one additional explicit memory write when the Brain
  proposes a durable fact or lesson.

This is the minimal outcome-to-memory bridge. It does not store raw tool
results, failed outcomes, or every successful result, and it is available for
an explicitly bounded multi-step interaction rather than an unbounded learning
loop.

## Long-term direction

Stella should grow into a dependable personal context-and-action system whose
behavior becomes more useful through selective experience. Its long-term
direction is not unlimited autonomy. It is better judgment within explicit
boundaries:

- richer context assembled from the right sources;
- memory that is relevant, scoped, inspectable, and useful over time;
- decisions that account for uncertainty, consequences, and user intent;
- trusted capabilities with clear authorization and observable outcomes;
- meaningful lessons that improve later decisions;
- personality expressed through consistent behavior rather than decoration;
- events and proactivity only when there is a clear, user-serving reason; and
- latency and interaction quality appropriate for real use.

Each future capability should be evaluated against the core question: does it
help Stella make a better bounded decision from context, memory, tools, and
outcomes?

## Current MVP boundaries

The current MVP intentionally includes:

- one local user/session assumption;
- explicit context assembly;
- deterministic lexical memory retrieval;
- explicit memory writes;
- a small approved tool set;
- trusted validation, risk, approval, and audit handling;
- synchronous execution;
- bounded multi-step tool interactions;
- selective memory writes from successful meaningful tool outcomes;
- one-shot due-task event evaluation with explicit, scoped delegation;
- trusted one-shot event handoff with process-local duplicate suppression;
- synchronous user-facing proactivity results without delivery infrastructure;
- a bounded provider-neutral input envelope with text compatibility;
- one-shot audio-to-text normalization through a provider-neutral boundary;
- one-shot image-to-text normalization through a provider-neutral boundary;
- one-shot speech rendering through a provider-neutral boundary; and
- one-shot finite video observation through a provider-neutral boundary; and
- answer, clarification, tool, and no-op outcomes.

It intentionally does not yet include:

- embeddings, vector databases, or semantic retrieval;
- automatic storage of all conversation or tool output;
- unbounded planning, retries, autonomous loops, or background execution;
- event ingress, notification delivery, scheduling, reminders, or background
  proactive work;
- audio capture, wake words, continuous listening, interruption, and
  provider-specific speech or vision integrations; image/video/environment
  processing beyond their boundaries, and interface rendering;
- continuous video or environment observation, camera or desktop capture, and
  raw-media retention;
- a personality or identity subsystem;
- multi-user memory ownership and migration;
- a plugin marketplace or broad tool ecosystem; or
- streaming, speculative execution, caching, and general latency redesign.

These are boundaries, not permanent prohibitions. They should be reconsidered
only when a concrete requirement shows that they advance Stella's identity and
the existing loop remains understandable and secure.

## Maintaining this document

This document is the source of truth for Stella's identity and original goal.
When an architectural decision, major capability, or roadmap change
materially affects what Stella is, what it is not, or how the core loop works,
update this document in the same change. Implementation-specific details that
do not affect those principles belong in the architecture and review
documents, not here.
