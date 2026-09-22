# Next Milestone Review

## Scope

This review compares the current project with [STELLA_VISION.md](STELLA_VISION.md)
after the original MVP and the independent `filesystem_read` outcome-to-memory
validation. No production code was changed for this review.

## What Stella proves today

Stella now proves the original loop in a constrained end-to-end scenario:

```text
context + memory + trusted tool
          -> Brain decision
          -> filesystem read
          -> independent outcome
          -> explicit memory write
          -> fresh-process retrieval
          -> later response using the memory
```

The current project demonstrates that:

- context reaches the Brain with bounded history and structured tool
  observations;
- memory can be explicitly written to SQLite and retrieved in a later
  process;
- the Brain can choose `answer`, `ask`, `tool`, or `do_nothing`;
- tools execute through trusted lookup, validation, risk, approval, and audit
  boundaries;
- a bounded multi-step interaction can interpret a tool result;
- a successful independent file result can become one explicit durable memory;
- failed and irrelevant outcomes are not automatically stored; and
- retrieved memory can change a later response without rereading the source.

This is meaningfully more than an answer-only chatbot. The MVP claim is
proved narrowly and honestly. It is not yet evidence that every tool outcome
will be interpreted correctly or that memory will consistently change future
actions.

## What still feels missing

The current proof demonstrates that memory changes what Stella says. It does
not yet demonstrate strongly that memory changes what Stella decides to do.
The Brain sees retrieved text, but there is no focused acceptance case in
which an old fact changes an otherwise ambiguous action selection, tool choice,
or tool arguments.

That gap matters because the vision defines Stella as a context-and-action
system. A remembered fact that only decorates an answer is useful, but it is
still close to ordinary retrieval-augmented chat. Stella will feel distinct
when remembered experience changes its behavior at the decision boundary.

Other limitations are real but not the next milestone:

- retrieval remains deterministic keyword overlap rather than semantic
  relevance;
- meaningfulness is proposed by the Brain and guarded by a minimal trusted
  success/non-empty/one-write policy;
- personality is expressed only through emerging behavior, not a persona
  subsystem; and
- the system is request-driven, with no events or proactive execution.

These are boundaries and future questions, not reasons to add more surface
area now.

## Single recommended next milestone

### Memory-driven decision continuity

Prove one fresh-process scenario in which a remembered fact changes a later
decision or trusted action, not merely the wording of the answer.

Use only existing capabilities and the current memory/decision boundaries. A
valid scenario should:

1. establish a durable preference or fact through the already validated
   independent outcome-to-memory flow;
2. start a fresh process with the same SQLite memory;
3. issue an underspecified request that does not repeat the fact;
4. show that the Brain retrieves the fact and selects a different action,
   asks a different clarification, or supplies different tool arguments
   because of it;
5. execute any selected tool through the existing trusted dispatcher; and
6. verify the resulting behavior against a no-memory control or an equivalent
   request where the fact is unavailable.

The milestone should use one scenario, one bounded interaction, and existing
tools. It should not require new capabilities, a new memory backend,
embeddings, or a personality prompt.

## Why this matters

This is the smallest next proof that connects all of Stella's intended
behavioral qualities:

- context supplies the current ambiguity;
- memory supplies experience from an earlier outcome;
- the Brain makes a decision rather than merely composing prose;
- a trusted tool performs useful action;
- the outcome remains observable; and
- the remembered experience produces consistent behavior later.

It is also the first concrete form of personality through behavior. Stella
would not need to announce a persona or repeat a preference in every answer;
its choices would reveal that it knows the user and acts accordingly. The
same boundary is the right foundation for eventual proactivity, because
proactivity is only valuable when it is guided by relevant context and
meaningful experience. Scheduling or background work is not needed to prove
that foundation.

## How success should be judged

The milestone is complete only if the evidence shows all of the following:

- the memory row exists before the later interaction;
- the later Brain context contains the retrieved memory;
- the later structured decision differs in a meaningful way because of that
  memory;
- any action still passes through the trusted dispatcher and existing safety
  policy;
- the response reflects the action or decision; and
- a no-memory comparison does not produce the same behavior by coincidence.

The test should inspect structured decisions and tool arguments, not infer
success from a fluent response that merely claims to remember something.

## Explicitly defer

Defer the following until the decision-continuity milestone exposes a concrete
need:

- embeddings, vector search, semantic ranking, or a redesigned Memory API;
- automatic capture of conversations, tool output, or every successful event;
- more tools, plugins, or external integrations;
- a personality, identity, or preference-management subsystem;
- events, scheduling, reminders, and background proactivity;
- unbounded planning, retries, parallel tools, or multi-agent execution;
- streaming, caching, speculative calls, and general latency work; and
- multi-user memory ownership or migration.

The current project already has enough capability to test the next meaningful
behavioral distinction. Feature expansion before that proof would make Stella
larger without making its identity clearer.

## Implemented validation scenario

The smallest scenario selected for this milestone is a remembered evening-tea
preference:

1. an earlier interaction stores `The user prefers jasmine tea in the evening.`
   in the existing SQLite memory;
2. a fresh Stella process receives `What tea should I have this evening?`;
3. lexical retrieval supplies the stored preference to the Brain; and
4. the Brain answers with jasmine tea instead of asking which tea the user
   wants.

The deterministic test also runs the same later question against an empty
memory database. That control asks for clarification, proving that the
decision change depends on the retrieved memory rather than the wording alone.
The scenario uses no new tool or capability and leaves production architecture
unchanged.

