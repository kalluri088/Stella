# Stella Architecture Review

This review describes the implementation currently in the repository. It separates observed behavior from recommendations; the recommendations are not architectural changes being made by this review.

## Scope inspected

The review covered the complete architecture document, every module under `src/stella`, and every test under `tests`:

- `src/stella/__init__.py`
- `src/stella/brain.py`
- `src/stella/cli.py`
- `src/stella/context.py`
- `src/stella/llm.py`
- `src/stella/memory.py`
- `src/stella/openai_client.py`
- `src/stella/stella.py`
- `src/stella/tools.py`
- `tests/test_brain.py`
- `tests/test_cli.py`
- `tests/test_context.py`
- `tests/test_llm.py`
- `tests/test_memory.py`
- `tests/test_openai_client.py`
- `tests/test_package.py`
- `tests/test_stella.py`
- `tests/test_tools.py`

The existing `docs/ARCHITECTURE.md` was also checked against the code. Its documented component boundaries and runtime flow are factually consistent with the implementation after the test-count update to 49 tests.

## Observed architecture

The current runtime path is:

```text
CLI input
  -> Context
  -> Stella retrieves Memory
  -> Brain produces Decision
  -> optional explicit memory write
  -> LLM answer or Tool execution
  -> StellaResult
  -> CLI output
```

The CLI composes `OpenAILLMClient`, `LLMBrain`, `SQLiteMemory`, and `EchoTool`. The core `Stella` class receives abstractions explicitly and does not know which LLM or memory implementation is used.

For an answer using `LLMBrain`, there are two LLM calls: the first asks the Brain for a structured decision, and the second is made by `Stella` to produce the answer. This is current behavior, not an inference.

## Findings

### 1. Dependency direction

The dependency direction is mostly clear and acyclic:

- `llm.py`, `memory.py`, and `tools.py` define low-level interfaces and data types.
- `context.py` combines `Message` and `MemoryItem` as data carried through the system.
- `brain.py` depends on `Context`, `LLMClient`, and memory-write types.
- `stella.py` coordinates Brain, LLM, Memory, Tool, and Context.
- `cli.py` is the composition root and is the only place that selects OpenAI, SQLite, and the example tool for normal use.

This is a suitable direction for the MVP. `Context` and `Decision` intentionally use shared domain data types, so they are not completely independent layers.

Recommendation: keep the current direction. Do not introduce a service container or additional indirection until there is a concrete composition problem.

### 2. Separation of responsibilities

The boundaries are generally sound:

- `LLMClient` sends messages and returns text.
- `LLMBrain` turns LLM text into a decision.
- `Memory` stores and retrieves memory items.
- `Stella` coordinates retrieval, explicit writes, decisions, LLM calls, and tool calls.
- `Tool` performs one bounded operation.
- The CLI collects input, maintains session history, and displays results.

`Stella` is the busiest component, but that is currently its intended role as the single-pass coordinator. It is not yet doing planning, persistence policy, or event handling.

The main future pressure point is that `Stella` currently owns both the action dispatch and the memory-write side effect. That is acceptable while the flow has four decision kinds and one tool, but it will become harder to read if more actions are added.

### 3. Provider independence

The core decision and orchestration path depends on `LLMClient`, not on OpenAI. `LLMBrain` also depends only on `LLMClient`. `OpenAILLMClient` is isolated in the provider adapter and selected by the CLI.

The provider-independent boundary is therefore real, although the CLI is intentionally OpenAI-specific today. The LLM decision protocol is application-owned JSON, which keeps the Brain interface independent of a provider's native response format.

Current limitation: `LLMClient` accepts either `Message` objects or ordinary dictionaries, so callers can bypass the typed message representation. This preserves compatibility but weakens the type boundary.

### 4. Memory boundaries

Memory is explicitly read before decision-making and written only when a `Decision` contains a `MemoryWriteRequest`. Neither implementation automatically stores user messages, assistant responses, or retrieved items.

`InMemoryMemory` is useful for deterministic tests. `SQLiteMemory` uses one minimal table and persists only `MemoryItem.content`. The CLI closes its SQLite connection when the session ends. Conversation history remains an in-process CLI concern and is not persisted.

