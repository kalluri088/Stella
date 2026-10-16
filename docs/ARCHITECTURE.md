# Stella Architecture

## What Stella is becoming

Stella is built as a small, understandable personal assistant. The v1 product is a local-first desktop assistant: conversation, user-controlled memory, workspace file actions with independent verification, and optional one-utterance voice — all behind the trusted approval boundary. The priority is to keep a few clear, provider-agnostic boundaries rather than add autonomous behavior. Each component is intentionally small and can be tested independently.

## Current MVP philosophy

The same explicit data structures, narrow interfaces, deterministic behavior, and ordinary Python over framework-heavy abstractions carry through the whole product. Components have one clear responsibility. In particular, the application does not hide persistence, retrieval, planning, tool execution, or conversation management behind implicit behavior.

## Current architecture

### LLMClient and Message

`stella.llm.Message` is a small immutable representation of a conversation message. It contains a `role` and `content`. The LLM layer also continues to accept normal message dictionaries, so existing conversation messages can be passed without converting them first.

`stella.llm.LLMClient` is the provider-agnostic interface for sending a list of messages to a language model and receiving a string response. `FakeLLMClient` provides a deterministic response for tests.

The abstraction does not decide how context is assembled, store conversations, or choose a model.

### OpenAI client

`stella.openai_client.OpenAILLMClient` is the current concrete `LLMClient`
implementation. It uses the official OpenAI Python SDK and takes the API key,
model, optional base URL and a `tool_dialect` through its constructor — the
client itself reads no environment variable; resolving the key to pass in is
the application's job (see `stella.provider_keys` below). `Message` objects are
converted to the dictionary shape expected by the SDK; existing dictionaries
are passed through.

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

Only OpenAI serves the Responses API, so the constructor's `tool_dialect`
takes on the other choice: presets whose endpoints are OpenAI-compatible but
not OpenAI (Claude, Grok, Groq, OpenRouter, Gemini, custom) speak the
`chat` dialect, and their `chat_with_tools()` goes through Chat Completions
function tools instead, normalizing `choices[0].message.tool_calls` into the
same provider-neutral `LLMToolCall` values. The Brain cannot tell the two
dialects apart; the conformance suite tests both as equivalent
implementations of the same boundary.

Clients without native tool calling inherit a compatibility implementation that
uses the existing text/JSON protocol through `chat()`. This preserves the
fallback for simple and test clients without making the Brain depend on the
OpenAI SDK. Native calls improve selection reliability, but they do not grant
authority: only `ToolDispatcher` can look up, validate, approve, and execute a
capability.

One shape of native reply is not a capability name. A hosted candidate on the
`chat` dialect sometimes proposes a call whose function name is a generic
`tool` with the decision envelope inside its arguments —
`tool:{"capability": "workspace_list", "arguments": {…}}` — which is the envelope
`_recover_text_channel_tool_call` has tolerated on the *text* channel since the
1.0.0 hardening, arriving on the other one.
`LLMBrain._decision_from_tool_call` reads the name out of it when the call's own
name is not registered and the name inside it is, because taking the wrapper
literally dispatched nothing and the dispatcher's rejection — "Tool capability
unavailable.", a sentence about a *name* — came back to the owner as "that
capability does not exist on this machine" (2 of 20 turns that could have been
answered, #278). The inner name is then authorized by the same lookup, tier and
approval as any proposal; a call whose own name *is* registered keeps it, so
nested model text cannot re-point a real call. A wrapper that names no capability
at all (`tool:{"path": "notes.txt"}`) is still refused, and the refusal is now
built by `capability_not_registered_output()`, which quotes the proposed name back
and states that nothing ran rather than that anything is unavailable — the same
sentence no longer reaches the owner as a fact about her machine. See
`TOOL_EXECUTION.md`.