The deterministic validation passes. A real `gpt-5.6` validation was attempted
again on 2026-09-08 using the fresh-process shape above. Both the sandbox and
the approved host environment lacked `OPENAI_API_KEY`, so the CLI stopped while
constructing its OpenAI client, before any model request or decision. This is an
environment prerequisite failure, not evidence about the scenario's model
behavior. No production code was changed and no secret value was printed.

The real acceptance result therefore remains pending: run the same two-process
check after providing the key, and require the persisted-memory process to
produce a materially different structured decision from the empty-memory
control.

## Recommendation

Build and validate one memory-driven decision. Keep the implementation small,
use the existing boundaries, and require evidence that the memory changes the
decision itself. The evening-tea scenario moves Stella from “it remembers and
answers” to “it remembers and behaves differently,” which is the highest-value
next step toward the vision without expanding the product surface.

## Follow-on milestone: contextual reasoning

The decision-continuity scenario exposed the next narrow improvement: a
follow-up request can be too short to retrieve a relevant memory on its own.
Stella retains direct retrieval for the current request and deterministically
appends additional matches from a query containing the current request plus
the already bounded conversation history. The Brain still receives those
sources as separate context fields, and the existing memory, dispatcher, and
security boundaries are unchanged.

The focused scenario is a tea-preparation follow-up. The current request is
`What should I prepare?`; history establishes a quiet evening tea at home; and
memory records a jasmine-tea preference. Together they cause a deterministic
Brain to choose the existing `record` test capability with jasmine-tea
arguments. With the same current request but no history or memory, Stella asks
for clarification and executes no tool.

This is intentionally one contextual-reasoning proof, not a general context
orchestration redesign. The implementation preserves the existing history
bound and lexical retrieval; it does not add semantic search, new tools,
personality behavior, or autonomous execution.

## Follow-on milestone: contextual uncertainty

The next narrow behavior is to recognize when context is not sufficient for a
safe action. The selected scenario asks Stella to save an evening tea plan:

- memory supplies the relevant jasmine-tea preference;
- conversation history supplies the destination `plans/evening.txt`; and
- the current request asks Stella to save the plan.

When all three are present, Stella chooses the existing action with explicit
arguments. If the destination is absent, Stella asks which file to use instead
of guessing. A retrieved memory about an unrelated evening-plan detail also
leads to clarification, proving that retrieval alone is not treated as
permission to act.

This remains a bounded decision at the existing Brain and dispatcher boundary.
It adds no tools, semantic retrieval, personality behavior, proactivity, or
autonomous execution.

## Follow-on milestone: behavioral personality

The next narrow behavior is one explicit preference that changes how Stella
responds when it is relevant. The selected scenario stores `The user prefers
one-line status update responses.` in one interaction, then applies that
preference to status requests in two later interactions. Stella returns a
one-line status response consistently; without the retrieved preference, the
same request returns a normal fuller response.

This is personality through behavior, not a persona framework. The preference
is an ordinary explicit memory item, the Brain applies it only when relevant,
and the current request remains authoritative. No new tools, embeddings,
emotion model, proactivity, or autonomous execution are needed.

## Next milestone review: meaningful proactivity

The implemented slice is deliberately one-shot and bounded. The normal
architecture remains request-driven: `Stella.process()` starts with user input,
retrieves memory, asks the Brain for one bounded decision, and returns. The new
`evaluate_due_task_event()` path evaluates one supplied event, but there is no
event ingress, notification sink, scheduler, background loop, or
deduplication state.

### Proposed smallest scenario

Prove one one-shot event check using an event supplied directly by a trusted
application caller:

1. The caller submits one structured `task_deadline` event without a user
   message. It identifies an open task whose deadline is due now, together
   with a trusted, separately stored delegation that permits reminders for
   due tasks.
2. Stella receives the event as context and decides `answer`, producing a
   concise attention notice such as “The expense report is due today.” The
   result is a notification candidate for a trusted sink; Stella does not send
   it or act on the task in this proof.
3. The caller submits the same event without the scoped reminder delegation.
   Stella decides `ask`, requesting permission to notify the user rather than
   silently increasing its authority.
4. The caller submits the same shape of event for a task that is already
   completed, not due, stale, or already handled. Stella decides `do_nothing`
   and produces no notice, tool call, or memory write.

The event should be the only new input in this proof. No scheduler needs to
discover it, and no action needs to be executed. The meaningful distinction is
that Stella notices an actionable condition, respects the permission boundary,
and deliberately ignores an irrelevant one.

### Implemented boundary

The focused implementation provides immutable `DueTaskEvent` data,
`ProactivityDelegation` policy data, and a `ProactivityResult` with exactly
`INFORM`, `ASK`, or `DO_NOTHING`. `Stella.evaluate_due_task_event()` is a pure
one-shot evaluation path: it does not consult ordinary memory, call the LLM,
invoke the Brain, dispatch a tool, write memory, or deliver a notification.

This is intentional. Ordinary memory cannot grant permission, and no LLM
output participates in authorization. The result is only a bounded decision
for a trusted caller to handle later under its own delivery policy.

### Why this is genuine proactivity

The decision is triggered by an observed condition rather than a user question.
Stella must decide whether the condition deserves attention, rather than merely
formatting every incoming event as a notification. The completed/not-due
control proves that proactivity includes disciplined non-action and is not an
always-alert event relay.

