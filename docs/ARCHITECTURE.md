# Stella Architecture

## What Stella is becoming

Stella is being built as a small, understandable personal assistant foundation. The long-term direction is an assistant that can use language models, conversation context, explicitly stored memory, decisions, and tools in a controlled way.

The current project is an MVP foundation, not a complete assistant. The priority is to establish a few clear, provider-agnostic boundaries before adding autonomous behavior. Each component is intentionally small and can be tested independently.

## Current MVP philosophy

The project favors explicit data structures, narrow interfaces, deterministic behavior, and ordinary Python over framework-heavy abstractions. Components should have one clear responsibility. In particular, the current foundation does not hide persistence, retrieval, planning, tool execution, or conversation management behind implicit behavior.

The interfaces are being established before the full response pipeline is built. The current implementations are suitable for unit testing and experimentation, but they do not yet form a complete end-to-end assistant.

## Current architecture

### LLMClient and Message

`stella.llm.Message` is a small immutable representation of a conversation message. It contains a `role` and `content`. The LLM layer also continues to accept normal message dictionaries, so existing conversation messages can be passed without converting them first.

`stella.llm.LLMClient` is the provider-agnostic interface for sending a list of messages to a language model and receiving a string response. `FakeLLMClient` provides a deterministic response for tests.

The abstraction does not decide how context is assembled, store conversations, or choose a model.

### OpenAI client

`stella.openai_client.OpenAILLMClient` is the current concrete `LLMClient` implementation. It uses the official OpenAI Python SDK, reads `OPENAI_API_KEY` from the environment, and accepts the model and optional base URL through its constructor. `Message` objects are converted to the dictionary shape expected by the SDK; existing dictionaries are passed through.

When the Brain is choosing an action, the optional provider-neutral
`LLMClient.chat_with_tools()` boundary is used. The OpenAI adapter uses the
Responses API for this native-tool path: it maps the approved tool inventory
into function schemas and normalizes returned `function_call` output items into
`LLMToolCall` values. `LLMBrain` then produces the existing provider-agnostic
`DecisionKind.TOOL` decision. The adapter converts Stella's compact argument
descriptions into bounded string properties; the trusted dispatcher remains
responsible for definitive argument validation.

This path deliberately uses Responses rather than Chat Completions because the
configured GPT-5.6 endpoint rejects function tools through Chat Completions
when reasoning is enabled. Stella's ordinary text `chat()` path remains on
Chat Completions and is unchanged.

Clients without native tool calling inherit a compatibility implementation that
uses the existing text/JSON protocol through `chat()`. This preserves the
fallback for simple and test clients without making the Brain depend on the
OpenAI SDK. Native calls improve selection reliability, but they do not grant
authority: only `ToolDispatcher` can look up, validate, approve, and execute a
capability.

`stella.brain.ToolUsePolicy` supplies the application-level distinction that
native `auto` alone cannot provide. It deterministically returns
provider-neutral `AUTO` for ordinary requests and `REQUIRED` only when a
request clearly asks for live, local, external, workspace, or explicit
inspection/tool information and at least one available tool is relevant by
description. It never selects a capability or supplies arguments. After a
successful tool observation, the next Brain pass returns to `AUTO` so the
model can answer from that observation. If `REQUIRED` produces no native or
textual tool decision, `LLMBrain` converts an attempted direct answer into a
clarification rather than accepting it as authoritative. Tool execution and
all security decisions remain exclusively in `ToolDispatcher`.

This is the only provider-specific component currently present. Its tests mock the SDK and do not make network calls.

### Context

`stella.context.Context` is the explicit response context currently needed by
Stella. It preserves the compatible string `user_input`, a bounded
`InputEnvelope`, a `conversation_history` made up of text `Message` objects,
and any `retrieved_memories` supplied for the current decision. A legacy text
context automatically receives one text `InputPart`; callers can supply
bounded text, audio, image, video, or environment parts without changing the
Brain interface. Stella creates a derived context with retrieved memories
rather than mutating the caller's context.