`stella.brain.ToolUsePolicy` supplies the application-level distinction that
native `auto` alone cannot provide. It deterministically returns
provider-neutral `AUTO` for ordinary requests and `REQUIRED` only when a
request clearly asks for live, local, external, workspace, or explicit
inspection/tool information and at least one available tool is relevant by
description. It never selects a capability or supplies arguments. After a
successful tool observation, the next Brain pass returns to `AUTO` so the
model can answer from that observation. If `REQUIRED` produces no native or
textual tool decision, `LLMBrain` asks once more with the demand stated in the
request — that reply carried no tool call, conversation text is not a fresh
observation, answer with `kind=tool` naming the capability — because an
identical retry re-rolls the same answer while a named demand was measured to
recover every refusal it was tried on (#269). If even that comes back an
answer, the turn clarifies with words that say nothing was checked, rather than
promising an inspection no step performs. Tool execution and
all security decisions remain exclusively in `ToolDispatcher`: the re-ask asks
the model to name a capability, and the named call is authorized, validated and
tiered exactly as a first-reply call would be. Either way the re-ask is one
request, never a loop, and the guard only fires while no observation exists.

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

`Context` also carries `dropped_proposals`: the capabilities a previous reply
asked for that this runtime did not dispatch, because one step runs one
capability. It is a report, not an observation — nothing executed, so there is
no success and no output to carry — and the runtime never runs a proposal on
the model's behalf. See `TOOL_EXECUTION.md`, *One step, one capability*.

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

`stella.voice` provides the concrete desktop hardware boundaries: a
`Recorder`/`Player` pair backed by local subprocesses (`pw-record`/`arecord`
and `pw-play`/`paplay`/`aplay`), plus `CommandTranscriptionProvider` (a local
command template, no shell), `OpenAITranscriptionProvider`, and
`CommandSpeechProvider`/`OpenAISpeechProvider` implementations of the existing
provider interfaces. The desktop UI's voice mode records one explicit
utterance per Listen press, transcribes it, and sends the transcript through
the same `StellaSession.run_turn()` path as typed input; the recording is
deleted right after transcription and never enters history. See `VOICE.md`.

Which device those captures read is a separate concern, owned by
`stella.capture_target`. `barge_in.capture_command(source)` has always named a
`--target` only when given one, and `mic_tap` stays deliberately
device-ignorant, so the deciding was nowhere: an unnamed capture takes
PipeWire's default source, which on a laptop with a Bluetooth headset is the
headset's own microphone, and holding it costs that headset stereo and full
volume (`VOICE.md`). `capture_target` asks PipeWire afresh every time a capture
opens and names the highest-ranked non-Bluetooth input whenever this machine has
one — a machine whose only microphone is the headset keeps it, and so does every
doubtful answer, including no PipeWire and a probe that stalls, because a probe
must never become a reason voice stops working. Listeners are handed a function
that builds the command line (`barge_in.live_capture_command`) rather than the
command line itself: a resident server lives for hours and its microphones do
not. The push-to-talk `Recorder` is untouched: a capture that lives
as long as one utterance does not pin a device across a whole session. The
owner's chosen node is a saved setting the panel shows, and a node name is
hardware identity rather than a secret.

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

In the desktop UI, speech output is an opt-in checkbox (off by default). When
enabled, the bridge renders only the final response text through a
`SpeechProvider`, plays the artifact on a separate daemon thread, and removes
it afterwards. "Stop speaking" cancels the audio subprocess only; the
understood decision, history entry, and displayed text are untouched, and a
synthesis or playback failure is reported honestly while the text response
remains available.

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

SQLite is used at this stage because it provides local persistence with no separate service or dependency while keeping the existing `Memory` boundary intact. Existing databases are upgraded with compatibility defaults for semantic user memory. The database stores only explicitly requested `MemoryItem` content and metadata; it does not store conversation history. `MemoryScope` is a trusted local ownership boundary, not authentication or a multi-user identity system. One consequence for any future multi-user work: rows written before a scope existed have no factual owner — legacy databases must keep them in an explicit unassigned scope rather than have ownership guessed or defaulted to the first authenticated user.

Memory is opt-in: it only stores items passed to `store()`. It does not automatically save conversation messages or interact with `Context`. Retrieval and writing are separate operations.

### Semantic retrieval boundary

`stella.semantic_memory` defines a provider-neutral semantic path without
changing `Memory` or the lexical retrieval path. `EmbeddingProvider`
turns text into a bounded vector, `SemanticIndex` owns scoped
index/upsert/search operations plus a scope-bounded `clear()`, and
`SemanticRetriever` composes the two for bounded query
retrieval. The index is constructed with a trusted `MemoryScope`; it rejects
mismatched item scope, invalid `MemoryType`, invalid vectors, and
unbounded/invalid limits. Search filters results again by scope and optional
type as defense in depth.

Semantic results are `SemanticMatch` values containing only a memory item and
an index-provided score. They contain no permissions, tool authority, or
approval state; they only enrich what the Brain is shown, never what it may
do. The existing `Memory` implementations remain the source of explicit
storage, lifecycle, lexical retrieval, and scope enforcement.

The default provider is `LocalHashEmbeddingProvider`. It uses stable
SHA-256 feature hashing over normalized words and character trigrams to create
small deterministic vectors. It is deliberately lightweight, offline and
dependency-free; it is not a learned language model and should not be described
as understanding general synonyms. Two real-model providers can be selected
instead (`STELLA_SEMANTIC_PROVIDER` or the Settings choice), and neither is ever
auto-activated: `ollama` embeds through a local Ollama server
(`OllamaEmbeddingProvider`, stdlib HTTP only, no new dependencies) and applies
nomic's `search_document:`/`search_query:` prefixes client-side, because Ollama
passes the input verbatim; `minilm` runs `all-MiniLM-L6-v2` on CPU via
sentence-transformers, installed only as the optional extra `stella[embed]` and
loaded lazily on first embed. Every provider labels itself (`method`) and
every index row stores that label plus the vector dimension; search computes
cosine only against rows from the same provider and dimension, so vectors from
different models are never mixed in one comparison. A provider that cannot
reach its backend returns no vector, which is reported honestly
(`SemanticSearchUnavailableEvent` in the trace) and degrades the turn to plain
keyword recall — never to fabricated similarity.
`SQLiteSemanticIndex` is the optional persistent backend. It
stores vectors and the authorized memory metadata in a separate SQLite table
and performs a bounded linear cosine scan, which is appropriate for Stella's
current local scale.

Semantic recall is on by default — the owner's decision of 2026-10-14,
reversible from the Settings checkbox or for one launch with
`STELLA_SEMANTIC_MEMORY=0`; an install that says no never creates the index
file. When enabled,
`Stella.process` performs fused, keyword-dominant recall: lexical matches
keep their exact order and retrieval slot priority, and at most two semantic
supplements (`MAX_SEMANTIC_SUPPLEMENT`) may fill the remaining space. Stella
enforces the cap itself rather than trusting the backend, and items already
retrieved lexically are never duplicated. The two score scales are never
compared with each other. Instead every retrieved memory carries reported
provenance (`RetrievalSource`: `keyword` with the lexical score, or the active
provider's label — `local-hash-embedding`, `ollama-embedding` or
`minilm-embedding` — with the rounded cosine score), and the Brain payload
surfaces it so the model can describe a semantic hit at most as "this may be
related" — never as understanding.

Index synchronization uses a full rebuild (`reconcile_semantic_index`): clear
the scope, then re-embed every memory item in it. A rebuild was chosen over
per-operation hooks because `Memory.store()` reports no new id and trusted
write paths are many (memory tools, memory-write proposals, the UI panel);
reconciliation is idempotent, heals updates and deletes at once, and also
heals changes made while Stella was down (one reconcile at startup). Stella
reconciles after every successful memory mutation in a turn. Failures are
reported honestly — a `MemoryIndexSyncEvent(ok=False)` in the trace plus a
plain note on the response — and never change a memory write's own outcome,
because the memory store remains the ground truth. Reconcile is also the
provider-change migration: switching the embedding provider rewrites every row
with the new label and dimension, and rows from another model are skipped by
search until that rebuild heals them.

### Brain and Decision

`stella.brain.DecisionKind` defines the four actions currently representable by Stella: `answer`, `ask`, `tool`, and `do_nothing`.

`stella.brain.Decision` is an immutable, provider-agnostic decision containing a kind, optional content, optional structured tool arguments, and an optional `MemoryWriteRequest`. A non-empty memory-write field is the Brain's explicit signal that the interaction produced something worth remembering. `Brain` defines the interface for turning a `Context` into a `Decision`.

`SimpleBrain` is a deterministic implementation for the current foundation. Ordinary input becomes an answer. Explicit `ask:`, `tool:`, and `answer:` prefixes select those decision kinds, while empty input or `do_nothing` selects `do_nothing`. An explicit `remember:` prefix creates an answer decision with a memory-write request for the remaining text. This is a testable placeholder, not a planning system.

`LLMBrain` is an alternative `Brain` implementation that depends only on the provider-agnostic `LLMClient`. It sends the current input, normalized input parts, conversation history, and retrieved memories to that client and parses the returned structured response into a `Decision`. It does not depend directly on `OpenAILLMClient`. Input parts are untrusted observations; their modality, provenance, and metadata cannot grant permission or authority.

The capability registry reaches the model exactly once per turn. A client
that puts the registry on the wire itself — every shipping provider client,
which is what `LLMClient.declares_tools_natively` records — is handed the
definitions through `chat_with_tools` and no prose copy in the system
prompt. A client that cannot carry them keeps the in-prompt
"Currently available tools:" list, because for it the text protocol is the
only source of capability names. `LLMBrain` asks the client, never guesses
from its class. This replaced the pre-#229 arrangement where `describe()`
output was rendered into the prompt *and* sent natively, which cost 1,198
tokens a turn (2,172 with the OS, shell and browser families registered)
for no additional information and was what pushed a recall turn past
`num_ctx=8192`. See `MODELS.md` (*The stable part of the prompt*, *Token
accounting*) for the numbers and the routing A/B that justified shipping
it.

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

The prompt also fixes the *shape* of what is stored: a memory is one short
third-person sentence, roughly fifteen words, that keeps every durable detail
(names, numbers, dates, places, choices, negations) and drops the wording
around it. Verbatim retention is reserved for the cases where the wording *is*
the fact — a name, a quote the user asked Stella to keep, an exact number or
URL. Storing the user's whole request was the failure this rule removes. The
runtime still stores exactly the string the proposal carries: compaction is the
model's job under the prompt contract, never a runtime rewrite of user text.

Malformed JSON, an unknown or missing kind, or invalid field types produce a deterministic `do_nothing` decision with no tool arguments or memory-write request. This safe fallback prevents unusable model output from causing an action.

That fallback is not the same event as rule 9, and it no longer pretends to be. `Decision.unreadable` marks the difference: `{"kind":"do_nothing"}` arrives with the flag false, because Stella chose silence, while a reply that failed to parse — prose with no decision object, a kind the runtime does not know, a field of the wrong type, or an empty completion — arrives with it true. The loop still grants the unreadable reply nothing at all, because failing closed is the safety property and the flag carries no capability, no arguments and no memory write. What changes is what the user hears. A turn whose decision could not be read now ends with the runtime's own sentence, "I couldn't make sense of that answer. Try asking again.", instead of ending with no words after a long wait; the model's prose is still never spoken, since the sentence is a literal in the runtime and the fallback's `content` is `None`. Two cases keep the silence they had: a genuinely chosen `do_nothing`, and an unreadable reply that followed a tool step which had already produced output, because garbling the summary of a read is no reason to hide the read.

Before that sentence is spent, the brain is asked once more. `LLMBrain.decide()` makes at most two provider requests per decision — for either lapse, never both: the check the runtime demanded and the reply answered past (#269), or the reply that carried no decision object at all (#276). Each re-ask appends one application-authored runtime note to the user turn and never the unreadable reply itself, so nothing the model wrote unsanctioned re-enters the request being repaired. There is no third request: a turn's budget is for work, not for retrying a brain that has decided. The second reply is read by the same parser and authorised by the same runtime, so a retry that also fails leaves the turn exactly where #244 put it — the runtime's sentence, no capability and no memory write. What the retry is for is measurable: on the router she runs, ~7 % of decision requests (11 of 160) carry no decision object, every one of them the correct answer with the envelope dropped rather than a wrong or truncated reply, and all eleven drawn from just two of the pool's eighteen candidates. A second request read all eight conditioned retries back. That 7 % is the rate on one repeated trivial request; on six production shapes across 40 decisions the pool read the format every time (#277), so the exposure is bounded to the request shape that triggers it rather than to every turn. Rule 9 still bounds the total, because a re-ask lives inside the one decision it replaces.

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
closed. The normal CLI registers this core of capabilities, given by name
rather than count so the doc cannot drift: the safe
`DateTimeTool` and `SystemInfoTool`, the workspace-scoped filesystem and
workspace tools (`filesystem_read` is `SENSITIVE`; `filesystem_write`,
`filesystem_edit` and `filesystem_delete` are `DANGEROUS` with exact approval),
`NetworkReadTool` (`DANGEROUS`), the memory tools (`memory_list` and, since
#246, `memory_write` are `SENSITIVE` — a fact the owner has just said is
local, reversible and hers, so it stores without a prompt, while a write
proposed after tool observations is still put to the owner by the runtime's
provenance guard; `memory_update` and `memory_forget` are `DANGEROUS` and
require exact application approval before they change or drop stored
memory), and `PersonaEditTool` (`DANGEROUS`, limited to the two
persona files; see `PERSONA.md`). The opt-in Outline, web and desktop tools
below add to this core only when their gates are open. `EchoTool` exists for tests but is
deliberately unregistered: an echo capability lets a confused model "succeed"
by parroting the user. Their outputs stay user-facing: remembered content without internal
database ids, an honest "No stored memories." when empty, a failure when no
memory matches, and a disclosed count when several memories matched an
ambiguous update query. Stella keeps no reminder store of its own: a
"remind me" is scheduled in the connected Outline workspace, which owns the
alert; Stella only reads that workspace's due list back to surface it. See
`REMINDERS.md`.

Tools are standalone abstractions. They are invoked by the orchestration layer
only after a `Brain` returns a tool decision. Stella delegates to the trusted
`ToolDispatcher`, which performs exact capability lookup, validates arguments,
evaluates trusted risk classification, checks exact application approval when
required, executes the selected tool, and returns a `ToolResult` for one final
natural-language response. The final call does not choose another action or
execute another tool. It also records an `AuditRecord` for each dispatch
attempt in a bounded action history (`stella.history`): process-local by
default, SQLite-backed in the desktop application so it survives restarts.
There is no plugin registry or dynamic discovery layer. #229 was closed
without one: the registry was being sent twice, and the fix was to send it
once, not to build machinery that decides when to send it.

### Stella orchestration

`stella.stella.Stella` is the minimal coordinator. It receives a `Brain` (either `SimpleBrain` or `LLMBrain`), an `LLMClient`, one `Tool` or `ToolDispatcher`, and a `Memory` explicitly. Its `process(context)` method retrieves matching memories, gives them to the brain through a derived context, and returns a structured `StellaResult` that also exposes retrieved items and an ordered step trace. `max_tool_steps` defaults to one; a larger explicit bound enables a synchronous sequence of Brain/tool cycles. The application composes both turn budgets — `max_tool_steps=settings.max_tool_calls` and `turn_time_limit_seconds=settings.turn_time_limit_seconds` — so the numbers the owner sets in the window are the numbers the loop enforces. See *Turn budgets* below.

The separate `stella.proactivity` module contains the focused one-shot
`DueTaskEvent` evaluator. `Stella.evaluate_due_task_event()` returns exactly
`INFORM`, `ASK`, or `DO_NOTHING` from the event and a trusted, narrowly scoped
`ProactivityDelegation`. It intentionally bypasses ordinary memory, the LLM,
the Brain, and the tool dispatcher: an event observation cannot grant
permission, and the result is not notification delivery or an action.

The evaluator's only remaining producer is the connected workspace: an
Outline result that reports overdue items is read during a turn and handed
through this same surface. The reminder adapter is gone — `Stella` has no
due store, no reminder table and no reminder of its own — so the only scheduled
alert it can act on is one Outline already declared due. The layer stays
generic rather than deleted because it carries rule 8: proactivity may increase
awareness, never authority, and an event observation grants no tool or action
permission.

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

### The two-tier event bus

`stella.event_bus` (built to the measurements of research reports 02
and 12) routes things that happened — an `Event` with a source, text
and structured fields — against at most ten registered `Intention`
records, and its only output is a `DispatchResult`: `Match` records
with a human-readable reason, per-intention raw answers, and an honest
tier status (`ran`, `skipped`, `absent`, `unavailable`). It never
delivers, executes, approves or persists anything; acting on a match
stays each consumer's own approved decision path, exactly as with
proactivity results.

Tier 0 is deterministic `Tier0Rule` matching (`TextMatches`,
`FieldIs`, `FieldMatches`) — free, auditable, and validated at
registration. A rule hit gates Tier 1 for that intention on that
event, so an already-explained match is never paid for twice. Tier 1
asks the remaining intentions' typed laya questions in one batched
call per dispatch, and only accepts questions in the shipped
`laya.presets` shape. Because the checkpoint ships uncalibrated
confidences, a `noul` probability fires an intention only when that
intention explicitly registered a threshold — otherwise it is
advisory data inside the result — while a choice question fires on
the returned label, a deterministic reading of the answer.

The judge (`stella.laya_judge`) keeps laya outside Stella's own
environment: it spawns the standalone `stella.laya_runner.py` under a
separate venv interpreter and speaks line-delimited JSON. A hung,
crashed, exiting or refusing runner is killed and reported as
`TierOneUnavailable` — "could not ask" never reads as "no match" —
and is restarted lazily on the next question. Routing death affects
routing only, following the voice-degradation precedent.

After the Brain returns, Stella performs at most one explicit memory write for the interaction. A write proposed by a decision grounded only in the user's own turn is stored directly; a proposal formed after tool observations may have been shaped by untrusted output, so it is stored only after a trusted approval provider approves the exact content, and it is never stored without a provider. A refused write is reported deterministically in the final response rather than left to the model's narration. For an ordinary `ANSWER` from `LLMBrain`, the Brain's structured `content` is required to be a complete final response and Stella returns it without a second LLM call. If that content is missing, Stella falls back to the dedicated final-response request. A tool-result answer still uses that dedicated request containing the current input, conversation history, retrieved memories, tool observations, and selected decision; the request explicitly tells the LLM to generate text for the already-selected answer rather than choose another action. For an ask decision, Stella returns the decision content and marks that more information is needed. For a tool decision, it passes the decision's structured arguments through the dispatcher. With a bound greater than one, the resulting structured `ToolObservation` is fed to the Brain for the next decision. Once the Brain selects `ANSWER` after tool observations, Stella makes one final response-generation call; a first tool call the Brain marked `tool_final` skips that re-decision entirely (see the terminal-tool fast path below). A failed `ToolResult` is also represented as an observation; no retry is performed independently by Stella. If the bound is reached before another tool proposal can execute, Stella returns a deterministic limit result. For a do-nothing decision, it returns without calling the LLM or tool.

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

### Outline app tools

`stella.outline_tools` adds four opt-in capabilities for the Outline task
app (`~/Projects/Outline`): `outline_search` (SAFE), `outline_create`,
`outline_update` and `outline_bulk` (all SENSITIVE, exact-argument approval).
They speak the server's loopback HTTP API with stdlib `urllib` only — no new
dependency, no direct database access; the token comes from `OUTLINE_TOKEN` or
the server's own data directory. Registration follows the desktop-tools rule:
the `STELLA_OUTLINE` flag *and* a live `/healthz` probe (quarter-second
timeout) must both answer, so a stopped Outline server means the model
never sees capabilities that could only fail. No delete is exposed. What
comes back from Outline is framed as stored data, never instructions, and
bounded (`MAX_OUTPUT_LINES`).

Because that gate is silent by design, `probe_outline` reports what it decided
and why, on the same precedence, and is read by `stella doctor` and `/status` —
never the token, only where it was looked for. `docs/OUTLINE.md` is the family
document.

The four capabilities stay four; they grow as `kind`/`action` values:
task `edit`/`reschedule` (with recurrence and tags on create), event
`reschedule`/`edit`, note and project `edit`, person-link `attach`/`detach`,
tag-filtered and `kind=person` search, `kind=graph` — a text rendering of
anyone's connections neighborhood — and `remind` (an ISO-8601 alert time
on task/event create, and an edit field where `null` clears it). Stella
mirrors Outline's compact recurrence/tag token validators (source of truth:
`outline_server/api/__init__.py`), rejecting malformed values before any
request leaves. Since Stella's own reminder store was dropped, this field
is the only alert Stella can arrange: a plain "remind me" becomes an Outline
task or event carrying that time (`docs/REMINDERS.md`, report 30 as moved by
the drop). The API-first parity rule holds with two documented
exceptions: bulk export is UI-only, and the force-graph *layout* is
rendering-only (the connections it shows are full parity).