The event evaluator uses only its explicit `INFORM`, `ASK`, and `DO_NOTHING`
outcomes. `INFORM` is returned as a notification candidate to a trusted caller;
it does not silently send a message or execute a tool. This keeps the proof
about judgment, not delivery infrastructure.

### Permission versus prior delegation

Prior delegation is not the same thing as a memory item. A user may explicitly
delegate one narrow authority, such as “you may remind me about open tasks that
are due.” That delegation may permit a due-task attention notice, but it does
not permit Stella to complete the task, edit a file, contact another person,
spend money, or invoke any tool. The delegation must be stored and checked as
trusted policy data, scoped by event kind, recipient/channel, and expiry; a
retrieved memory saying that the user likes reminders cannot grant authority.

If a relevant event arrives without the matching delegation, Stella must ask
whether the user wants to be notified or return a non-delivered recommendation
to the trusted caller. It must not infer consent from prior conversation,
silence, a general preference, or the event payload itself. A delegation also
cannot override a current explicit request, a safety rule, or a tool approval.

### Remaining architecture changes for real integration

The one-shot evaluator has focused in-process event and delegation types. A real
event integration would still need:

- a generalized immutable event envelope with an allow-listed event kind,
  stable event identifier, source, timestamp, and structured payload;
- a trusted event ingress that can invoke the one-shot evaluator without
  fabricating user input;
- explicit event data in the Brain payload, separate from conversation history,
  retrieved memory, and tool observations;
- a trusted, separately scoped and persisted delegation/policy input with
  expiry, not represented as ordinary memory;
- event-aware memory retrieval only when the event provides a meaningful query,
  without treating retrieved memory as permission;
- a trusted notification-candidate/result sink for `INFORM`, with no implicit
  delivery or tool execution; and
- idempotency handling keyed by the stable event identifier so repeat delivery
  cannot create repeated attention notices.

The existing dispatcher, risk levels, approval rules, memory-write contract,
context bounds, and maximum tool-step limit remain unchanged. The event result
is intentionally separate from the ordinary Brain decision enum, and this
milestone adds no tool.

### Safety concerns

- Event payloads are untrusted observations; the Brain must not follow
  instructions embedded in them or treat them as authorization.
- Only trusted application code may create or deliver an event, classify its
  kind, and select its notification destination.
- Prior delegation must be explicit, narrow, current, and independently
  verifiable. Memory retrieval, event content, or model prose cannot create or
  broaden it.
- A malformed, stale, duplicate, conflicting, or incomplete event must result
  in `do_nothing` or a safe clarification path, never a guessed action.
- Stella must ask when the event is meaningful but permission is absent,
  ambiguous, expired, or scoped to a different kind of notice.
- Stella must do nothing for irrelevant, completed, not-due, already-handled,
  or low-confidence conditions, even when a broad reminder preference exists.
- The first milestone must not execute side effects. Any later action must use
  the existing dispatcher and approval boundary.
- Notification volume needs deterministic bounds and deduplication before any
  background integration is considered.
- Event data and any resulting memory write need explicit scope and retention;
  no automatic storage of every event is acceptable.

### Observation versus action authority

An event is evidence to evaluate, not an instruction to execute. The first
milestone may observe one condition and return one bounded decision. It may
produce a notification candidate only when the trusted delegation permits that
specific notice. It may not create a tool decision merely because the event
claims that an action is needed. Any future tool action still requires the
existing exact capability lookup, validation, risk classification, approval,
and audit path, plus any user permission required for that action.

### Explicitly defer

Defer schedulers, daemons, timers, polling, webhooks, push integrations,
background workers, retries, autonomous loops, parallel event handling, action
execution, new tools, UI, voice, video, notification providers, embeddings,
semantic event ranking, broad delegation policies, multi-user event ownership,
and automatic event-to-memory capture. First prove the single event decision,
its permission clarification, and its deliberate no-op control with
deterministic tests. Only then consider how events are produced or delivered
in a real application.

## Next milestone review: multimodal interface readiness

Do not implement multimodal support yet. The current core is conceptually
interface-agnostic at the decision boundary: `Brain`, `Decision`, `Memory`,
`Tool`, `ToolDispatcher`, and `Stella` do not know about the CLI or an OpenAI
provider. However, the current data contracts are text-native:
`Context.user_input` is a string, `Message.content` is text, memory stores text,
tool observations contain text output, and `StellaResult.response` is a string.
The CLI also owns the only conversation-history assembly path.

The correct conclusion is that Stella has a good architectural seam, but is not
yet multimodal-ready. Adding an audio or vision provider directly to `Brain` or
`Stella` would couple the intelligence layer to an interface and make future
modalities harder to secure and test.

### Smallest viable readiness scenario

The smallest proof should be a provider-neutral normalized input envelope, not a
real audio or vision feature:

1. An interface adapter supplies one text input and one non-text attachment
   reference with explicit modality metadata.
2. The adapter normalizes both into one bounded user turn containing typed input
   parts, provenance, and safe references rather than raw media bytes.
3. The existing Brain receives that normalized turn through the same Context,
   memory, and tool decision path and returns the same provider-agnostic
   `answer`, `ask`, `tool`, or `do_nothing` decision model.
4. A deterministic test proves that the core does not inspect which interface
   produced the turn and that the output remains a structured result for an
   interface adapter to render.