The current retrieval operation uses a shared deterministic keyword-overlap matcher. It tokenizes text case-insensitively, ignores common stop words, and requires two shared meaningful terms for a multi-term query or the one available term for a single-term query. This fixes the observed failure where “What is my favorite color?” did not match “The user's favorite color is cobalt blue.” It remains only lexical matching, not semantic relevance: it does not understand synonyms, word forms, or context.

The memory abstraction does not expose lifecycle methods, so SQLite-specific resource management exists outside the interface. That is reasonable for now, but callers using SQLite directly must remember to call `close()` or use a context manager.

### 5. Brain and LLM boundaries

The Brain decides; it does not execute tools or write memory. `LLMBrain` parses a strict JSON protocol and safely returns `DO_NOTHING` for malformed output. This is a good safety boundary for the current stage.

The important behavioral distinction is that a natural-language answer such as “I will remember that” has no memory effect. Only the structured `memory_write` field becomes a `MemoryWriteRequest`. The debug mode exists specifically to inspect this distinction.

The current two-call answer path is the largest Brain/LLM design concern. The first call proposes `answer`, and the second call generates the answer. This is explicit and testable, but it adds latency and cost and means the decision response's `content` is not used as the final answer for an answer decision.

The JSON protocol also has no version, schema identifier, or detailed validation. Invalid output safely becomes `DO_NOTHING`, but the failure is silent unless debug inspection is enabled.

### 6. Tool execution boundaries

The LLM never directly executes code, shell commands, or an API call. It proposes a `TOOL` decision; `Stella` invokes the one injected `Tool` with the parsed arguments.

The boundary is appropriate for the MVP, but it is currently intentionally narrow:

- There is only one configured tool.
- A decision does not contain a tool name.
- Tool arguments are an unconstrained dictionary.
- There is no schema validation before execution.
- `EchoTool` expects a `message` key and can raise `KeyError` for incompatible arguments.

These are known scaffolding limitations, not reasons to add a registry or permissions framework now.

### 7. Is Stella doing too much or too little?

For the current scope, Stella is doing the right kind of work: it is the single place that sequences retrieval, decision, explicit memory write, LLM response, and tool execution. The class is small enough to follow and its tests cover each decision branch.

It is doing slightly too much if judged as a long-term design. Its constructor requires a Brain, LLM, Tool, and Memory even when a particular path does not use all of them. It also has direct branches for every action. That is not a problem at the current MVP size, but it will become a maintenance concern as actions multiply.

It is doing too little to be a complete assistant: it does not provide multi-tool selection, retry behavior, error policy, response composition after tools, or persistent conversation history. Those omissions are intentional and documented.

### 8. Abstractions that are currently scaffolding

The following are useful scaffolding rather than demonstrated product capability:

- `SimpleBrain`, which makes decision paths deterministic through prefixes.
- `FakeLLMClient` and test recording clients.
- `EchoTool`, which proves the tool boundary but has no real-world utility.
- The generic `Tool` abstraction with only one configured tool.
- The JSON decision protocol, which is a narrow bridge until a stronger decision contract is justified.
- `MemoryWriteResult`, which makes the explicit write visible but currently carries only a boolean and item.
- The CLI debug flag, which is valuable for diagnosis but is not user-facing functionality.

The ABC interfaces are not unnecessary at this point: they enable deterministic tests and provider substitution with little code. They should remain small.

### 9. Future evolution risks

The current implementation may make these future changes more difficult:

1. `Decision` has several optional fields that can be combined inconsistently, such as tool arguments on an answer or a memory write on a do-nothing decision. There are no invariants beyond parser checks.
2. `Context` is mutable and contains lists. Stella avoids mutating the caller's context, but the data can still be changed by callers after construction.
3. The one-tool constructor and lack of a tool name will require a deliberate API change for multiple tools.
4. The simple keyword matcher is still coupled to lexical overlap and will not cover synonyms, word forms, or richer memory queries.
5. SQLite has no timestamps, source, user/session scope, schema version, or migration mechanism. Adding those later will require a schema decision.
6. The CLI persists memory but not conversation history, so a restarted process can recall explicit facts but cannot continue the prior chat transcript.
7. `Stella` writes memory before executing the selected answer/tool action. If the later action fails, the memory write remains. There is no transaction spanning an interaction.
8. The CLI's composition root selects one concrete provider, storage backend, and example tool. This is appropriate now, but configuration will need a clearer boundary if more providers or tools are supported.