### Web capability

`stella.web_tools` adds the two Stage-E capabilities `web_search` and
`web_fetch` (see `docs/WEB.md`), registered only under the opt-in
`STELLA_WEB` flag. Both are DANGEROUS — every call is egress a human
approves, and the approval wording names the receiving third party. The
backend is decided per call: TinyFish when `TINYFISH_API_KEY` is set,
otherwise the optional `ddgs` package and a direct https fetch that
reuses `network_read`'s pinned-DNS, peer-validated machinery; with
neither, the tools answer a structured "web is off". A runtime-owned
`WebBudget` (500 searches/hour, 1000 URLs/day) enforces the free-tier
limits the model can see but never ration, and all returned text comes
back size-bounded inside `<<<UNTRUSTED_WEB_CONTENT>>>` markers, so web
content is information only (rules 3, 6, 10).

### Desktop tools

`stella.desktop` (the `src/stella/desktop/` package — formerly a single
`os_tools.py`) adds four capabilities behind the `os_tools_enabled` flag:
`screen_read` (grim capture piped to local `tesseract --psm` OCR),
`window_focus`, `key_send` (wtype into a focus-verified window), and
`desktop_control` — one approval-gated tool whose *arguments* carry the app
and window surface the owner asked for: `target=app action=launch` (with an
optional workspace), and `target=window` with `action=close|move|focus`.
It never grows new verbs (rule 16), and it is offered only when the probed
adapter actually provides a window manager: on Hyprland, launches go through
`hl.exec_cmd`, closes and moves through the same verified Lua dispatch path
as focus, and each act is confirmed by a re-query — a launch is verified
when a *new* window appears (and is then moved silently if a workspace was
named), a close when the id is gone, a move when the client's workspace
matches. A backend that cannot manage windows simply does not register the
tool. A small
backend registry probes for a session in order (Hyprland → sway → KDE → GNOME
→ X11/EWMH → Wayland screencopy) and returns these tools only when a
complete adapter is found; otherwise the model sees nothing. The package
encodes the report-03/11/13 surface rules — and, where the live
0.56.2/Omarchy build disagreed with them, the measured truth instead: the
instance flag is `-i` (not `-r`), focus dispatches through the compositor's
Lua API (`hl.dispatch(hl.dsp.focus{window=…})` via `hyprctl eval`), and
grim takes `X,Y WxH` written to stdout with a trailing `-`. Backend status:
Hyprland is fully implemented and measured on this machine; sway and
X11/EWMH are written but unverified on a live session and do not vouch for
window management, so on those desktops `desktop_control` is simply not
offered; KDE, GNOME and the Wayland screencopy adapter are still stubs
(`probe` returns `None`).

