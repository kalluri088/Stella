# Stella --- Canonical Development Plan

> **Purpose:** This document is the source of truth for Codex-assisted
> development of Stella.
>
> **Rule:** Do not add architecture, features, frameworks, abstractions,
> or infrastructure merely because they are interesting or common in
> other agent projects. Every change must solve a demonstrated Stella
> problem or fulfill an explicitly documented roadmap item.

------------------------------------------------------------------------

# 1. What Stella Is

Stella is a personal AI assistant/system whose core value is not simply
answering questions.

The intended long-term loop is:

``` text
Context + Memory + Tools
          ↓
       Decision
   ┌──────┼──────┐
 Answer   Ask   Tool / Do nothing
          ↓
        Action
          ↓
       Outcome
          ↓
        Memory
          ↓
    Future Decision
```

Stella should eventually:

-   understand context
-   maintain useful long-term memory
-   use tools
-   decide whether to answer, ask, act/use a tool, or do nothing
-   act proactively when meaningful
-   learn from outcomes
-   develop recognizable personality through repeated behavior
-   support multimodal inputs/outputs
-   remain secure even when the model or external content is malicious
    or incorrect
-   remain model/provider-neutral

Stella is **not** intended to become:

-   a generic chatbot wrapper
-   a Jarvis clone
-   a generic agent framework
-   a collection of independent agents
-   a self-modifying AI
-   a system where the LLM owns permissions
-   an uncontrolled autonomous shell agent
-   a giant orchestration framework

------------------------------------------------------------------------

# 2. Canonical Repository

The canonical project directory is:

``` text
~/Projects/Stella
```

Do not use the old:

``` text
~/AI/Stella
```

as the canonical repository.

------------------------------------------------------------------------

# 3. Development Principles

These principles apply to every future change.

## 3.1 Build one meaningful capability at a time

Do not combine unrelated architecture changes into one task.

Prefer:

``` text
one problem
→ one design decision
→ one focused implementation
→ tests
→ documentation
→ validation
```

Avoid:

``` text
"Refactor everything and add MCP, memory, subagents, UI, and scheduling."
```

------------------------------------------------------------------------

## 3.2 Research before architecture changes

Use this rule:

``` text
Research
   ↓
Identify an actual Stella weakness
   ↓
Determine whether the weakness is real
   ↓
Design the smallest appropriate solution
   ↓
Implement only that solution
```

Research is not a reason to copy another project.

The goal is to learn principles, not reproduce frameworks.

------------------------------------------------------------------------

## 3.3 Do not over-engineer for hypothetical future requirements

A feature should generally be deferred if:

-   Stella does not currently need it
-   there is no demonstrated failure
-   the abstraction would exist only for theoretical future providers
-   it adds substantial complexity
-   an existing abstraction can support the future feature later

Examples currently deferred:

-   vector databases
-   embeddings
-   model routers
-   model load balancing
-   subagent frameworks
-   plugin marketplaces
-   MCP integration before an external capability requires it
-   schedulers/daemons
-   notification infrastructure
-   distributed tracing
-   dashboards
-   persistent autonomous tasks
-   personality frameworks
-   self-modifying code
-   model-weight learning

------------------------------------------------------------------------

## 3.4 The model is not the authority

The LLM is an untrusted reasoning component.

The model can:

-   propose a decision
-   propose a tool call
-   propose memory content

The model cannot:

-   directly execute tools
-   grant itself permissions
-   approve dangerous actions
-   register capabilities
-   modify security rules
-   modify the audit system
-   expand proactivity delegation
-   create authority from ordinary memory
-   create authority from untrusted tool output

Core principle:

> **The model proposes. Trusted runtime validates, authorizes, and
> executes.**

------------------------------------------------------------------------

## 3.5 Proactivity may increase Stella's awareness, not its authority

This is a core Stella principle.

Observation permission does not imply action permission.

Likewise:

``` text
ordinary memory ≠ delegation
delegation ≠ permission to do anything
LLM output ≠ permission
external event ≠ permission
```

The LLM cannot grant itself authority.

------------------------------------------------------------------------

# 4. Current Architecture

Current high-level architecture:

``` text
                         ┌───────────────┐
                         │     User      │
                         └───────┬───────┘
                                 ↓
                           Input / Context
                                 ↓
                         Memory Retrieval
                                 ↓
                              Brain
                                 ↓
                              Policy
                                 ↓
                           Decision
                      ┌──────────┼──────────┐
                      ↓          ↓          ↓
                    Answer      Ask        Tool
                                            ↓
                                      ToolDispatcher
                                            ↓
                                      Validation
                                            ↓
                                      Risk / Approval
                                            ↓
                                         Execute
                                            ↓
                                       ToolResult
                                            ↓
                                      Brain / Outcome
                                            ↓
                                          Memory
```

