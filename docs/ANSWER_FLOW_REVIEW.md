# Answer Flow Review

This review records the `ANSWER` path and the later single-call latency change. It focuses on `Stella`, `LLMBrain`, `LLMClient`, `OpenAILLMClient`, `Context`, and their tests.

## Observed current flow

For the CLI's normal configuration, the components are composed as:

```text
OpenAILLMClient
  -> LLMBrain
  -> Stella
  -> CLI
```

For one non-exit CLI input, the actual flow is:

1. The CLI creates a `Context` containing the current user input and the session's conversation history.
2. `Stella.process()` retrieves matching memories from `Memory`.
3. Stella creates a derived `Context` containing the input, conversation history, and retrieved memories.
4. `LLMBrain.decide()` sends two `Message` objects to the configured `LLMClient`:
   - a `system` message containing the JSON decision protocol;
   - a `user` message containing JSON for the current input, conversation history, and retrieved memories.
5. `LLMBrain` parses the returned JSON into a `Decision`.
6. Stella performs one explicit memory write if the decision contains a `MemoryWriteRequest`.
7. For an ordinary `ANSWER` from `LLMBrain`, the structured `content` is the complete final response and becomes `StellaResult.response` directly.
8. If the content is missing, or if the answer follows tool observations, Stella creates a dedicated response request containing the current input, conversation history, retrieved memories, observations, and selected decision. That second call's string becomes `StellaResult.response`.

With `OpenAILLMClient`, each `LLMClient.chat()` call becomes one OpenAI chat-completions request. Therefore, an ordinary `ANSWER` interaction through `LLMBrain` now normally produces one provider request; tool-result answers and the missing-content fallback still produce two.

This does not apply identically to every path:

- `ASK`, `TOOL`, and `DO_NOTHING` use the decision call but do not make a second ordinary answer call. A single TOOL still makes a final synthesis call after execution.
- `SimpleBrain` does not promise that its `ANSWER` content is final. Its `ANSWER` path therefore retains the existing final LLM call.

## Why there are two calls

The abstractions assign two separate responsibilities:

- `LLMBrain` interprets context and chooses an action represented by `DecisionKind`.
- `Stella` executes that decision. For `ANSWER`, execution means asking `LLMClient` to generate response text.

The separation is visible in the code. `LLMBrain.decide()` returns a `Decision`; `Stella.process()` does not use `Decision.content` as the answer for an `ANSWER` decision. Instead, it calls `self.llm.chat(messages)` and stores that returned string as the response.

The revised decision protocol requires non-empty, complete final response content for `kind=answer` when returned by `LLMBrain`. `Brain.answer_content_is_final` makes this execution contract explicit; the base `Brain` remains conservative for other implementations.

## Information available to each call

### Decision call

The LLM used by `LLMBrain` receives:

- the system instruction describing the JSON decision protocol;
- the current user input;
- the complete session conversation history supplied in `Context`;
- retrieved memory contents supplied in `Context`.

It can therefore decide whether to answer, ask, invoke the tool, do nothing, or request an explicit memory write.

The decision call does not receive a tool registry or tool schema. It receives only the generic decision protocol and whatever context data `LLMBrain` serializes.

### Final answer call

The second call made by Stella for `ANSWER` receives:

- a system instruction that the `ANSWER` action is already selected and that only response text should be returned;
- the current user input;
- the original conversation history;
- retrieved memory contents;
- the selected decision, including its kind, content, arguments, and memory-write field.

It does not independently reinterpret whether the action should be answer, ask, tool, or do nothing. Stella only makes this call after the Brain has selected `ANSWER`.

The final answer call does not receive:

- the Brain's decision JSON protocol instruction. That protocol is only needed for the first decision call.

The final answer call now has the same decision-relevant context that led to the selected action, while remaining a response-generation call rather than a second decision call.

## Are both calls necessary?

They remain necessary for tool-result answers and for a missing-content fallback:

- the first call produces the `Decision`;
- the second call produces the response string that Stella returns.

They are not necessary for ordinary `LLMBrain` answers after the protocol change. The single-call path is safe because `LLMBrain` explicitly requires complete answer content and Stella only uses it for an already-selected ANSWER; it does not apply to tool-follow-up synthesis or arbitrary Brain implementations.

Removing the second call would not break the `ASK`, `TOOL`, or `DO_NOTHING` decision architecture, because those paths already stop after decision processing and their existing result fields do not depend on answer generation.

## Options

### A. Decision call plus separate answer call (previous ordinary-answer path)

This was the previous ordinary-answer design; it remains the design for
tool-result synthesis and the missing-content fallback.

Advantages:

- Keeps decision-making and response generation clearly separated.
- Lets Stella enforce the decision before any action occurs.
- Keeps tool execution and memory writing outside the LLM.
- Allows a future answer-generation implementation to be swapped independently.
- Fits the existing `Brain -> Decision -> Stella action` architecture.

Costs:

- Two provider calls for ordinary LLM-backed answers.
- Higher latency and cost.
- The two calls can disagree or produce inconsistent tone/content.
- The second call still incurs a separate provider request.
- The first call's answer `content`, if present, is ignored.

### B. One structured LLM call containing decision and answer

The protocol could make an answer response explicit, for example by adding a required `response` field when `kind` is `answer`:

```json
{
  "kind": "answer",
  "response": "The final answer text",
  "memory_write": null
}
```

Stella would parse the decision and use the same structured result as the final answer without another call. Ask decisions could carry their question text, tool decisions could carry arguments, and do-nothing decisions would carry neither.

Advantages:

- One provider call for an ordinary answer.
- The model sees the context once and can make the decision and answer consistently.
- Retrieved memories are available at the point where the answer is generated.
- Lower latency and cost.

Costs:

- The structured protocol becomes more demanding and must validate answer-specific fields.
- Decision selection and answer wording become coupled.
- Partial failures are harder to classify: a valid action with an invalid response, for example.
- Tool results are still produced outside the call, so a tool-follow-up answer would require another design.
- This changes the meaning of the current protocol and `Decision` contract; it is not a safe local optimization.

### C. Keep two stages, but make answer generation explicit and memory-aware (tool-result path)

This approach preserves the current decision boundary for paths that still need a second call. Stella passes the decision and retrieved memories to the answer call using an explicit prompt/message shape.

Advantages:

- Preserves the current separation between deciding and executing.
- Makes memory grounding explicit instead of incidental.
- Allows the second call to know what action was selected.
- Can be introduced without making `Decision.content` silently change meaning.

Costs:

- Retains the two-call latency and cost on those paths.
- Adds an answer-generation message contract.
- Still needs a later decision about whether two calls are worth the cost.

The implemented ordinary-answer path is a small version of this approach: the existing structured `content` field is declared final for `LLMBrain` answers. Tool-result synthesis remains two-stage because a tool result does not exist during the initial decision call.

## Recommendation

The implemented fix is a guarded form of option B for ordinary `LLMBrain` answers. The decision/action boundary remains intact, and only a parsed `ANSWER` decision with non-empty final content is returned directly. Tool execution and tool-result synthesis remain on the existing two-stage path. A future explicit response field could make the protocol even more self-documenting, but is not necessary for this MVP.

## Conclusion

The former two-call behavior was an intentional consequence of the architecture, not an accidental duplicate inside `OpenAILLMClient`. The current ordinary-answer path combines selection and answer content in one structured call, while tool-result answers still use the second synthesis call because the result is only available after execution. The remaining tradeoff is protocol strictness versus latency.