Two invariants hold the trust model. No decision reads a subprocess return
code: reads require parseable JSON of the expected shape (the compositor
answers "unknown request" at rc 0), and a failed shape is an error, never an
empty list that would read as "no windows". Every act is followed by a fresh
compositor re-query, so `window_focus` reports *verified* only when
`activewindow` agrees and `key_send` reports *inconclusive* — the keystrokes
reached a focus-confirmed window, but the application's reaction is
unknowable from the compositor. `screen_read` bounds its OCR text
(`MAX_SCREEN_TEXT_CHARS`) and masks obvious credentials before the model
sees them, and pixels never persist. Registration is doubly gated — an
explicit settings flag (`os_tools_enabled`, env override `STELLA_OS_TOOLS`,
*on by default* so the box starts checked; an explicit unchecked choice or
`STELLA_OS_TOOLS=0` turns it off)
*and* a real session signature with `hyprctl`, `grim`,
`tesseract` and `wtype` on `PATH` — so the model never sees a capability
that could only fail. Risk levels stay application-owned: reads and focus
are `SENSITIVE`, typing and window management are `DANGEROUS`, and all four
route through the existing
exact-argument approval dialog, whose preview names the concrete target —
the window (class, title, pid, address) or the literal launcher command and
workspace — and shows the literal text.