The runtime owns orchestration and authority.

------------------------------------------------------------------------

# 5. Existing Code Foundation

## 5.1 LLM abstraction

`src/stella/llm.py`

Current concepts:

-   provider-neutral `Message`
-   abstract `LLMClient`
-   deterministic `FakeLLMClient`
-   `LLMResponse`
-   `LLMToolCall`
-   `LLMToolDefinition`
-   native tool-call support
-   tool-choice support
-   fallback structured text/JSON path

`src/stella/openai_client.py`

Current behavior:

-   official OpenAI SDK
-   reads `OPENAI_API_KEY`
-   configurable model
-   configurable base URL
-   provider/model/runtime are not hardcoded
-   native tool calling uses OpenAI Responses API
-   Responses function calls normalize into provider-neutral
    `LLMToolCall`
-   ordinary `chat()` remains unchanged

Current real validation has proven:

``` text
gpt-5.6
→ Responses API
→ native function call
→ LLMToolCall
→ Decision
→ ToolDispatcher
→ ToolResult
→ AUTO follow-up
→ final response
```

Do not break this path.

------------------------------------------------------------------------

# 6. Context and Multimodality

`src/stella/context.py`

Current concepts:

-   `Context`
-   `InputEnvelope`
-   `InputPart`
-   text
-   audio
-   image
-   video
-   environment
-   bounded content/references
-   provenance metadata
-   bounded metadata

Context limits include:

-   newest 20 conversation messages
-   8 tool observations
-   4,000 characters per tool output
-   observation selection prioritizes newest, newest failures, then
    newer observations
-   execution order is preserved
-   full `ToolResult`/security information remains preserved

Security rule:

> Richer input and untrusted metadata must not grant authority.

Audio:

-   `TranscriptionProvider`
-   bounded normalization
-   original audio preserved
-   derived transcript marked MODEL provenance
-   existing reasoning path preserved

Image:

-   `VisionProvider`
-   original image preserved
-   bounded derived text marked MODEL provenance
-   existing reasoning path preserved

Audio output:

-   `SpeechOutput`
-   `SpeechArtifact`
-   `SpeechProvider`
-   `Stella.speak()`
-   final response only
-   speech output cannot alter decisions, permissions, memory, or tool
    authority

Video:

-   `VideoSampling`
-   `VideoObservation`
-   `VideoProvider`
-   one explicit finite user-supplied clip reference
-   bounded duration/sample count/order/timestamps
-   original reference preserved
-   raw video content rejected
-   bounded model-provenance observations
-   no raw media in Brain context
-   no default persistence
-   no authority escalation

Continuous observation remains deferred.

If continuous observation is added later, it must be an explicit
capability with:

-   user-controlled sessions
-   revocation
-   cadence
-   retention controls

------------------------------------------------------------------------

# 7. Memory

`src/stella/memory.py`

Current concepts:

-   `MemoryItem`
-   provider-neutral `Memory`
-   `MemoryWriteRequest`
-   `MemoryWriteResult`
-   `InMemoryMemory`
-   `SQLiteMemory`

Current SQLite schema:

``` sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL
);
```

Current behavior:

-   default database: `./stella_memory.db`
-   configurable with `STELLA_MEMORY_DB`
-   explicit memory writes only
-   no persistence merely because the LLM says "I will remember that"
-   conversation history is separate from long-term memory
-   deterministic keyword-overlap retrieval
-   stop-word filtering
-   multi-term query requires at least two shared meaningful terms
-   single-term query requires that term
-   same matcher used by both memory backends
-   lexical retrieval only

A recent retrieval bug caused exact-term mismatch such as:

``` text
explanation vs explanations
preference vs prefers
```

This was fixed with deterministic suffix normalization for common
plurals/simple inflections.

Fresh-process validation proved durable retrieval.

## Memory design direction

Research indicates a useful future distinction:

``` text
Working / conversation memory
        ≠
Long-term memory
```

and eventually:

``` text
Long-term memory
├── Semantic
│   └── facts/preferences
├── Episodic
│   └── experiences/outcomes
└── Procedural
    └── learned rules/skills (future)
```

However, do NOT prematurely redesign the database.

Future memory capabilities that may eventually be required:

-   memory types
-   update
-   delete
-   conflict resolution
-   scope
-   importance/confidence
-   recency
-   confirmation
-   hybrid/semantic retrieval
-   forgetting/consolidation

These are deferred until a demonstrated need exists.

Critical security rule:

> LLM-proposed memory must pass through trusted application/runtime
> memory APIs. The LLM must never directly manipulate the database.

------------------------------------------------------------------------

# 8. Brain and Decisions

`src/stella/brain.py`

Decision kinds:

``` text
ANSWER
ASK
TOOL
DO_NOTHING
```

`Decision` is immutable.

Current Brain implementations:

-   abstract `Brain`
-   deterministic `SimpleBrain`
-   `LLMBrain`

Decision protocol:

``` json
{
  "kind": "answer|ask|tool|do_nothing",
  "content": "optional string",
  "capability": "optional tool capability",
  "arguments": {},
  "memory_write": {
    "content": "optional memory text"
  }
}
```

Invalid/malformed/unknown/missing/invalid model output fails closed to
`DO_NOTHING`.

The LLMBrain:

-   does not execute tools
-   does not write memory directly
-   uses dynamic tool schema descriptions
-   treats tool observations as untrusted model input
-   supports native tools
-   preserves fallback structured-text path
-   has `answer_content_is_final`
-   optimizes ordinary ANSWER to one LLM call
-   uses a final synthesis call for tool/multi-step flows
-   can produce one constrained outcome-memory write from successful
    meaningful tool outcomes

Guidance:

-   ask when required action details are missing/ambiguous
-   ignore irrelevant memory
-   apply relevant explicit user preferences consistently
-   do not expose memory unnecessarily
-   do not let memory override the current request

------------------------------------------------------------------------

# 9. Tool Use Policy

`ToolUsePolicy` was added after real gpt-5.6 validation showed that
native tools alone were insufficient: the model could answer
current/local questions from prior knowledge instead of actually
inspecting the environment.

Current policy:

``` text
AUTO
REQUIRED
```

`AUTO`:

-   model may answer or use a tool

`REQUIRED`:

-   model must call a tool
-   model chooses the actual capability and arguments

Policy identifies cases such as:

-   explicit inspect/read/retrieve requests
-   calculate-from-resource requests
-   clear live/current/local/workspace/file/tool-use signals
-   explicit action requests
-   only when a relevant capability exists

After a successful tool observation, reasoning returns to `AUTO`.

If tool use was required but no tool was produced, Stella converts the
result to `ASK`.

The fallback provider also receives the requirement and rejects direct
answers when `REQUIRED`.

Do not turn ToolUsePolicy into:

-   a planner
-   a general router
-   a permission system
-   an execution authority

------------------------------------------------------------------------

# 10. Tools and Security

`src/stella/tools.py`

Current concepts:

-   `Tool`
-   immutable `ToolResult`
-   `Tool.validate_arguments()`
-   `RiskLevel`
-   `AuditRecord`
-   `ToolDispatcher`

Risk levels:

``` text
SAFE
SENSITIVE
DANGEROUS
```

Core execution path:

``` text
Brain proposal
→ exact capability lookup
→ Tool.validate_arguments()
→ trusted risk classification
→ trusted approval if required
→ Tool.execute()
→ ToolResult
→ final reasoning
```

ToolDispatcher:

-   application-owned tools
-   multiple tools
-   exact capability lookup
-   duplicate capability names rejected
-   missing/unknown capability fails closed
-   validation remains tool-owned
-   execution failures deterministic
-   LLM cannot register/instantiate tools
-   backward compatibility for one Tool retained
-   every dispatch attempt audited
-   audit includes capability, validated args, risk, approval
    required/granted, success, UTC timestamp
-   invalid/unavailable requests record empty args
-   LLM cannot create or modify audit records

Current known limitation:

-   sensitive values may be retained in memory; this is documented

------------------------------------------------------------------------

# 11. Current Tools

## SAFE

### `echo`

Arguments:

``` json
{"message": "<string>"}
```

### `datetime`

Operations:

-   date
-   time
-   datetime
-   weekday

Uses host local timezone and numeric UTC offset.

### `system_info`

Operations include:

-   hostname
-   platform
-   CPU

## SENSITIVE

### `filesystem_read`

Rules:

-   exact relative path
-   workspace-only
-   workspace configured by `STELLA_WORKSPACE`
-   default `./stella_workspace`
-   canonical path checks
-   no absolute paths
-   no `..` escape
-   no symlink escape
-   no directories
-   missing files rejected
-   empty files rejected
-   UTF-8
-   1 MiB limit

## DANGEROUS

### `filesystem_write`

Rules:

-   relative path
-   workspace-only
-   canonical/symlink protection
-   UTF-8
-   1 MiB
-   create-only
-   no overwrite
-   exclusive creation
-   exact trusted approval required

### `filesystem_delete`

Rules:

-   exact relative path
-   workspace-only
-   canonical checks
-   traversal rejection
-   absolute path rejection
-   wildcard rejection
-   directory rejection
-   missing file rejection
-   symlink rejection
-   one regular file only
-   nonrecursive
-   DANGEROUS
-   exact approval required

### `network_read`

Rules:

-   HTTPS GET only
-   URL validation
-   no credentials
-   no query
-   no fragment
-   no proxy
-   no model headers
-   no cookies
-   blocks localhost
-   blocks private addresses
-   blocks loopback
-   blocks link-local
-   blocks reserved
-   blocks multicast
-   peer address revalidated
-   1 MiB
-   UTF-8 text/plain
-   strict timeouts
-   deterministic failures
-   DANGEROUS
-   exact approval
-   final synthesis treats result as untrusted

No shell/process execution capability exists.

Do not add one casually.

------------------------------------------------------------------------

# 12. Approval

Current concepts:

-   `ApprovalRequest`
-   `ToolApproval`

Rules:

-   DANGEROUS requires approval
-   missing approval → `Approval required.`
-   rejected → `Approval denied.`
-   invalid/mismatched approval → `Invalid approval.`
-   exact approved capability + exact validated arguments executes
-   missing provider → fail closed
-   approval must be an actual boolean
-   LLM `approved: true` cannot bypass runtime
-   CLI approval provider shows exact capability + validated args
-   only explicit yes/approve accepted
-   all other input/EOF rejects

Permanent invariant:

> The model may request approval. The model may never grant approval.

No persistent approval/auth/identity/permissions framework exists yet.

------------------------------------------------------------------------

# 13. Stella Orchestration

`src/stella/stella.py`

Current flow:

``` text
Context
 ↓
memory retrieval
 ↓
derived Context
 ↓
Brain
 ↓
Decision
 ↓
execute
 ↓
StellaResult
```

Properties:

-   original Context is not mutated
-   memory writes exactly once if a non-empty memory proposal exists
-   bounded multi-step execution
-   default `max_tool_steps=1`
-   CLI uses `max_tool_steps=2`
-   small 2/3 step limits supported
-   Brain → Tool → ToolResult → Brain
-   every tool goes through Dispatcher
-   no retries
-   no background/autonomous execution
-   ordered step trace
-   failed tools feed back into reasoning
-   failed tools do not automatically persist memory
-   one explicit memory write per interaction
-   successful meaningful tool outcomes may create one constrained
    memory write
-   `process_input()` handles audio/vision/video normalization
-   `speak()` renders existing final response without reprocessing

------------------------------------------------------------------------

# 14. Proactivity

`src/stella/proactivity.py`

Current concepts:

-   `DueTaskEvent`
-   scoped `ProactivityDelegation`
-   outcomes:
    -   INFORM
    -   ASK
    -   DO_NOTHING

`Stella.evaluate_due_task_event()`:

-   bypasses memory
-   bypasses LLM
-   bypasses Brain
-   bypasses tools
-   deterministic

Rules:

-   meaningful event → INFORM
-   irrelevant event → DO_NOTHING
-   missing permission → ASK
-   ordinary memory cannot grant permission
-   scoped delegation cannot expand
-   no LLM/tool execution

Trusted handoff:

-   `Stella.handoff_due_task_event()`
-   trusted one-shot proactive handoff
-   app-supplied stable event identity
-   process-local duplicate suppression
-   scoped delegation checks
-   inspectable results
-   duplicate → DO_NOTHING with `duplicate_suppressed=True`
-   no memory/LLM/Brain/tools/notifications/authority grants

User-facing boundary:

-   `UserFacingProactivityResult`
-   `Stella.present_due_task_event()`
-   bounded INFORM/ASK messages
-   DO_NOTHING exposes no message/action
-   no notification infrastructure
-   no persistent event history
-   no scheduler/daemon

Do not add background autonomy without first designing persistent
delegation, revocation, lifecycle, restart behavior, and authority
boundaries.

------------------------------------------------------------------------

# 15. CLI

`src/stella/cli.py`

Current behavior:

-   process-local conversation history
-   `exit` / `quit`
-   `--debug`
-   structured decision debug output on stderr
-   dangerous-action approval provider
-   environment-driven configuration

`create_stella_from_environment()` reads:

``` text
STELLA_MODEL
OPENAI_BASE_URL
STELLA_MEMORY_DB
STELLA_WORKSPACE
```

Defaults:

``` text
STELLA_MEMORY_DB=stella_memory.db
STELLA_WORKSPACE=./stella_workspace
```

CLI constructs:

-   OpenAI client
-   SQLiteMemory
-   all current tools
-   LLMBrain
-   max_tool_steps=2