`InputPart` carries a modality, provenance, bounded text content or opaque
reference, and bounded string metadata. `InputEnvelope` bounds the number of
parts and serializes them for the Brain as observations. It does not decode
media, fetch references, authorize tools, or write memory. Provenance and
metadata are descriptive data, not permission or approval.

### Audio input boundary

`stella.audio.TranscriptionProvider` is the provider-neutral interface for one
bounded audio-to-text request. `normalize_input()` sends only audio parts to
that interface, preserves the original audio part, and appends a bounded text
part marked with `MODEL` provenance and `derived_from=audio` metadata. It
returns the transcript as the compatibility `Context.user_input` value.

`Stella.process_input()` performs this one-shot normalization and then calls
the existing `Stella.process(Context)` path. A text envelope bypasses
transcription and follows the same path as today. The transcription provider
cannot return a decision, tool authorization, memory write, or approval; its
text is an untrusted observation for the existing Brain and security boundary.

### Image input boundary

`stella.vision.VisionProvider` is the provider-neutral interface for one
bounded image-to-text observation request. `normalize_input()` sends image
parts to that interface, preserves the original image part, and appends a
bounded text part marked with `MODEL` provenance and `derived_from=image`
metadata. `Stella.process_input()` then supplies both the derived text and the
typed envelope to the same existing reasoning path.

The vision provider cannot return a decision, tool authorization, memory write,
or approval. Image-derived text is untrusted observation, just like a
transcript or tool result; it cannot grant authority or override trusted
runtime permissions. Multiple image parts are handled deterministically in
envelope order, subject to the existing input bounds.

### Video input boundary

`stella.video.VideoSampling` describes one finite, user-supplied clip window
with bounded start/end times, sample count, and sampling mode.
`VideoProvider.analyze()` receives only the original `VIDEO` `InputPart` and
that sampling description. The clip is represented by its bounded reference;
raw media is not placed in the Brain context.

The provider returns bounded `VideoObservation` values. Normalization preserves
the original video reference and appends one text `InputPart` per observation,
marked with `MODEL` provenance and bounded sample/timestamp metadata. These
derived observations reach the existing `Context.user_input` and Brain path;
there is no video-specific decision loop or automatic persistence.

Only one finite clip and at most four observations are accepted in this
milestone, with observations required to remain within the requested sampling
window. This boundary describes an explicit observation supplied by the user;
it does not capture media, create observation consent, grant delegation or
permissions, authorize tools, or establish continuous observation.

### Audio output boundary

`stella.audio_output.SpeechOutput` is the bounded provider-neutral
representation of an already-selected final response. `SpeechProvider` accepts
only that text and returns a provider-neutral `SpeechArtifact` reference.
`Stella.speak()` performs this one-shot rendering after `Stella.process()` has
returned; it does not invoke the Brain, re-run tools, retrieve or write memory,
or change the existing `StellaResult`.

Speech rendering is an interface concern after decision and action. The speech
provider receives no permissions, approvals, tool arguments, memory, or input
context, and its output is not fed back into Stella's decision path.

Context is only a data structure at this stage. It does not call an LLM, retrieve memory itself, summarize history, or manage persistence.

### Memory and MemoryItem

`stella.memory.MemoryItem` is an immutable representation of one stored memory item. It contains text content, an optional backend-assigned identity, a closed `MemoryType` (`SEMANTIC` or `EPISODIC`), and a closed `MemoryScope` (`USER` or `STELLA`). The default remains semantic user memory for compatibility. `MemoryWriteRequest` explicitly identifies one item that should be stored, and `MemoryWriteResult` records that Stella performed that write.

`stella.memory.Memory` defines the provider-agnostic interface for explicitly storing and retrieving memory items. `InMemoryMemory` is the current deterministic implementation. It keeps items in a list, can return all items or items matching the query's meaningful keywords, and accepts an optional `MemoryType` filter. Stella supplies the current user input as the retrieval query; there is no semantic ranking or relevance model.