This proves readiness without pretending that Stella can understand an image,
audio stream, video, or desktop screen. Actual modality understanding remains a
later adapter/provider concern.

### Input normalization

Interface adapters should convert raw inputs into a small, typed envelope before
the core sees them. A future input part needs at least:

- a modality such as `text`, `audio`, `image`, `video`, or `environment`;
- bounded content or a controlled reference, never an unbounded binary blob in
  the decision context;
- provenance identifying user-provided, tool-produced, or model-derived data;
- capture time and source/interface metadata; and
- transformation metadata such as transcript, OCR, caption, or confidence,
  with the derived status kept distinct from the original observation.

Text can enter unchanged. Audio may be normalized to a transcript plus a
controlled audio reference. Images and video may eventually provide OCR,
captions, or bounded frame observations plus references. Desktop or environment
input should enter as explicit observations, never as implicit permission to
control the environment.

Normalization belongs at the interface boundary. The Brain should receive
meaningful normalized observations and metadata, not microphone, camera,
window-manager, codec, or provider SDK objects.

### Output representation

`StellaResult.response: str` is sufficient for the current CLI but is too narrow
as a long-term interface contract. The future core should separate the decision
and outcome from rendering, with a bounded response representation containing
text and optional interface-neutral output metadata. Text, audio, visual, or
desktop renderers should be adapters selected after the core decision.

The core must not decide to speak, display, stream, or control a device merely
because the input arrived through that modality. A response can be rendered in
multiple ways, but the selected action, approval, audit, memory, and safety
semantics must remain shared.

### Context and history

The current `Context` already provides the right ownership boundary—current
input, bounded history, retrieved memory, and tool observations—but its fields
need a future typed-part representation. History should retain the normalized
user turn and relevant provenance, not duplicate every raw media representation.

Selection must remain deterministic and bounded by turns, parts, references, and
derived text size. Media references need explicit lifecycle and retention rules;
history must not silently become a media archive. The original user input and
derived transcript/OCR/caption should remain distinguishable so later decisions
can account for uncertainty and provenance.

### One shared intelligence path

All interfaces should converge on the same flow:

```text
interface adapter
  -> normalized input parts + modality metadata
  -> bounded Context + Memory
  -> Brain decision
  -> trusted ToolDispatcher when explicitly selected
  -> interface-neutral result
  -> interface adapter rendering
```

Audio transcription, image understanding, video sampling, and desktop capture
belong at adapters or provider boundaries. They may produce observations for the
same Brain/Memory/Tool system, but they must not create a second decision loop,
bypass tool authorization, or write memory automatically.

### What must remain interface-agnostic

Keep these concepts independent of text, audio, vision, video, or desktop APIs:

- `Context` semantics, bounds, provenance, and uncertainty handling;
- Brain decisions and clarification behavior;
- explicit memory retrieval and writing rules;
- tool lookup, validation, risk, approval, and audit;
- event and delegation authority boundaries;
- outcome handling and deterministic step limits; and
- provider-independent tests and result contracts.

An adapter may choose how to capture or render content. It must not redefine
what permission means, make model output authoritative, or turn a modality into
a new class of hidden tool.

### Security implications of richer inputs

Richer input increases the attack and privacy surface even when no new action is
added:

- OCR, transcripts, captions, screen text, metadata, and filenames are
  untrusted data and may contain prompt injection;
- media references must be scoped and validated so they cannot become arbitrary
  filesystem, camera, microphone, or network access;
- audio/video may contain bystanders, credentials, biometric information, or
  sensitive location data and therefore need explicit capture and retention
  consent;
- derived content needs provenance and confidence so a hallucinated caption is
  not treated as a fact or authorization;
- media content must never grant tool permission, delegation, or memory-write
  authority; and
- size, duration, frame rate, transcription, and provider costs need deterministic
  limits before content reaches the Brain.

The existing trusted dispatcher remains the only path to side effects. A future
multimodal adapter can increase what Stella observes, not what Stella is
authorized to do.

### Explicitly defer

Defer audio capture or playback, vision models, video models, camera or
microphone integrations, desktop automation, screen control, UI changes,
voice/video streaming, provider-specific multimodal APIs, raw-media persistence,
cross-modal embeddings, automatic media-to-memory capture, new tools, and any
background or realtime loop. The next multimodal implementation, when
authorized, should extend only the typed normalized-input contract with
text-only compatibility and deterministic tests for bounds, provenance, and
interface neutrality.

## Next milestone review: video/environment input readiness

Do not implement video or environment observation yet. The existing
`InputEnvelope` can carry a bounded `VIDEO` or `ENVIRONMENT` part with an
opaque reference and provenance, and the existing Brain already receives input
parts as observations. That is enough for a safe contract review, but it is
not permission to capture, retain, sample, or continuously inspect anything.

### Recommended smallest implementation

The smallest useful future proof is one user-supplied, one-shot video
observation with an explicit sampling description:

1. A user-controlled interface supplies one bounded clip reference, not raw
   video bytes, as one `InputPart` with `VIDEO` modality and `USER`
   provenance.
2. A separate adapter-level value or bounded metadata records the sampling
   window and policy: capture start/end, sample count, sampling mode, source,
   and retention disposition. These values describe what was observed; they
   do not authorize observation or action.
3. A provider-neutral video-understanding boundary receives that explicitly
   supplied reference and sampling request. It returns bounded derived text or
   frame observations marked with `MODEL` provenance and `derived_from=video`.