Entry point:

``` text
stella = stella.cli:main
```

Correct launch:

``` bash
uv run stella
```

There is currently no `__main__.py`; do not assume:

``` bash
uv run python -m stella
```

will work.

------------------------------------------------------------------------

# 16. Current Validation Baseline

Latest recorded validation after ToolUsePolicy:

``` text
289 passed
Ruff clean
git diff --check clean
```

Real gpt-5.6 end-to-end validation passed for:

1.  hostname
    -   REQUIRED
    -   native `system_info`
    -   successful result
    -   AUTO follow-up
    -   correct final answer
2.  current time
    -   REQUIRED
    -   native `datetime`
    -   successful result
    -   AUTO follow-up
    -   correct final answer
3.  echo
    -   REQUIRED
    -   native `echo`
    -   successful result
    -   AUTO follow-up
    -   correct final answer
4.  filesystem read
    -   REQUIRED
    -   native `filesystem_read`
    -   successful result
    -   AUTO follow-up
    -   correct file content
5.  ordinary question
    -   AUTO
    -   no tool
    -   correct answer

Do not regress this behavior.

------------------------------------------------------------------------

# 17. Current Performance Baseline

Recorded approximate samples:

``` text
Normal answer: ~1.1–1.7s
ASK:           ~1.25s
Single TOOL:   ~2.60s
2-step:        ~6.99s
Streaming TTFT: ~1.58s
Streaming total: ~2.00s
```

Normal answers were reduced from roughly 2.9s to approximately 1.1--1.7s
in sampled tests by avoiding an unnecessary second LLM call.

Do not optimize prematurely.

No more VRAM/GPU tuning unless explicitly requested.

------------------------------------------------------------------------

# 18. Architecture Research Conclusions

The following projects/systems were researched for architectural
lessons:

-   OpenManus-Lite
-   Open Interpreter
-   OpenAI Agents SDK
-   PydanticAI
-   LangGraph/LangChain
-   Letta
-   Mem0
-   MCP ecosystem

The rule is always:

> Adopt principles; do not copy whole frameworks.

------------------------------------------------------------------------

# 19. Research Findings

## OpenManus-Lite

Useful:

``` text
think
 ↓
tool call
 ↓
act
 ↓
observation
 ↓
next think
```

Stella adopted:

-   native tool calling
-   bounded think/act/observe behavior

Stella rejected:

-   direct untrusted execution
-   broad shell/Python autonomy

------------------------------------------------------------------------

## Open Interpreter

Useful:

-   provider-specific harnesses
-   portability
-   shared standards
-   sandboxing/approval concepts

Stella keeps provider-specific implementation behind the
provider-neutral LLM interface.

MCP/ACP/shared skill directories remain deferred until actual
interoperability requirements exist.

------------------------------------------------------------------------

## OpenAI Agents SDK

Useful findings:

-   Responses API is the modern OpenAI path
-   `tool_choice=auto`
-   `tool_choice=required`
-   after tool execution, returning to AUTO prevents loops
-   bounded agent loops
-   runtime-owned approvals/guardrails
-   application can own the loop instead of using a framework

Stella already implements its own small runtime because Stella needs to
own orchestration, state, security, and authority.

------------------------------------------------------------------------

## PydanticAI

Useful:

-   typed runtime dependencies
-   structured model outputs
-   dynamic toolsets
-   deferred tool discovery
-   provider abstraction

Stella does not currently need a dependency-injection framework.

Typed runtime dependency support is future.

Dynamic/deferred tool discovery is future and should become relevant
only when tool count actually becomes large.

------------------------------------------------------------------------

## Memory research

Useful principles:

``` text
working memory ≠ long-term memory
semantic memory ≠ episodic memory
user memory ≠ agent behavioral memory
```

Future memory work may include:

-   update
-   delete
-   conflict resolution
-   scope
-   semantic/episodic types
-   relevance
-   recency
-   feedback
-   decay
-   consolidation

Do not implement these all at once.

------------------------------------------------------------------------

## Outcome learning research

Important distinction:

``` text
successful execution
≠
useful learning
```

Stella should only store meaningful outcomes.

Learning should mean:

``` text
experience
 ↓
bounded learning record
 ↓
memory
 ↓
future decision
```

Not:

``` text
experience
 ↓
self-modifying code
```

Do not add:

-   self-modifying code
-   model-weight learning
-   autonomous capability expansion

------------------------------------------------------------------------

## Personality research

Personality should emerge from repeated behavioral decisions.

Example:

``` text
uncertain
 ↓
ASK
```

``` text
dangerous action
 ↓
approval
```