`SQLiteMemory` is the persistent implementation. It uses Python's standard-library SQLite support and takes a configurable database path. It keeps the same insertion-order and keyword-overlap retrieval behavior as `InMemoryMemory`, while allowing items to survive process restarts. It exposes `close()` and context-manager support so its database connection can be released cleanly.

Memory lifecycle is deliberately separate from Brain writes. Each backend is constructed with a trusted scope and only stores or retrieves items in that scope. Trusted application code may call `Memory.update(memory_id, replacement)` or `Memory.delete(memory_id)` using an existing backend-assigned identity. Updates preserve the backend scope while applying a validated memory type. Both implementations reject missing or invalid identities, identity-bearing replacements, invalid types, mismatched scopes, and empty replacement content without changing memory. These operations return a boolean and do not allow user or model content to authorize broader ownership.

Both memory implementations now use the same small deterministic matcher. Text
is tokenized case-insensitively, common stop words are ignored, and a query
matches when it shares at least two meaningful terms with a memory item (or
the one available meaningful term for a one-term query). The relevance score is
the number of shared normalized terms. Matching results are ranked by score,
then by backend-assigned logical recency, then by ID, all descending. For
example, “What is my favorite color?” matches “The user's favorite color is
cobalt blue.” A newer result wins only when relevance ties. Query-less
retrieval preserves insertion order; a type filter can narrow either form.
SQLite applies the same scoring and ranking in Python, keeping behavior
consistent with `InMemoryMemory`.

This is still keyword overlap, not semantic search. It does not understand
synonyms, meaning, or context, and it may miss relevant memories or match items
that happen to share common keywords. Recency is a deterministic storage
sequence, not wall-clock time, and the system does not resolve contradictory
facts.

The schema is intentionally minimal:

```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'semantic',
    scope TEXT NOT NULL DEFAULT 'user',
    created_at INTEGER NOT NULL DEFAULT 0
)
```

SQLite is used at this stage because it provides local persistence with no separate service or dependency while keeping the existing `Memory` boundary intact. Existing databases are upgraded with compatibility defaults for semantic user memory. The database stores only explicitly requested `MemoryItem` content and metadata; it does not store conversation history. `MemoryScope` is a trusted local ownership boundary, not authentication or a multi-user identity system.

Memory is opt-in: it only stores items passed to `store()`. It does not automatically save conversation messages or interact with `Context`. Retrieval and writing are separate operations.

### Semantic retrieval boundary

`stella.semantic_memory` defines a provider-neutral semantic path without
changing `Memory` or the current lexical retrieval path. `EmbeddingProvider`
turns text into a bounded vector, `SemanticIndex` owns scoped index/upsert/search
operations, and `SemanticRetriever` composes the two for bounded query
retrieval. The index is constructed with a trusted `MemoryScope`; it rejects
mismatched item scope, invalid `MemoryType`, invalid vectors, and
unbounded/invalid limits. Search filters results again by scope and optional
type as defense in depth.

Semantic results are `SemanticMatch` values containing only a memory item and
an index-provided score. They do not contain permissions, tool authority, or
approval state, and they are not connected to Brain control flow yet. The
existing `Memory` implementations remain the source of explicit storage,
lifecycle, lexical retrieval, and scope enforcement.

`LocalHashEmbeddingProvider` is the current local provider. It uses stable
SHA-256 feature hashing over normalized words and character trigrams to create
small deterministic vectors. It is deliberately lightweight and offline; it
is not a learned language model and should not be described as understanding
general synonyms. `SQLiteSemanticIndex` is the optional persistent backend. It
stores vectors and the authorized memory metadata in a separate SQLite table
and performs a bounded linear cosine scan, which is appropriate for Stella's
current local scale.