4. The original clip reference and derived information enter the existing
   `Context`/`InputEnvelope`; the existing Brain, memory retrieval, tool
   dispatcher, and final response path handle them without a video-specific
   decision loop.

This should begin with a finite clip and deterministic sampling, for example a
small fixed maximum number of ordered samples within one user-selected time
window. Exact numeric limits should be chosen with the implementation and
tested as input bounds; they must cover part count, reference length, duration,
sample count, derived-text size, and provider cost.

### Frame versus clip representation

Individual frames alone lose temporal meaning. A raw clip alone hides the
sampling actually presented to the provider. The recommended representation is
one clip reference plus an ordered, bounded sampling descriptor and bounded
derived observations. If frames are represented as separate parts, each must
carry a stable position or timestamp and a shared observation identifier; the
envelope must still preserve their order. Do not place raw frames or a full
clip in Brain context.

### Temporal context and provenance

Every derived statement must be traceable to the source clip and either a
timestamp or bounded time range. `MODEL` provenance means “provider-derived
observation,” not fact, instruction, permission, or approval. A confidence or
uncertainty field may describe provider quality, but it must not be used as an
authorization signal. Contradictory or insufficient temporal evidence should
reach the Brain as uncertainty so it can ask rather than guess.

### Privacy, retention, and observation boundaries

The default should be no raw-media persistence and no retention beyond the
explicit interaction unless the user separately chooses it. References must be
scoped, expiring or otherwise lifecycle-controlled, and must not silently
become filesystem, camera, microphone, or network access. Capture consent must
be distinct from action permission and from memory consent. Bystanders,
credentials, biometric data, location, and private screens are expected risks.

Continuous observation is materially different from one-shot image input. It
requires an explicit user-controlled observation session or lease, visible
start/stop state, bounded sampling cadence and duration, revocation, and clear
retention rules. It must not be implemented as repeated hidden calls to the
one-shot image or video path, and it must not run from ordinary conversation,
memory, prior delegation, or a proactivity decision.

### Proactivity and permissions

Video/environment observations can be evidence for a later one-shot
proactivity evaluation, but they cannot trigger action by themselves. An event
evaluator may choose `INFORM`, `ASK`, or `DO_NOTHING`; it must never infer that
the user wants to be watched, notified, or acted for merely because a stream or
reference exists. Ordinary memory is not capture consent or delegation. Prior
delegation may authorize only its exact stated action and never expands to
observation, collection, retention, or a different action.

Any resulting tool action remains subject to the existing trusted capability
lookup, validation, risk, approval, and audit path. LLM output, video-derived
text, timestamps, metadata, and provider confidence cannot grant themselves
permission.

### Explicit deferrals

Defer camera or desktop capture, continuous observation, background services,
schedulers, streaming, realtime frame delivery, video/audio fusion, vision or
video model integrations, raw-media storage, automatic media-to-memory writes,
identity or biometric recognition, UI indicators, notification delivery, new
tools, and autonomous action. The first implementation should prove only one
explicit finite observation, bounded deterministic sampling, provenance and
uncertainty preservation, and unchanged authority boundaries.

## Audio input milestone status

The provider-neutral typed envelope is now used by a minimal one-shot audio
path. `TranscriptionProvider` is the only boundary a transcription
implementation must satisfy; `Stella.process_input()` preserves the audio
observation, appends derived text with provenance, and delegates to the
existing text reasoning path. Deterministic tests cover text compatibility,
fake transcription, provenance, and the fact that audio content cannot grant
authority.

This does not add audio capture, a transcription provider, audio output,
streaming, wake words, background work, or any new tool. Those remain deferred
until a concrete interface requirement justifies them.

## Image input milestone status

The provider-neutral typed envelope now also supports a minimal one-shot image
path. `VisionProvider` is the only boundary an image-understanding
implementation must satisfy; `Stella.process_input()` preserves the original
image observation, appends bounded derived text marked with `MODEL` provenance,
and delegates to the existing text reasoning path. Deterministic tests cover
provider use, preservation, integration, failure handling, and the existing
untrusted-input security boundary.

This does not add camera capture, image generation, a vision provider, UI,
streaming, video, new tools, or authority. Image-derived text cannot authorize
tools, memory writes, approvals, or delegation; those remain controlled by the
existing Brain/runtime boundaries.

## Audio output milestone status

The existing final response can now be rendered through a minimal
provider-neutral speech boundary. `SpeechOutput` carries only bounded response
text, `SpeechProvider` returns a provider-neutral artifact reference, and
`Stella.speak()` runs after normal processing without re-entering decision,
tool, permission, or memory logic. Deterministic tests cover bounds, provider
handoff, integration, and the absence of authority or context in the speech
request.

This does not add audio capture, wake words, continuous listening,
interruption/barge-in, streaming, UI, provider integrations, or autonomous
behavior. Speech output remains presentation only.

## Trusted one-shot proactive handoff milestone status

The recommended handoff is now implemented around the existing due-task
evaluator. `Stella.handoff_due_task_event()` is the trusted application
boundary: the caller constructs the event and establishes its stable `event_id`,
while the LLM is not involved in identity or trust. Each new event is evaluated
with the separately supplied scoped delegation and returns an inspectable
`INFORM`, `ASK`, or `DO_NOTHING` result. A repeated ID is suppressed as an
inspectable `DO_NOTHING` result with `duplicate_suppressed=True`.