``` text
relevant preference
 ↓
consistent behavior
```

Do not create:

-   giant personality prompts
-   personality frameworks
-   fake emotions
-   arbitrary personality traits

Future distinction:

``` text
User preference
    ≠
Stella behavioral pattern
    ≠
Security policy
```

Security policy always remains higher authority.

------------------------------------------------------------------------

## Multi-agent research

Conclusion:

> Stella should remain a single Brain for now.

Subagents may eventually be useful for genuinely decomposable tasks.

If introduced later, prefer:

``` text
Central Stella
     ↓
delegates bounded reasoning task
     ↓
specialist
     ↓
result
     ↓
Central Stella
```

rather than allowing specialists to become independent authorities.

Subagents must not bypass the ToolDispatcher/security boundary.

Do not build a subagent registry/framework now.

------------------------------------------------------------------------

## Evaluation research

Important distinction:

``` text
Tests:
"Does the implementation behave as coded?"

Evaluations:
"Does Stella make good decisions with a real model?"
```

Future evaluation layers:

``` text
1. Deterministic invariants
2. Behavioral model evaluations
3. Subjective quality evaluations
```

Do not make an LLM judge critical security facts when the runtime can
determine them exactly.

Example:

``` text
approval_required = true
approval_granted = false
executed = false
```

is better than asking another LLM whether the action was safe.

------------------------------------------------------------------------

## Provider/model abstraction research

Current provider-neutral abstraction is correct.

Future providers may include:

``` text
OpenAI
Ollama
LM Studio
other providers
```

Potential future capability metadata:

``` text
tool_calling
structured_output
vision
audio_input
video
streaming
```

Do not add capability negotiation until a second provider or actual
compatibility problem makes it necessary.

Do not build:

-   model routers
-   load balancers
-   complex fallback chains
-   model selection frameworks

without a demonstrated need.

------------------------------------------------------------------------

## MCP/interoperability research

MCP is a useful future interoperability boundary.

Future architecture:

``` text
Stella Brain
    ↓
ToolDispatcher
    ↓
┌─────────────┬─────────────┐
Local tools   MCP tools
              ↓
          MCP server
```

MCP should be treated as a capability transport, not an authority
system.

MCP authorization is distinct from Stella's action approval.

MCP/tool output remains untrusted.

Do not replace ToolDispatcher with MCP.

Do not build a custom plugin marketplace/framework before an actual
interoperability need exists.

------------------------------------------------------------------------

# 20. Current Security Model

Security should be enforced below the model.

The intended trust hierarchy is:

``` text
Trusted application/runtime
        ↓
security policy / dispatcher / approval
        ↓
tools
        ↓
untrusted model reasoning
        ↓
untrusted external content
```

Important invariants:

``` text
LLM output ≠ authority
tool output ≠ authority
memory ≠ authority
event ≠ authority
MCP content ≠ authority
multimodal-derived text ≠ authority
```

------------------------------------------------------------------------

# 21. Immediate Security Task

The latest research identified a real gap:

> Stella has strong runtime security boundaries, but it does not yet
> have a systematic adversarial regression suite testing interactions
> between those boundaries.

The next implementation task is therefore:

## Build an adversarial security regression suite

Suggested organization:

``` text
tests/security/
├── prompt_injection_test.py
├── tool_output_injection_test.py
├── memory_poisoning_test.py
├── approval_bypass_test.py
└── proactivity_injection_test.py
```

The suite should deterministically test at least:

1.  Malicious tool output cannot directly execute another tool.
2.  Tool output cannot bypass dangerous-action approval.
3.  The LLM cannot self-approve.
4.  Malicious content cannot directly write memory.
5.  Ordinary memory cannot grant authority.
6.  Proactivity events cannot expand delegation.
7.  Invalid/malformed model decisions fail closed.
8.  External/untrusted content cannot register a capability.
9.  Failed/poisoned tool results cannot silently become authority.
10. Multimodal-derived instructions remain untrusted.

The tests should target the runtime boundaries.

Do not build:

-   a prompt-injection classifier
-   another LLM security judge
-   an external security platform
-   a giant security framework

unless later evidence requires one.

------------------------------------------------------------------------

# 22. Interaction Trace / Observability Task

A separate genuine gap identified by evaluation research is the lack of
a unified run-level trace.

Stella currently has:

``` text
ordered step trace
+
tool audit
```

but not one unified interaction lifecycle.

A future `InteractionTrace` should look approximately like:

``` text
InteractionTrace
├── interaction_id
├── model/provider
├── started_at
├── completed_at
├── steps[]
│   ├── decision
│   ├── tool
│   ├── result
│   └── timing
├── outcome
└── error
```