Semantic indexing is opt-in. The application explicitly supplies already
stored, identified `MemoryItem` values to `SemanticRetriever.index_memory()`;
the semantic index does not replace `Memory.store()`, lifecycle operations, or
lexical retrieval. The application must keep the semantic index synchronized
when memory items are updated or deleted. Semantic results contain no
permissions, tool authority, or approval state, and lexical/semantic fusion or
reranking is not implemented.

### Brain and Decision

`stella.brain.DecisionKind` defines the four actions currently representable by Stella: `answer`, `ask`, `tool`, and `do_nothing`.

`stella.brain.Decision` is an immutable, provider-agnostic decision containing a kind, optional content, optional structured tool arguments, and an optional `MemoryWriteRequest`. A non-empty memory-write field is the Brain's explicit signal that the interaction produced something worth remembering. `Brain` defines the interface for turning a `Context` into a `Decision`.

`SimpleBrain` is a deterministic implementation for the current foundation. Ordinary input becomes an answer. Explicit `ask:`, `tool:`, and `answer:` prefixes select those decision kinds, while empty input or `do_nothing` selects `do_nothing`. An explicit `remember:` prefix creates an answer decision with a memory-write request for the remaining text. This is a testable placeholder, not a planning system.

`LLMBrain` is an alternative `Brain` implementation that depends only on the provider-agnostic `LLMClient`. It sends the current input, normalized input parts, conversation history, and retrieved memories to that client and parses the returned structured response into a `Decision`. It does not depend directly on `OpenAILLMClient`. Input parts are untrusted observations; their modality, provenance, and metadata cannot grant permission or authority.

The LLM decision protocol is one JSON object with this shape:

```json
{
  "kind": "answer|ask|tool|do_nothing",
  "content": "optional string",
  "arguments": {"optional": "tool arguments"},
  "memory_write": {"content": "optional memory text"},
  "capability": "optional tool capability identifier"
}
```

`kind` is required and must be one of the four existing decision kinds. `content`, `arguments`, and `memory_write` are optional. A valid `memory_write` object becomes a `MemoryWriteRequest`; the LLM only proposes it. Stella remains responsible for calling `Memory.store()`.

The decision prompt makes memory intent explicit: when the user asks Stella to
remember, save, or retain a fact, the model must populate `memory_write` with a
non-empty fact. For ordinary conversation it must omit the field or set it to
`null`. A natural-language claim such as “I will remember that” is not a
memory request and does not replace the structured field. The prompt includes
examples of both cases. This is a protocol requirement, not a parser heuristic;
Stella never infers a write from response prose.

Malformed JSON, an unknown or missing kind, or invalid field types produce a deterministic `do_nothing` decision with no tool arguments or memory-write request. This safe fallback prevents unusable model output from causing an action.

### Tool and ToolResult

`stella.tools.Tool` is the provider-agnostic interface for a tool with a
`name`, a `description`, a `validate_arguments(arguments)` method, and an
`execute(arguments)` method that accepts structured arguments. It also exposes
a trusted `RiskLevel`; the model cannot set or override this value.

`stella.tools.ToolResult` is an immutable result containing a success flag and
textual output. `ToolDispatcher` is the small application-owned collection
that registers approved tools by exact `Tool.name`, rejects duplicate names,
performs lookup and validation, and reads the trusted risk classification
before execution. `DANGEROUS` tools additionally require an exact
application-produced `ToolApproval`; missing or mismatched approval fails
closed. The normal CLI currently registers `DateTimeTool`,
`SystemInfoTool`, `EchoTool`, the workspace-scoped `FileSystemReadTool`, and
the approval-required `FileSystemWriteTool` and `FileSystemDeleteTool`
together, and `NetworkReadTool`. It also registers the memory tools:
`memory_list` is `SENSITIVE`, while `memory_update` and `memory_forget` are
`DANGEROUS` and require exact application approval before they mutate stored
memory. Their outputs stay user-facing: remembered content without internal
database ids, an honest "No stored memories." when empty, a failure when no
memory matches, and a disclosed count when several memories matched an
ambiguous update query. The Phase 3 reminder tools `reminder_create`
(`DANGEROUS`), `reminder_list` (`SENSITIVE`), and `reminder_cancel`
(`DANGEROUS`) manage the user's own one-shot reminders through a trusted
`stella.reminders` store; see `REMINDERS.md`.