The suppression set is process-local and deliberately not a retention system.
The handoff does not consult ordinary memory, execute tools, deliver a
notification, write memory, or grant authority. Missing or mismatched
delegation still produces `ASK`; irrelevant events produce `DO_NOTHING`; and
distinct IDs are evaluated independently. Deterministic tests cover the
trusted handoff, duplicates, distinct events, scoped delegation, missing
permission, irrelevant events, and the absence of action/tool execution.

This completes the recommended one-shot handoff milestone. Schedulers,
daemons, background services, notification delivery, persistent event state,
new tools, autonomous loops, and continuous observation remain deferred.

## Post-foundation roadmap review: 2026-09-09

This review compares the current project with [`STELLA_VISION.md`](STELLA_VISION.md)
after the completed memory/continuity, contextual reasoning, contextual
uncertainty, behavioral personality, bounded proactivity, and multimodal
input/output milestones. It does not propose production changes.

### What Stella proves now

The core loop is demonstrated in a deliberately bounded form:

```text
context + memory + trusted tools
          -> decision
          -> bounded action
          -> observed outcome
          -> explicit durable memory
          -> later behavior
```

The project has evidence that:

- relevant history and memory can change a decision, tool choice, arguments,
  clarification, and response;
- insufficient or irrelevant context leads to clarification or no action rather
  than guessing;
- behavioral preferences are applied when relevant without a persona system;
- successful meaningful tool outcomes can produce one explicit durable memory
  that survives a fresh process and changes later behavior;
- tools remain behind trusted lookup, validation, risk, approval, and audit;
- due-task proactivity can distinguish `INFORM`, `ASK`, and `DO_NOTHING` while
  keeping ordinary memory and model output outside authority decisions; and
- text, audio, image, finite video, environment references, and speech output
  have provider-neutral boundaries without coupling the Brain to an interface.

This is enough to prove Stella's original MVP identity. The multimodal work
increases what can be observed or rendered; it does not by itself make Stella
more agentic. The strongest differentiator remains memory and context changing
bounded decisions.

### Remaining milestones by importance

#### 1. Required for Stella's core vision

These are the smallest remaining obligations implied by the documented loop,
not a request for a larger product:

- Keep the context-to-decision-to-outcome-to-memory path reliable across a
  small acceptance matrix, including successful, failed, irrelevant, and
  ambiguous outcomes.
- Preserve decision continuity as context grows: relevant history, memory,
  observations, provenance, and uncertainty must remain bounded and must not
  silently displace the facts needed for a decision.
- Keep authority explicit as capabilities expand: ordinary memory, event data,
  multimodal observations, provider output, and model prose must never become
  permission or delegation.
- Treat selective outcome learning as the documented form of self-improvement:
  meaningful outcomes may be explicitly remembered and later used. No
  self-modifying model or automatic learning subsystem is part of the vision.

The first three are primarily reliability and acceptance work. They do not
justify embeddings, a new memory design, more tools, or autonomous loops.

#### 2. Important future capabilities

These support the long-term direction once a concrete need is demonstrated:

- A trusted one-shot event ingress and notification-candidate handoff so the
  existing proactivity evaluator can be invoked by an application and return a
  bounded, inspectable attention result. It must retain `INFORM`/`ASK`/
  `DO_NOTHING`, stable event identity, narrow delegation checks, and no hidden
  delivery or action.
- Better deterministic memory relevance, conflict handling, provenance, and
  scope if the current lexical retrieval produces a demonstrated failure.
- More contextual decision cases where current request, history, memory,
  observations, and uncertainty jointly affect an action, not just a response.
- Real provider integrations at the existing audio, image, video, or output
  boundaries only when an interface requirement exists. Continuous observation
  would require a separate explicit session/consent model.
- Carefully bounded multi-step action sequences only when one-step outcomes
  expose a concrete need, with the existing dispatcher and approval boundary
  retained.

#### 3. Optional feature expansion

These are possible but do not advance Stella's identity today:

- embeddings, vector search, semantic ranking, or a broad Memory redesign;
- larger tool/plugin ecosystems and external service integrations;
- continuous camera, microphone, desktop, or environment observation;
- streaming, wake words, interruption, realtime UI, and voice/video UX;
- background daemons, schedulers, retries, parallel plans, or autonomous
  loops;
- a persona, identity, emotion, or general personality framework;
- automatic media, conversation, or event-to-memory capture;
- self-modifying prompts/models, reward learning, or broad self-improvement;
  and
- multi-user memory ownership, migration, or enterprise policy systems.

### Recommended next milestone

### Trusted one-shot proactive handoff

Build one application-level path around the already implemented due-task
evaluator:

1. A trusted caller submits one structured event with a stable identifier and
   no fabricated user message.
2. Separately supplied, narrowly scoped delegation is checked for the exact
   attention outcome; ordinary memory and event text cannot create it.
3. Stella returns one inspectable notification candidate, clarification, or
   no-op, with the event identity and reason preserved for the caller.
4. Duplicate, stale, irrelevant, incomplete, or unauthorized events produce
   no delivery and no action.

The milestone should stop at the trusted candidate handoff. It should not add a
scheduler, daemon, notification provider, UI, tool action, background loop, or
automatic memory write. Deterministic tests should prove permitted inform,
missing-permission ask, irrelevant no-op, duplicate suppression, and scoped
delegation. This is the smallest change that makes proactivity observable as a
real application behavior while preserving the principle that awareness does
not increase authority.

### What to defer