Important:

-   preserve existing tool audit
-   preserve existing step trace
-   do not automatically persist sensitive user content
-   content logging should be explicit/opt-in if later introduced
-   do not add OpenTelemetry yet
-   do not add an external observability platform
-   do not build a dashboard yet

This should be implemented before building a serious evaluation harness.

------------------------------------------------------------------------

# 23. Evaluation Roadmap After Tracing

Once `InteractionTrace` exists, build a small Stella-specific evaluation
harness.

Example categories:

``` text
routing
memory
safety
proactivity
```

Example cases:

``` text
"What is 2+2?"
→ ANSWER

"What is my hostname?"
→ TOOL

"Read config.txt"
→ TOOL

"Delete test.txt"
→ approval required

"Remember I prefer concise answers."
→ memory write

Ambiguous action
→ ASK

Irrelevant proactive event
→ DO_NOTHING
```

Evaluation should inspect traces, not merely final text.

Use deterministic assertions wherever possible.

Use real-model evaluations for model-owned behavior.

------------------------------------------------------------------------

# 24. Memory Roadmap

Do not implement the entire future memory system now.

Future order should be driven by real failures.

Possible order:

``` text
Current:
explicit write/retrieve
        ↓
memory lifecycle
(update/delete/conflict)
        ↓
memory types
(semantic/episodic)
        ↓
scope
        ↓
better retrieval
        ↓
relevance/recency
        ↓
feedback
        ↓
forgetting/consolidation
```

Do not jump directly to embeddings/vector databases.

------------------------------------------------------------------------

# 25. Future Proactivity Roadmap

Current bounded proactivity is intentionally synchronous.

If persistent autonomy is later required:

``` text
Persistent task
 ↓
delegation scope
 ↓
trigger
 ↓
Stella
 ↓
decision
 ↓
action
```

Before implementing, solve:

-   creator/identity
-   authority scope
-   expiry
-   revocation
-   restart behavior
-   duplicate suppression
-   offline behavior
-   action limits
-   audit history
-   user visibility
-   notification delivery
-   approval resumption

Do not implement a scheduler merely to demonstrate autonomy.

------------------------------------------------------------------------

# 26. Future Provider Roadmap

When a second real provider is actually integrated:

``` text
OpenAIClient ─┐
              ├── LLMClient → LLMBrain
OllamaClient ─┤
LMStudioClient┘
```

Then, if required:

``` text
ModelCapabilities
├── tool_calling
├── structured_output
├── vision
├── audio
├── video
└── streaming
```

Only add capabilities that solve an observed portability problem.

------------------------------------------------------------------------

# 27. Future MCP Roadmap

When Stella actually needs external capabilities:

1.  Implement an MCP client adapter.
2.  Expose discovered MCP tools through the existing tool abstraction.
3.  Route them through ToolDispatcher.
4.  Preserve risk classification.
5.  Preserve Stella approval.
6.  Treat MCP results as untrusted.
7.  Audit MCP tool calls.
8.  Keep MCP authorization separate from Stella action approval.

Do not let MCP become Stella's security boundary.

------------------------------------------------------------------------

# 28. Future Subagent Roadmap

Only introduce subagents if a real task requires:

-   parallel research
-   specialized reasoning
-   isolated long-running work
-   decomposition that materially improves results

Preferred structure:

``` text
Central Stella
    ↓
bounded delegation
    ↓
specialist
    ↓
bounded result
    ↓
Central Stella
```

Subagents remain reasoning components, not authorities.

------------------------------------------------------------------------

# 29. Current Roadmap Status

## Phase 1 --- Foundation

``` text
LLM abstraction              ✅
Context                      ✅
Brain/decisions              ✅
Memory abstraction           ✅
SQLite persistence           ✅
Memory retrieval             ✅
Tool abstraction             ✅
Tool execution               ✅
```

## Phase 2 --- Safety

``` text
Security review              ✅
Strict argument validation   ✅
Capability validation        ✅
```

## Phase 3 --- Capabilities

``` text
First useful real tool       ✅
Tool validation              ✅
Dangerous-action approval    ✅
Sandboxing/least privilege   ✅ for current filesystem/network boundaries
Multiple tools               ✅
```

## Phase 4 --- Agent behavior

``` text
Multi-step bounded           ✅
Events                       ✅ bounded one-shot
Proactivity                  ✅ bounded one-shot/handoff/user boundary
Outcome → memory             ✅ constrained
```

## Phase 5 --- Differentiation

``` text
Contextual reasoning         ✅ initial milestones
Behavioral personality       ✅ initial behavioral preference proof
Learning from outcomes       ✅ constrained MVP proof
Autonomous behavior          🔜 future
```