Tools are standalone abstractions. They are invoked by the orchestration layer
only after a `Brain` returns a tool decision. Stella delegates to the trusted
`ToolDispatcher`, which performs exact capability lookup, validates arguments,
evaluates trusted risk classification, checks exact application approval when
required, executes the selected tool, and returns a `ToolResult` for one final
natural-language response. The final call does not choose another action or
execute another tool. It also keeps a process-local `AuditRecord` for each
dispatch attempt. There is no plugin registry or dynamic discovery layer.

### Stella orchestration

`stella.stella.Stella` is the minimal coordinator. It receives a `Brain` (either `SimpleBrain` or `LLMBrain`), an `LLMClient`, one `Tool` or `ToolDispatcher`, and a `Memory` explicitly. Its `process(context)` method retrieves matching memories, gives them to the brain through a derived context, and returns a structured `StellaResult` that also exposes retrieved items and an ordered step trace. `max_tool_steps` defaults to one; a larger explicit bound enables a synchronous sequence of Brain/tool cycles.

The separate `stella.proactivity` module contains the focused one-shot
`DueTaskEvent` evaluator. `Stella.evaluate_due_task_event()` returns exactly
`INFORM`, `ASK`, or `DO_NOTHING` from the event and a trusted, narrowly scoped
`ProactivityDelegation`. It intentionally bypasses ordinary memory, the LLM,
the Brain, and the tool dispatcher: an event observation cannot grant
permission, and the result is not notification delivery or an action.

Phase 3 connects persisted reminders to that same evaluator rather than
adding a second decision system. `Stella.check_due_reminders()` turns each
due reminder in the trusted `stella.reminders` store into a `DueTaskEvent`
with an exactly-scoped delegation, delivers the resulting message only after
the store confirms the terminal `handled` transition (otherwise delivery is
withheld), and suppresses duplicates in-session and across restarts. The CLI
performs this check once per user interaction: there is no scheduler,
daemon, or heartbeat, and a due reminder grants no tool or action authority.

`Stella.handoff_due_task_event()` is the trusted application boundary around
that evaluator. The application constructs the event and supplies its stable
`event_id`; the LLM never establishes identity or trust. Stella keeps a
process-local set of handled IDs and returns an inspectable
`DO_NOTHING`/`duplicate_suppressed` result for a repeated ID. New IDs are
evaluated once with the separately supplied scoped delegation. This handoff
does not retrieve memory, call the Brain, execute tools, deliver notifications,
or persist event state, and ordinary memory cannot create delegation.

`Stella.present_due_task_event()` wraps that handoff in a
`UserFacingProactivityResult` for a trusted caller. `INFORM` and `ASK` retain
their bounded message for presentation; `DO_NOTHING`, including duplicates,
has no message. This is a synchronous result contract, not a notification
provider or delivery mechanism. The caller remains responsible for deciding
whether and how to display the message.

After the Brain returns, Stella performs at most one explicit memory write for the interaction. A write proposed by a decision grounded only in the user's own turn is stored directly; a proposal formed after tool observations may have been shaped by untrusted output, so it is stored only after a trusted approval provider approves the exact content, and it is never stored without a provider. A refused write is reported deterministically in the final response rather than left to the model's narration. For an ordinary `ANSWER` from `LLMBrain`, the Brain's structured `content` is required to be a complete final response and Stella returns it without a second LLM call. If that content is missing, Stella falls back to the dedicated final-response request. A tool-result answer still uses that dedicated request containing the current input, conversation history, retrieved memories, tool observations, and selected decision; the request explicitly tells the LLM to generate text for the already-selected answer rather than choose another action. For an ask decision, Stella returns the decision content and marks that more information is needed. For a tool decision, it passes the decision's structured arguments through the dispatcher. With a bound greater than one, the resulting structured `ToolObservation` is fed to the Brain for the next decision. Once the Brain selects `ANSWER` after tool observations, Stella makes one final response-generation call. A failed `ToolResult` is also represented as an observation; no retry is performed independently by Stella. If the bound is reached before another tool proposal can execute, Stella returns a deterministic limit result. For a do-nothing decision, it returns without calling the LLM or tool.