### CLI and shared application layer

`stella.app` is the shared application layer that both interfaces build on.
`build_application(StellaSettings)` constructs the `OpenAILLMClient` or
`OllamaLLMClient`, `LLMBrain`, a `ToolDispatcher` holding the registered
capabilities, `SQLiteMemory`, then wraps them in
a `Stella` instance and a `StellaApplication` that owns their lifecycle.
For the OpenAI-family provider the settings carry only the non-secret
`preset` id; the key itself is resolved at build time through
`provider_keys.effective_api_key(settings.preset)` (environment first for the
openai/custom slots, then the private stored key) and the client is
constructed with that key, the preset's base URL and the preset's tool
dialect. Voice gates resolve the **openai slot only** — a Claude or Grok key
is never offered to OpenAI-compatible transcription or speech endpoints.
Both interfaces resolve their startup settings through
`stella.config.resolve_settings()`: an explicit `STELLA_MODEL` (and the other
provider environment variables) wins, otherwise the non-secret saved
configuration written by the first-run setup dialog is loaded from
`config.json`, otherwise the UI opens setup and the CLI reports that none is
configured. `StellaSettings.from_environment()` remains for the
environment-variable path. Each opt-in capability is wired across six
touchpoints — env override, `Settings` field, `from_saved`,
`from_environment`, `_CONFIG_FIELDS`, and the Settings checkbox — and
`tests/test_settings_wiring.py` fails if any one of them drifts.
`StellaSession` holds the conversation `Message` history and
runs one turn through `Stella.process()` with shared error and display rules,
and the small panel classes (`MemoryPanel`, `ApprovalBroker`, and
`VoicePanel`) expose memory, approval, and voice operations only
through the existing trusted APIs. `VoicePanel` combines the `stella.voice`
recorder, player, and provider abstractions; its transcript enters through
`StellaSession.run_turn()` like any typed message, so voice adds no second
orchestration path. Neither interface implements a second orchestration path.