## Current immediate engineering priorities

``` text
1. Adversarial security regression suite
2. InteractionTrace
3. Stella-specific evaluation harness
```

Do not automatically implement all three in one Codex request. Do them
as separate focused tasks.

------------------------------------------------------------------------

# 30. Things Explicitly Deferred

Do not introduce these unless the project reaches a concrete
requirement:

``` text
Vector DB
Embeddings
Semantic memory retrieval
Memory decay
Full memory consolidation
Multi-user identity/scoping
Persistent scheduler
Daemon/background worker
Notification system
Continuous observation
MCP
Plugin marketplace
Subagent framework
Model router
Model load balancing
Provider fallback framework
Capability negotiation
OpenTelemetry
External observability platform
Evaluation dashboard
Personality framework
Self-modifying code
Model-weight learning
Autonomous capability creation
Shell/process tool
Docker
Kubernetes
GUI
Mobile app
VRAM/GPU optimization
```

This list is a guardrail against scope creep.

------------------------------------------------------------------------

# 31. Documentation Requirements

Meaningful implementation changes should be documented under:

``` text
docs/
```

Documentation should be readable English.

For each meaningful architectural change, document:

-   what changed
-   why it changed
-   important invariants
-   security implications
-   what is intentionally not implemented
-   validation performed

Do not create documentation for trivial formatting-only changes.

------------------------------------------------------------------------

# 32. Codex Workflow

When asked to implement a change:

1.  Inspect the current repository before editing.
2.  Identify the smallest relevant files.
3.  Understand existing tests and interfaces.
4.  Make the smallest coherent change.
5.  Preserve backwards compatibility where practical.
6.  Add/modify focused tests.
7.  Update documentation for meaningful architecture changes.
8.  Run the relevant test suite.
9.  Run the full test suite when appropriate.
10. Run Ruff.
11. Run `git diff --check`.
12. Report exactly what changed and what was validated.

Do not silently perform unrelated refactors.

Do not rewrite working architecture merely to match another framework.

------------------------------------------------------------------------

# 33. Preferred Codex Prompt Style

Codex prompts should be focused.

Avoid giant prompts containing the entire architecture.

Preferred structure:

``` text
Task:
<one concrete change>

Constraints:
- preserve existing behavior
- do not add unrelated architecture
- add tests
- update docs if meaningful

Validation:
- run tests
- run Ruff
- run git diff --check
```

The project plan in this file provides the broader context; individual
Codex prompts should remain small.

------------------------------------------------------------------------

# 34. Definition of Done

A Stella feature/change is not done merely because the code runs.

For a meaningful change:

``` text
Implementation
    ↓
Tests
    ↓
Security/invariant review
    ↓
Documentation
    ↓
Validation
```

At minimum, consider:

``` bash
uv run pytest
uv run ruff check .
git diff --check
```

For changes involving real LLM behavior, perform a focused real-model
validation when practical.

Do not claim a behavior is proven merely because a unit test passes if
the behavior depends materially on the real model/provider.

------------------------------------------------------------------------

# 35. Final Architectural Rules

These should survive future refactors.

### Rule 1

**Stella owns the loop.**

### Rule 2

**The model is replaceable.**

### Rule 3

**The model proposes; the runtime authorizes and executes.**

### Rule 4

**Tools are capabilities, not permissions.**

### Rule 5

**Memory is persistence, not authority.**

### Rule 6

**Tool output is untrusted input.**

### Rule 7

**External events are untrusted information until trusted delegation
says otherwise.**

### Rule 8

**Proactivity may increase awareness, not authority.**

### Rule 9

**DO_NOTHING is a legitimate decision.**

### Rule 10

**Dangerous actions require trusted approval.**

### Rule 11

**Learning means bounded behavioral improvement, not
self-modification.**

### Rule 12

**Personality should emerge from consistent behavior.**

### Rule 13

**Add complexity only when a real problem demands it.**

### Rule 14

**Research informs architecture; it does not dictate architecture.**

### Rule 15

**Prefer a small understandable system over a framework-shaped system.**

------------------------------------------------------------------------

# 36. Current Direction

The current goal is no longer to keep ideating indefinitely.

The architecture has reached a strong MVP foundation.

The immediate job is:

``` text
Harden
  ↓
Observe
  ↓
Evaluate
  ↓
Find real weaknesses
  ↓
Improve deliberately
```

Not:

``` text
Research another framework
  ↓
copy feature
  ↓
add abstraction
  ↓
repeat forever
```

If research produces no demonstrated Stella weakness:

> **Do not make a code change.**

That is a valid and preferred outcome.