Each `Stella.process()` result also exposes an `InteractionTrace`. The trace is
an ordered, in-memory, provider-neutral record of the interaction boundaries:
bounded input metadata, memory-retrieval counts, redacted Brain decisions,
approval outcomes, redacted tool results, bounded action receipts (mutation,
verification status, and result size), the final-response summary, and the
memory-write outcome. It stores lengths, kinds, capability names, and argument
keys rather than raw user text, tool output, memory content, or argument
values. The trace is observational only: it cannot select decisions, grant
approval, execute tools, write memory, or replace the existing step trace and
tool audit.

### CLI

`stella.cli` is a thin terminal interface around the core `Stella` class. Its
entry point builds an `OpenAILLMClient`, `LLMBrain`, a `ToolDispatcher` holding
`DateTimeTool`, `SystemInfoTool`, `EchoTool`, `FileSystemReadTool`,
`FileSystemWriteTool`, `FileSystemDeleteTool`, and `NetworkReadTool`, and
`SQLiteMemory`, then
repeatedly creates a `Context` and calls
`Stella.process()`. The CLI keeps the session's `Message` history locally,
displays answer or tool output, and exits on `exit`, `quit`, or end-of-file. It
closes the SQLite memory connection when the session ends and does not
implement a second orchestration path.

For inspection during manual testing, the CLI accepts `--debug`. This prints the structured `Decision` returned by the Brain as JSON to stderr after each processed input. The normal user-facing output is unchanged when the flag is omitted, and the inspection output does not alter execution, tool calls, or memory writes.

Manual testing showed that a natural-language response claiming that Stella will remember something does not itself create a memory write. Only a parsed `Decision` containing an explicit `MemoryWriteRequest` causes Stella to call `Memory.store()`. The debug output exists to make that distinction visible.

## Component boundaries

- `LLMClient` knows how to request a model response; it does not own memory, decisions, tools, or personality.
- `OpenAILLMClient` contains OpenAI-specific SDK details; the rest of the architecture depends on `LLMClient` rather than on that provider.
- `Context` carries compatible text input plus bounded provider-neutral input
  parts, conversation history, retrieved memories, and structured tool
  observations; it does not fetch or persist information.
- `TranscriptionProvider` converts one bounded audio observation to text; it
  does not choose decisions, authorize tools, or write memory.
- `VisionProvider` converts one bounded image observation to text; it does not
  choose decisions, authorize tools, or write memory.
- `SpeechProvider` renders one bounded final response as speech; it does not
  receive or grant decisions, permissions, memory, or tool authority.
- `VideoProvider` analyzes one bounded, explicitly supplied clip reference; it
  does not capture media, retain raw media, make decisions, or grant authority.
- `Memory` stores only explicitly supplied `MemoryItem` objects; `InMemoryMemory` keeps them for one process and `SQLiteMemory` persists them at a configured path. Neither automatically captures conversations, and retrieval does not imply a write.
- `Brain` decides what kind of action is appropriate and may explicitly attach a `MemoryWriteRequest`; `SimpleBrain` does this deterministically and `LLMBrain` parses an LLM proposal. A Brain does not execute tools or write memory itself.
- `Tool` describes and executes one bounded operation; it is not exposed to arbitrary model-generated code or shell commands.
- `ToolDispatcher` owns exact capability lookup, tool validation, trusted risk
  classification, and action-specific approval checks. Approval is not
  derived from LLM output.