Defer all optional expansion above, plus any continuous multimodal observation,
until the one-shot handoff exposes a concrete requirement. In particular, do
not build self-improvement beyond selective explicit outcome memory, and do not
turn prior delegation or remembered preference into permission to observe or
act. Stella should become more useful through better bounded judgment before
it becomes broader, more autonomous, or more complex.

## Video input milestone status

The smallest finite video path is now implemented. `VideoSampling` bounds one
explicit clip window and sample count; `VideoProvider` receives only the
bounded video reference and sampling description; and bounded
`VideoObservation` results are converted into text input parts with `MODEL`
provenance and timestamp metadata. `Stella.process_input()` preserves the
original video reference and sends the derived observations through the same
existing reasoning path.

The implementation accepts one clip and at most four observations, rejects raw
video content, rejects observations outside the requested time window, and
does not persist raw media. Deterministic tests cover normal flow, bounds,
provenance, provider failures, raw-media exclusion, and the existing
untrusted-input authority boundary.

This does not add capture, continuous observation, streaming, background work,
retention systems, UI, video-model integrations, new tools, proactivity
triggers, or autonomous behavior. A video reference and its derived text can
inform a decision, but never grant observation consent, delegation,
permission, approval, or tool authority.

## Current checkpoint: 2026-09-09

This review re-evaluates the implementation against
[`STELLA_VISION.md`](STELLA_VISION.md) and the roadmap after the trusted
one-shot proactive handoff milestone. No production code was changed for this
review.

### Core Stella behaviors now working end-to-end

The original MVP loop is genuinely demonstrated in bounded scenarios:

- Context, bounded history, relevant memory, uncertainty, and observations
  reach the Brain together and can change a decision or tool arguments.
- Stella can answer, ask for missing information, use a trusted tool, or do
  nothing instead of treating every request as a chat response.
- Tool execution remains application-authorized through validation, risk,
  approval, and audit boundaries.
- A successful meaningful tool result can become one explicit durable memory;
  the memory persists across processes and changes later behavior.
- A remembered preference can change a later decision, not only response prose,
  and a relevant behavioral preference is applied without a persona system.
- Insufficient or irrelevant context produces clarification or no action rather
  than an invented target, destination, or permission.
- A supplied due-task event can produce inspectable `INFORM`, `ASK`, or
  `DO_NOTHING` results with stable application-owned identity and duplicate
  suppression. Memory and model output cannot grant delegation.
- Text, audio, image, finite video, and speech have provider-neutral boundaries
  that preserve provenance and keep interface concerns outside the Brain.

Together these prove the original distinction from a normal chatbot: Stella's
memory and context can change what it decides to do, and its outcomes can
change what it knows later. The proactivity path is deliberately an
application-facing candidate result, not silent delivery or autonomous action.

### Important capability still missing

Nothing essential is missing from the original MVP proof. The important
remaining gap for the broader vision is user-visible proactive attention: the
trusted handoff currently returns a candidate to its caller, but no bounded
application output consumes that candidate and presents the user with the
notice or permission question.

Other limitations are intentionally bounded rather than missing MVP
mechanisms: lexical memory retrieval, one-shot outcome learning, one finite
video observation, and process-local duplicate state. They should be expanded
only when a concrete acceptance case fails.

### Highest-value remaining roadmap item

The highest-value next milestone is a **one-shot proactive attention result**:

1. A trusted application submits one already-validated due-task event through
   the existing handoff.
2. Stella returns exactly one user-facing attention result derived from the
   inspectable `INFORM`, `ASK`, or `DO_NOTHING` outcome.
3. `INFORM` remains limited to the exact scoped delegation; `ASK` requests
   permission and performs no action; `DO_NOTHING` produces no attention.
4. The result retains the stable event ID and duplicate suppression behavior.

The smallest implementation should use a provider-neutral result/caller
contract or deterministic test sink, not a notification service or UI. It
should prove that a meaningful condition can reach a user-facing boundary and
that irrelevant, duplicate, or unauthorized conditions remain silent. No
tool execution, memory write, LLM decision, or background process is needed.

This matters because it is the next behavior that cannot be mistaken for
ordinary request/response chat: Stella notices a condition without a new user
question, but still asks or remains silent when authority is absent. It also
keeps proactivity's authority boundary explicit instead of hiding delivery in
the evaluator.

### What should explicitly not be built yet

Defer the following until the one-shot attention result exposes a concrete
need:

- schedulers, daemons, polling, background services, retries, or autonomous
  loops;
- automatic notification delivery, channels, UI, voice, or realtime alerts;
- persistent event history or cross-process deduplication;
- new tools or any action based only on an event or delegation;
- treating ordinary memory, preferences, event content, or model prose as
  observation consent or permission;
- embeddings, semantic memory, broad conflict resolution, or a Memory rewrite;
- continuous camera, microphone, desktop, or environment observation;
- general personality, emotion, identity, or self-modifying learning systems;
  and
- additional modalities or provider integrations without a concrete interface
  requirement.

The next step should make one already-proven proactive decision observable at a
trusted boundary, then pause and reassess. Stella should gain clearer judgment
before it gains more autonomy or surface area.

## One-shot proactive attention result milestone status

The recommended user-facing boundary is now implemented. The trusted caller
uses `Stella.present_due_task_event()`, which invokes the existing
`handoff_due_task_event()` and returns a `UserFacingProactivityResult`.
`INFORM` and `ASK` preserve their inspectable message for presentation;
`DO_NOTHING`, including duplicate events, has no user-facing message.