`stella.cli` is a thin terminal interface over that layer. It repeatedly reads
input, calls `StellaSession.run_turn()`, displays the
answer or tool output, and exits on `exit`, `quit`, or end-of-file. It closes
the SQLite connections when the session ends. The graphical interface in
`stella.ui` is an equally thin Tkinter client of the same layer; see
`UI.md`.

For inspection during manual testing, the CLI accepts `--debug`. This prints the structured `Decision` returned by the Brain as JSON to stderr after each processed input. The normal user-facing output is unchanged when the flag is omitted, and the inspection output does not alter execution, tool calls, or memory writes.

Manual testing showed that a natural-language response claiming that Stella will remember something does not itself create a memory write. Only a parsed `Decision` containing an explicit `MemoryWriteRequest` causes Stella to call `Memory.store()`. The debug output exists to make that distinction visible.

## Component boundaries

- `LLMClient` knows how to request a model response; it does not own memory, decisions, tools, or personality.
- `OpenAILLMClient` contains OpenAI-specific SDK details; the rest of the architecture depends on `LLMClient` rather than on that provider.
- `Context` carries compatible text input plus bounded provider-neutral input
  parts, conversation history, retrieved memories, and structured tool
  observations; it does not fetch or persist information.
- `TranscriptionProvider` converts one bounded audio observation to text; it
  does not choose decisions, authorize tools, or write memory. `stella.voice`
  supplies local-command and OpenAI implementations plus the desktop
  `Recorder`/`Player` subprocess boundaries; recordings exist only in a
  temporary directory that is deleted right after transcription.
- `VisionProvider` converts one bounded image observation to text; it does not
  choose decisions, authorize tools, or write memory.
- `SpeechProvider` renders one bounded final response as speech; it does not
  receive or grant decisions, permissions, memory, or tool authority. Spoken
  output is an interface rendering and never re-enters the decision path.
- `VideoProvider` analyzes one bounded, explicitly supplied clip reference; it
  does not capture media, retain raw media, make decisions, or grant authority.
- `Memory` stores only explicitly supplied `MemoryItem` objects; `InMemoryMemory` keeps them for one process and `SQLiteMemory` persists them at a configured path. Neither automatically captures conversations, and retrieval does not imply a write.
- `Brain` decides what kind of action is appropriate and may explicitly attach a `MemoryWriteRequest`; `SimpleBrain` does this deterministically and `LLMBrain` parses an LLM proposal. A Brain does not execute tools or write memory itself.
- `Tool` describes and executes one bounded operation; it is not exposed to arbitrary model-generated code or shell commands.
- `ToolDispatcher` owns exact capability lookup, tool validation, trusted risk
  classification, and action-specific approval checks. Approval is not
  derived from LLM output.
- `Stella` retrieves memories before asking the brain to decide, performs at most one explicit requested memory write, and coordinates a bounded synchronous sequence of decisions and tool executions; it does not create decisions, persist tool traces, or run background loops.
- `CLI` and `UI` collect input and display results through the shared
  `stella.app` layer; they do not make decisions, call providers directly for
  conversation handling, bypass `Stella`, or manufacture approvals.
- `stella.app` builds and wires the trusted components and exposes shared
  session, memory, and approval-broker behavior to both interfaces;
  it adds no decision path of its own.

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
Brain selects a non-tool decision, the trusted step bound is reached, or the
turn's time budget expires between steps (see *Turn budgets*). A proposal
after either bound returns the deterministic honest stop without execution or
another final LLM call. The default bound of one preserves the single-tool
flow above.