- `Stella` retrieves memories before asking the brain to decide, performs at most one explicit requested memory write, and coordinates a bounded synchronous sequence of decisions and tool executions; it does not create decisions, persist tool traces, or run background loops.
- `CLI` collects terminal input and displays results; it does not make decisions, call providers directly for conversation handling, or bypass `Stella`.

Memory participates in both sides of the response path, but only explicitly. Stella reads matching items before decision-making and writes one item only when the returned decision contains a `MemoryWriteRequest`. Ordinary conversation and retrieved memories are not written automatically.

## Intended flow

The implemented flow constructs or receives a `Context`, and `Stella` performs these steps:

1. Stella asks the configured `Memory` for items matching the current user input. The CLI configures this as `SQLiteMemory`; tests commonly use `InMemoryMemory` or temporary SQLite databases.
2. Stella creates a derived `Context` containing those retrieved memories and passes it to the configured Brain. With `SimpleBrain`, the decision is local and deterministic; with `LLMBrain`, the Brain sends the context to `LLMClient` and parses the JSON protocol.
3. If the decision contains a `MemoryWriteRequest`, Stella calls `Memory.store()` exactly once and includes a `MemoryWriteResult` in the result.
4. An `answer` decision from `LLMBrain` normally returns its required
   non-empty `content` directly. If that content is missing, or if the answer
   follows tool observations, Stella sends the current input, conversation
   history, retrieved memories, observations, and selected decision to the
   configured `LLMClient` for final response text. That second call does not
   re-decide the action.
5. An `ask` decision returns a structured result indicating that more information is needed.
6. A `tool` decision is passed to the `ToolDispatcher`, which requires an
   exact registered capability, validates arguments with that tool, evaluates
   the trusted risk classification, checks exact application approval when
   required, executes it, and sends the `ToolResult`
   to the LLM for final response text. `filesystem_read` is classified as
   `SENSITIVE`, remains workspace-scoped, and does not require interactive
   approval. `filesystem_write` is classified as `DANGEROUS`, is create-only,
   and requires an exact application approval before execution.
   `filesystem_edit` is classified as `DANGEROUS`, replaces the full content
   of one existing regular workspace file, and requires an exact application
   approval before execution.
   `filesystem_delete` is classified as `DANGEROUS`, only deletes one
   existing regular workspace file, and requires an exact application
   approval before execution.
   Each filesystem mutation then verifies the resulting state with trusted
   application code (create/edit re-read the expected bytes, delete confirms
   absence) and returns a bounded `ActionReceipt`; unverified or inconclusive
   outcomes are reported as failures, never as success.
   `network_read` is classified as `DANGEROUS`, accepts one public HTTPS
   `text/plain` URL without credentials, query strings, or fragments, and
   requires exact application approval before it connects.
   The trusted dispatcher records the capability, validated arguments, risk,
   approval outcome, execution outcome, and UTC timestamp for this attempt.
   Unexpected tool exceptions become a failed result with the output `Tool execution failed.`.
7. A `do_nothing` decision returns without calling the LLM or tool.

When `max_tool_steps` is greater than one, a tool result is added as a
structured observation and steps 2, 5, and 6 repeat synchronously until the
Brain selects a non-tool decision or the trusted step bound is reached. A
proposal after the bound returns the deterministic limit result without
execution or another final LLM call. The default bound of one preserves the
single-tool flow above.

For a normal terminal session, the CLI repeats this flow synchronously. It adds each user message and displayed response to the next context's conversation history. Typing `exit` or `quit` stops the session without sending that command through Stella.

This is a direct, bounded synchronous coordinator. It does not run in the
background, learn, or turn a natural-language response into another action.
The important design decision is that retrieval and writing are both explicit:
Stella chooses the current-input query, while the Brain's non-empty write
request is the only cause of a memory write; an observation-grounded proposal
additionally executes only under exact approval-provider consent. No
conversation, response, or retrieved memory is stored by default.