These are design constraints to remember, not changes recommended for this review.

### 10. Test coverage

The suite currently has 49 passing tests and covers:

- All four SimpleBrain decisions.
- All four parsed LLMBrain decision kinds.
- Explicit memory-write parsing and execution.
- Malformed LLM output fallback.
- In-memory and temporary SQLite storage, matching, and persistence across instances.
- The full LLMBrain → Stella → SQLiteMemory memory-write path.
- A cross-instance SQLite lifecycle in which persisted memory changes a later Brain decision and answer.
- Final answer generation receiving retrieved memories and the selected decision.
- All Stella action branches.
- CLI input, output, history, exit, and debug inspection.
- OpenAI client construction and request translation without network calls.

The most notable remaining coverage gaps are:

- The CLI environment factory with a temporary `STELLA_MEMORY_DB` path and mocked OpenAI client.
- A full LLMBrain → Stella → Tool path.
- Explicit assertions around SQLite behavior when a database path is invalid or a connection is closed.
- A multi-turn LLMBrain integration test proving that the history serialized for the second decision contains the first turn.

These are useful future tests, but none is required to understand the current architecture or to justify a production refactor now.

## Smallest useful Stella system today

The smallest useful core is:

1. `Context` and `Message` to represent one interaction.
2. A `Brain` that returns a `Decision`.
3. An `LLMClient` to produce an answer for an answer decision.
4. `Stella` to connect the decision to the LLM.

Adding `Memory` and explicit memory writes makes the system materially different from a stateless chatbot, but is not required for the smallest answer-only loop. `Tool` and the CLI are useful integration surfaces, not prerequisites for that core loop.

## What is genuinely necessary

For the current intended product direction, the necessary concepts are:

- A typed interaction/context representation.
- A provider-independent LLM boundary.
- A decision boundary separate from answer generation.
- An orchestrator that owns action sequencing.
- Explicit, testable memory read/write behavior.
- A persistent local store if memory must survive process restarts.

The concrete OpenAI adapter, SQLite adapter, and CLI are replaceable implementations of those boundaries rather than the core concepts themselves.

## What is scaffolding

The deterministic Brain, fake clients, EchoTool, single-tool slot, JSON protocol, CLI debug mode, and simple substring retrieval are scaffolding around the central experiment. They make the system testable and observable, but they do not yet demonstrate broad assistant capability.

## What should not be built yet

The current evidence does not justify embeddings, vector databases, semantic ranking, an agent framework, autonomous loops, event systems, multi-user identity, tool permissions, background jobs, or a large tool registry. It also does not justify persisting full conversation history until the desired session and privacy behavior is clear.

## Recommended next capability

The most important next capability identified in the previous review has now been proven: persistent memory changes a later decision and answer across separate Stella/SQLiteMemory lifecycles.

The proof is deterministic and narrow:

1. Explicitly store a fact in SQLite.
2. Start a new Stella instance.
3. Submit a related question.
4. Verify that the retrieved fact reaches the Brain and changes the resulting answer or decision.

The test proves the defining difference from a normal chatbot: Stella can deliberately retain and reuse user-specific information across sessions. It also confirms that the current simple substring retrieval is the mechanism involved; it does not justify semantic retrieval yet. The next experiment should focus on whether the current query shape is useful for real conversations, still using deterministic tests before considering any retrieval upgrade.

## Review conclusion

The current implementation is a coherent, deliberately small foundation. Its boundaries are real, its persistent memory behavior is explicit, and the test suite validates the main branches plus cross-instance memory reuse and memory-aware final answer generation. The main architectural risks are the growing responsibility of `Stella`, the latency and cost of the two-call LLM answer path, the single-tool assumption, and the intentionally weak retrieval/decision schemas.

No production refactor is warranted by this review. The next development step should be a memory-grounded response experiment, not a larger framework or more abstractions.