One step dispatches one capability, so a native reply that carries several
calls cannot have them all run here. The runtime keeps the first and reports
the rest — to the trace (`[not run: …]`) and to the next decision's request
payload (`unhandled_proposals`) — rather than losing them or executing them
without the model having seen the first result (#264). The report is built by
the runtime from its own dispatch decision, has no `success` or `output` key,
and is cleared by the next reply that carries one call.

A `kind=tool` decision may carry the reserved marker `"tool_final": true`
(when calling a tool directly, the same signal is an argument on the call; the
runtime strips it before execution). It declares that this single successful
observation is the whole answer. The fast path runs only when the marker is
set, the call was the turn's first and only tool step, and the tool itself
opts in: `Tool.terminal` is a trusted runtime property, true only for
capabilities whose successful output is already display-ready, non-secret
text (`datetime`, `system_info`). Then the observation is
rendered verbatim — for terminal tools — or synthesized in one dedicated
final-response call, and the middle re-decision call is skipped entirely.
Failed observations, every non-terminal capability, and the honest synthesis
path are unchanged; approval, memory-write gating, tool-output limits,
step trace and audit semantics run exactly as before. The marker never
overrides approval and never applies to multi-step plans. Finality is the
model's claim to make and the runtime's to honour. On the hosted router she
runs it has never been volunteered (0 of 16 tool turns), so this path is
currently a local-brain path, and the runtime does not infer finality from
`Tool.terminal` because that property says *this output may be shown as
written*, not *this call is the whole request* — ending the turn on it would
answer "What time is it? Remind me in ten minutes." and drop the reminder to
save a round trip.

Both halves of that sentence are now measured rather than assumed. The argument
the prompt invites is one the native declaration forbids — every capability is
sent as a schema with `additionalProperties: false` and all its arguments
`required`, so on the channel she runs the reserved argument was not expressible
until a screen added it as an option to the two terminal schemas (#279). That
raised the rate from 0 of 16 tool turns to **4 of 16**, and both of the four that
landed on a request with a second half were wrong — the CPU count never asked, the
file never read — while the arm's median looked 1.6 s *faster*, because a turn that
stops doing half the work is fast. So the schemas stay as they are: making the claim
legal does not make it reliable, and the runtime has no way to check a statement about
the owner's request that only the model can make. `docs/TOOL_EXECUTION.md` carries the
measurement, and research report 109 the data.

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

### Turn budgets

A turn carries two bounds, and whichever comes first ends it: at most
`max_tool_steps` dispatched tool calls (the application composes
`settings.max_tool_calls`, default 8) and at most `turn_time_limit_seconds`
of wall clock (default 90). Both are owner-settable in the Settings panel and
persisted in `config.json`, so the numbers are not a code edit and not an
environment variable.

**The clock decides whether another step begins, never whether a step that
began is worth finishing.** Both bounds are consulted only at the two points
where new work could start: at the top of the loop before another decision is
asked for, and after a `tool` decision but before its dispatch — a capability
proposed at 89.9 s does not execute at 90.5 s. A provider request already in
flight when the clock runs out is read to the end, and an `answer` that
arrives after the deadline is kept. That is a deliberate choice, not an
oversight: one call on this machine measures 10–68 s of decoding (`MODELS.md`,
*Where a turn's seconds actually go*), so a turn can finish well past the
number in Settings — and the panel says so in those words rather than
promising a deadline it cannot meet. Cancelling mid-call is the provider
client's business (`llm.py`, `run_cancellable`) and is left exactly as it was.

The deadline lives in the `process` frame, never on the instance: a second
concurrent turn must not inherit the first one's clock. The clock is injected
(`clock=time.monotonic` by default, following the `LLMBrain._clock`
precedent), so the whole time path is tested deterministically without
sleeping.

Running out is an honest stop, not a silent one and not a new model call.
`budget_stop` composes the sentence from facts the runtime knows — the limit
in force and the capability names of the steps that actually got a tool
result, deduplicated in order — and never from model text: *"I reached the
limit of 8 tool calls, so I stopped. What ran: outline search, memory list.
The request may not be finished — raise the tool-call limit in Settings."*
The runtime cannot know what remains of the request, so it says *may not be
finished* instead of inventing a remainder. The result is a normal
`StellaResult` (`cancelled=False`), so history, the window, the CLI and voice
all render it with no special case. `StellaResult.budget` (`"steps"` or
`"time"`) names which bound fired; it is display and trace material only and
never changes what is authorized. `FinalResponseEvent` carries the same value
and the CLI prints `stopped at time limit` beside the existing `stopped at
step limit`. The steps stop keeps `max_steps_reached=True` for consumers that
already looked at it.

A user cancel outranks both budgets at both checkpoints, and it stays as
silent as it is today. Reaching the tool-call bound still outranks the time
bound when both are true at the same instant, because the steps guard is
checked first.

Persistence follows the wake-word merge rather than the environment-splat
path: an environment value wins for one launch, otherwise the validated saved
value, otherwise the default. These are the first numeric fields
`config.json` persists, and a malformed number there falls back to the
default instead of refusing the file — a rejected load would silently discard
every other setting the owner saved. An out-of-range *environment* value does
refuse the launch, matching the token-budget doctrine.

What is deliberately not part of this: a per-turn token budget, a
task-progress model, and any second LLM call to summarise what is left. The
`do_nothing` prompt line now says it may never be used to abandon a task
halfway (`brain.py`), which is the whole of the "quitting early" doctrine —
whether tools already ran is in `step_trace`, so no marker the runtime cannot
honestly hold was invented.

### A tool decision that names no capability

Alongside the two budgets, the loop holds a third runtime-owned stop, checked
after them and before any dispatch. A `tool` decision with **no capability
named** is refused there, by the same kind of ending. It is
not a rare reply: measured on this machine (#257), a `qwen3:4b` turn proposed
`{"kind": "tool", "arguments": {"kind": "weekday"}}` five times out of thirty-five
tool decisions, and the reply on screen was *"the tool to check the time is
unavailable"*. The old path manufactured that sentence: the empty name went to
`ToolDispatcher.execute`, which correctly answered *"Tool capability
unavailable"*, that became a `tool_observations` entry, and the brain — told to
be honest about what it saw — reported a tool that had never existed. Three
things follow from that, all runtime rules rather than model fixes:

- **Nothing is guessed.** The runtime does not infer `datetime` from
  `{"kind": "time"}`. Rule 3 is one-directional: the model proposes, the runtime
  authorizes, and a capability Stella invented is a capability nobody asked for —
  it would also carry the risk tier of a tool chosen by the wrong party.
- **Nothing is dispatched, so nothing is audited as failed.** No `AuditRecord`
  exists for a call that never was, and `outcome_status(None)` reads
  *"Nothing was executed."* with no success tick (#227's rule that a refusal
  never borrows a tick).
- **The turn ends in words, and the proposal stays on the record.** The refused
  `Decision` remains the result's decision — `kind=tool`, no capability — so the
  trace shows what actually happened, and `unnamed_tool_stop` speaks the same
  family as `budget_stop`: runtime facts only, no model text and no second
  provider call. *"That answer asked me to use a tool without saying which one,
  so I stopped instead of guessing. Nothing ran and nothing was changed — try
  asking again."*

The refusal is gated on **nothing having run yet**. Once a step produced an
observation, an unnamed proposal is not the end of the turn: the existing guard
that retires a tool loop on an unregistered capability converts the decision to
`answer` and the user gets the result they were owed. Losing a good answer
because a second proposal was garbled is a different bug and the worse one.
`tests/test_stella.py` holds both halves, and `tests/test_tool_outcomes.py` holds
the after-a-real-step half.

The stop has since been seen catching the real thing. The six-question local
`qwen3:4b` run of 2026-10-12 (`~/bench-out/router_ab_1791796765.json`) fumbled
on its `cpus` turn exactly as #257 measured: a `tool` decision with no
capability. It ended at the gate in one step with nothing executed, not with a
manufactured observation about a tool that never existed. The turn still failed
— an unnamed proposal cannot answer a question about CPU count, and the run
graded it as failed, which is honest — and what the guard buys is that the
failure names itself instead of blaming a capability that is fine. A fumble is
stochastic, so this is one sighting of the guard working, not a rate.

## Intentionally not implemented yet

The following are outside the current system (things that have shipped are
described in the architecture above, not here):

- Unbounded LLM-based planning or decision-making — the tool loop is bounded
  by both turn budgets (tool calls and wall clock, `Turn budgets` above) and
  the limit result is deterministic (what the bound
  costs a multi-tool request, and which techniques were reviewed and rejected
  for closing that gap, is recorded in `AGENTIC_TASKS.md`)
- Summarization, compaction, or learned context compression; retrieval is
  bounded and observable, not silently rewriting history
- Automatic memory capture or memory writes without an explicit Brain request
- External embedding services or vector databases — semantic recall uses
  Stella's own SQLite vector storage with the local MiniLM or Ollama providers
  (see `semantic_memory.py`, `ROADMAP.md` B2.1)
- Multi-user data management and shared workspaces
- Plugin registries, permissions, external APIs, subprocesses, or shell execution
- Generalized event ingress or action execution from a background source;
  schedulers, daemons and loops of Stella's own. With the reminder store
  gone, the one remaining timed wake (`ReminderTicker`) does nothing but ask
  Outline for the alerts *it* already declared due, and the answer is one line
  of chat — never a tool call, never a model turn (see `REMINDERS.md`)
- Audio capture and output are limited to the explicit one-utterance desktop
  voice mode in `stella.voice` (see `VOICE.md`); there is no continuous
  listening or streaming. The one always-*open* path is the opt-in wake ear in
  `stella.wake`: it holds the shared capture for a local classifier, records
  nothing until a phrase is confirmed, and its whole authority is pressing the
  existing **Listen** button. Vision providers remain interfaces only;
  video or environment processing, or provider-specific multimodal
  integrations beyond the current voice path are not implemented
- Plugin loading, dynamic discovery, and permission checks (approval
  presentation now exists in the CLI prompt and the Tkinter UI dialog, both
  served by the trusted `ApprovalBroker`)
- Filesystem operations beyond the current narrow workspace tools, shell
  execution, and unrestricted network access

## Project structure

```text
.
├── README.md
├── CHANGELOG.md
├── pyproject.toml
├── uv.lock
├── src/
│   └── stella/
│       ├── __init__.py
│       ├── app.py             # shared application layer + settings
│       ├── audio.py           # recording boundary
│       ├── audio_output.py    # playback boundary
│       ├── barge_in.py        # opt-in Silero VAD voice-interrupt ear (Stage B7)
│       ├── brain.py           # structured decisions from the LLM
│       ├── capture_target.py  # which microphone the always-open ear takes
│       ├── cli.py             # terminal front-end
│       ├── conformance.py     # provider conformance suite harness (Stage B4)
│       ├── config.py          # settings and first-run configuration
│       ├── context.py         # conversation/context assembly
│       ├── event_bus.py       # two-tier intention routing (rules, then laya)
│       ├── history.py         # durable action-history store
│       ├── laya_judge.py      # subprocess Tier-1 judge client
│       ├── laya_runner.py     # line-JSON server for the laya venv
│       ├── llama_server.py    # Stella-owned llama.cpp brain process
│       ├── llm.py             # provider-agnostic LLM interface
│       ├── memory.py          # SQLite + in-memory memory stores
│       ├── minilm_embedding.py # optional CPU sentence-transformers provider
│       ├── ollama_client.py   # local Ollama client
│       ├── ollama_embedding.py # Ollama /api/embed provider
│       ├── openai_client.py   # OpenAI-compatible client
│       ├── desktop/           # screen/focus/type tools pkg (was os_tools.py)
│       ├── outline_tools.py   # opt-in Outline app search/create/update tools
│       ├── persona.py         # style files, edit snapshot history, reflection
│       ├── proactivity.py     # due-task surface during interaction
│       ├── provider_keys.py   # provider presets, verified keys, 0600 store
│       ├── semantic_memory.py # provider-neutral semantic retrieval + local fallback
│       ├── stella.py          # turn orchestration
│       ├── tools.py           # tools, dispatcher, risk, approval, receipts
│       ├── trace.py           # compact action timeline
│       ├── ui.py              # Tkinter desktop front-end
│       ├── video.py           # one-shot video observation boundary (interface only)
│       ├── vision.py          # one-shot image observation boundary (interface only)
│       └── voice.py           # one-utterance voice periphery
└── tests/
    ├── evaluation/            # model-behavior evaluation harness
    ├── security/              # trust-boundary regression tests
    └── test_*.py              # one module plus behavior suites
```

The project uses Python with uv. Runtime code lives under `src/stella`, and the independent behavior tests live under `tests`.

## Running Stella locally

For normal use, install the built package (see the README) and just start:

```bash
stella-ui    # desktop window
stella       # terminal chat
```

No environment variables are required. On first launch `stella-ui` shows a
setup dialog that detects a local Ollama server, lists the models actually
installed through Ollama's own API, and records the provider/model/endpoint
choice (never an API key) in `config.json` under the XDG data directory once
a connection test succeeds. The CLI uses the same file; with neither a saved
configuration nor `STELLA_MODEL` it prints a one-line pointer to `stella-ui`.
From a source checkout, replace the commands with `uv run stella-ui` /
`uv run stella`. Add `--debug` to inspect Brain decisions and `--trace` for
the compact action timeline.

Environment variables remain the advanced-user path and always win over the
saved file: `STELLA_MODEL` selects the model, `OPENAI_API_KEY` overrides the
stored key for the OpenAI slot (and a custom endpoint) for that launch —
`STELLA_PRESET` chooses which preset's key and endpoint that launch uses,
while every named preset (Claude, Grok, …) resolves only against its own
stored key — `STELLA_LLM_PROVIDER=ollama|openai` overrides
provider selection, `OPENAI_BASE_URL` and `OLLAMA_BASE_URL` point either
client at a non-default address, and `STELLA_MEMORY_DB`
and `STELLA_WORKSPACE` override the state locations, which default under the
XDG data directory (`~/.local/share/stella`). Verified API keys live in
`api_keys.json` next to those files, in the same directory, at 0600 — see
`SECRETS.md`. Voice mode is configured through
`STELLA_VOICE_TRANSCRIPTION`, `STELLA_VOICE_SPEECH`,
`STELLA_TRANSCRIPTION_COMMAND`, `STELLA_SPEECH_COMMAND`,
`STELLA_TRANSCRIPTION_MODEL`, `STELLA_SPEECH_MODEL`, and
`STELLA_SPEECH_VOICE`, all optional and local-first; see `VOICE.md`. The
interfaces read these variable names through `stella.app` but do not contain
or expose secret values.

## Current verification status

The full pytest suite (unit, security, and evaluation tests) and Ruff pass
on the release commit; see the release notes for the exact final count.

The OpenAI client tests mock the SDK, so the test suite does not make real API calls.

This document should be updated when a meaningful architectural component is added or an important design boundary changes.