The cross-instance persistence behavior is covered by an integration test: one Stella/SQLiteMemory instance explicitly writes a fact, both instances are closed or discarded, and a second Stella/SQLiteMemory instance retrieves that fact. A deterministic Brain changes its decision to `answer` when the retrieved memory is present, proving that SQLite memory affects behavior rather than merely retaining rows.

`NetworkReadTool` is a bounded external-data capability. It performs one
fixed HTTPS GET with no redirects, credentials, cookies, model-supplied
headers, or ambient proxy configuration. Trusted code rejects local,
private, link-local, reserved, multicast, and other non-global destinations,
pins the connection to a validated public address, limits the response to
1 MiB of UTF-8 `text/plain`, and returns fetched content as untrusted data.
The content is not automatically stored or treated as an instruction.

## Intentionally not implemented yet

The following are intentionally outside the current MVP foundation:

- Unbounded LLM-based planning or multi-step decision-making
- Automatic context assembly, summarization, or retrieval
- Automatic memory capture, learning, or memory writes without an explicit Brain request
- File-backed memory formats other than the minimal SQLite table, embeddings, vector databases, or external persistence services
- Memory migrations, ranking, semantic search, and multi-user data management
- Plugin registries, permissions, external APIs, subprocesses, or shell execution
- Personality, generalized event ingress, background notification delivery,
  autonomous loops, schedulers, daemons, or heartbeats — reminder checks
  happen only during a real user interaction (see `REMINDERS.md`)
- Audio capture and output, transcription and vision providers beyond their
  interfaces; video or environment processing; interface rendering; streaming;
  or
  provider-specific multimodal integrations
- Plugin loading, dynamic discovery, permission checks, and approval UI
- Filesystem operations beyond the current narrow workspace tools, shell
  execution, and unrestricted network access

## Project structure

```text
.
├── README.md
├── pyproject.toml
├── uv.lock
├── src/
│   └── stella/
│       ├── __init__.py
│       ├── brain.py
│       ├── cli.py
│       ├── context.py
│       ├── llm.py
│       ├── memory.py
│       ├── openai_client.py
│       ├── stella.py
│       └── tools.py
└── tests/
    ├── test_brain.py
    ├── test_cli.py
    ├── test_context.py
    ├── test_llm.py
    ├── test_memory.py
    ├── test_openai_client.py
    ├── test_stella.py
    ├── test_package.py
    └── test_tools.py
```

The project uses Python with uv. Runtime code lives under `src/stella`, and the independent behavior tests live under `tests`.

## Running Stella locally

Install the environment with uv, set the required environment variables, and run the console entry point:

```bash
uv sync
export OPENAI_API_KEY
export STELLA_MODEL
# Optional; defaults to ./stella_memory.db
export STELLA_MEMORY_DB
# Optional; defaults to ./stella_reminders.db
export STELLA_REMINDERS_DB
# Optional; defaults to ./stella_workspace
export STELLA_WORKSPACE
uv run stella
```

Add `--debug` when inspecting Brain decisions:

```bash
uv run stella --debug
```

`OPENAI_API_KEY` is required by the OpenAI client. `STELLA_MODEL` selects the model passed to `OpenAILLMClient`. `OPENAI_BASE_URL` may also be set when a non-default OpenAI-compatible endpoint is needed. `STELLA_MEMORY_DB` selects the SQLite database path; if it is unset, the CLI uses `stella_memory.db` in the current working directory. `STELLA_WORKSPACE` selects the directory available to the workspace-scoped `filesystem_read`, `filesystem_write`, `filesystem_edit`, and `filesystem_delete` capabilities; if unset, the CLI uses `./stella_workspace`. The CLI reads these variable names but does not contain or expose secret values.

## Current verification status

At the time this document was written:

- Pytest: 217 tests passing
- Ruff: all checks passing

The OpenAI client tests mock the SDK, so the test suite does not make real API calls.

This document should be updated when a meaningful architectural component is added or an important design boundary changes.