This remains one-shot and synchronous. The result contract does not send a
notification, invoke a tool, write memory, consult the LLM, persist event
history, or grant authority. Deterministic tests cover permitted informing,
permission questions, irrelevant no-op behavior, duplicate silence, and the
absence of action/tool execution. Notification infrastructure and background
behavior remain explicitly deferred.

## User-facing MVP review: 2026-09-09

This review evaluates the current system as a user would experience it, using
the existing CLI-facing `Stella.process()` path and the synchronous
`present_due_task_event()` boundary. No production code was changed. The
behavioral claims below are supported by the deterministic tests named in each
row; they do not claim that an external model will make every decision
correctly without a live-provider validation.

| Interaction | Intended behavior | Current result | Concrete gap |
| --- | --- | --- | --- |
| Normal conversation with a follow-up | Use recent context and answer or ask appropriately | **Works.** The CLI carries user/assistant history; `select_conversation_history()` keeps the latest 20 messages and the Brain receives it. Context tests also prove stable truncation. | History is process-local and disappears in a fresh CLI process. That is a deliberate boundary, not an MVP failure. |
| Memory, then later recall | A meaningful remembered fact should be retrieved and affect a later response | **Works end-to-end.** SQLite persistence, fresh-process retrieval, and later response impact are covered by the outcome-memory and persisted-memory tests. | Retrieval is lexical and requires overlapping terms; phrasing changes can miss a relevant fact. Do not address this with embeddings yet. |
| Uncertain action request | Ask instead of guessing a missing target or destination | **Works.** Missing-target and irrelevant-memory tests return `ASK`; the Brain prompt explicitly rejects guessing. | Clarification quality is model-dependent and there is no richer clarification state. One concise question is sufficient for the MVP. |
| Useful read/record tool use | Choose a bounded useful tool, execute it, and explain the result | **Works.** Structured tool decisions reach the dispatcher; arguments are validated, observations return to the bounded flow, and the final response includes the result. | The CLI has no separate progress/status display while a tool runs. Current synchronous behavior is acceptable at MVP scale. |
| Tool outcome to memory | A successful meaningful outcome may create one explicit durable memory; failures must not | **Works.** Tests prove independent file content can lead to one write, persistence, later retrieval, and response impact; failed/irrelevant results stay out of memory. | Meaningfulness remains a Brain proposal guarded by a small runtime policy. It is intentionally not automatic learning. |
| Behavioral preference | Apply one relevant preference consistently without exposing a persona layer | **Works.** The status-response and decision-continuity tests show the preference changes behavior and can change a decision, not just stored text. | Preference retrieval has the same lexical limitation as other memory. A preference-management framework is not justified. |
| Irrelevant proactive event | Notice it but produce no attention or action | **Works.** Completed, not-due, already-handled, and duplicate events produce inspectable `DO_NOTHING` with no message, tool, LLM, or memory activity. | The normal CLI has no event-ingress command; a trusted application caller must supply the event. That is the intended one-shot boundary. |
| Meaningful proactive event | Inform when narrowly delegated; otherwise ask permission, never act | **Works at the trusted user-facing boundary.** `present_due_task_event()` exposes `INFORM`/`ASK` messages and keeps `DO_NOTHING` silent; ordinary memory cannot create delegation. | There is no notification delivery or UI sink. The result is observable to a trusted caller, but not delivered automatically. This is deliberately deferred. |
| Dangerous action | Require exact, trusted approval before execution | **Works.** Filesystem write/delete and network actions are classified by application code; the CLI asks for action-specific approval, and tests prove denial, missing, mismatched, or model-supplied approval cannot bypass dispatch rules. | Approval is currently a synchronous CLI prompt and process-local. A richer approval UI is feature expansion, not an MVP requirement. |

### Overall assessment

The original MVP is now genuinely working end-to-end in the important sense:
context and memory can change a structured decision; a trusted tool can act;
the outcome can be interpreted and selectively persisted; and the persisted
lesson can change a later interaction. The system also demonstrates disciplined
uncertainty, behavioral preference, bounded no-op behavior, and authority
separation for proactive events. This is a real behavioral distinction from an
answer-only chatbot, even though the current interface is a small synchronous
CLI.

The most visible user-facing limitations are not missing core capabilities:
conversation history is not durable across processes, memory retrieval is
lexical, tool work is synchronous, and proactive results are exposed to a
trusted caller rather than sent through a notification channel. Each boundary
is documented and keeps the MVP small and safe.

### Smallest recommended improvement

Do not add another capability yet. The smallest materially useful next step is
a narrow end-to-end acceptance path for the existing proactive result: have one
trusted application/CLI caller render the returned `INFORM` or `ASK` text and
prove that `DO_NOTHING` remains silent. This should be a presentation adapter
or test sink only; it must not add a scheduler, notification service,
persistent event history, or action execution. If that boundary is already
adequate for the consuming application, the correct next step is to pause and
use Stella rather than expand the roadmap.

### Explicitly defer

Defer embeddings and a Memory rewrite, automatic transcript/event capture,
durable conversation history, new tools, richer approval UX, notification
infrastructure, schedulers and background observation, autonomous loops,
continuous multimodal input, general personality systems, and self-improvement
beyond selective explicit outcome memory. None is needed to strengthen the
current MVP proof; adding them now would expand the surface without making
Stella's core behavior clearer.
