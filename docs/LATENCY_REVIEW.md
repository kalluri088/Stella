# Stella Latency Review

## Scope and method

This review measures the current implementation without changing production
code. It covers the normal `LLMBrain` paths with the configured `gpt-5.6`
model, plus repeated deterministic runs to isolate local orchestration cost.

The live measurements used one representative run for each path, a temporary
workspace, and in-memory memory. Network and model scheduling make these
single-run values variable; they are useful for identifying bottlenecks, not as
stable service-level benchmarks.

The profiler timed:

- total `Stella.process()` wall time;
- every LLM client call, classified as decision or final synthesis;
- the Brain call, including local prompt construction and response parsing;
- memory retrieval; and
- dispatcher/tool execution.

## Measured live timings

| Path | Decision sequence | LLM calls | Total | Main call timings |
|---|---|---:|---:|---|
| ANSWER | `answer` | 2 | 4.070 s | decision 2.500 s; final 1.570 s |
| ASK | `ask` | 1 | 1.246 s | decision 1.245 s |
| Single TOOL | `filesystem_read` | 2 | 2.600 s | decision 1.331 s; final 1.268 s |
| 2-step TOOL | `filesystem_read` → `filesystem_write` → `answer` | 4 | 6.993 s | decisions 1.758 s + 1.915 s + 1.601 s; final 1.717 s |

The two-step workflow therefore makes four provider calls: one decision for
each of the two tool proposals, a third decision selecting `ANSWER`, and one
final response-generation call.

The live tool execution timings were:

- `filesystem_read`: approximately 0.639 ms;
- `filesystem_read` in the two-step workflow: approximately 0.379 ms;
- `filesystem_write`: approximately 0.502 ms.

The filesystem operations are negligible compared with the model calls. The
approval callback itself was local and did not materially affect the timings.

The live ASK request was explicitly phrased to require one clarifying
question. The model produced an `ASK` decision, so it made only the decision
call and no final synthesis call.

## Local orchestration timings

Repeated deterministic runs (200 per path) used a fixed Brain and a no-op LLM
to remove network variance. These numbers describe local control-flow cost,
not real model latency:

| Path | Mean `process()` time | Median | Memory retrieval mean | Tool dispatch mean |
|---|---:|---:|---:|---:|
| ANSWER | 0.0133 ms | 0.0123 ms | 0.00043 ms | — |
| ASK | 0.0055 ms | 0.0052 ms | 0.00034 ms | — |
| Single TOOL | 0.0257 ms | 0.0213 ms | 0.00042 ms | 0.0071 ms |
| 2-step TOOL | 0.0228 ms | 0.0221 ms | 0.00038 ms | 0.0060 ms |

These fixed-Brain measurements intentionally exclude a real decision-model
call. They show that Python orchestration, lexical memory retrieval, JSON
prompt construction, validation, approval checks, and local tool dispatch are
not the current bottleneck.

## Where time is spent

### Normal ANSWER path

The normal OpenAI-backed ANSWER path currently requires two LLM calls:

1. `LLMBrain.decide()` sends the decision protocol, available tools, current
   context, conversation history, and memories. It returns a structured
   `Decision`.
2. `Stella` sends a second request for final natural-language response text.

The first call is required by the current separation between deciding an
action and generating the final answer. `Decision.content` is not defined as
the final response, so Stella cannot safely display it instead. The live
measurement shows that the two provider calls account for essentially all of
the approximately 4.07 seconds.

The local work around the decision call was about 0.12 ms in the live sample,
estimated as Brain time minus the underlying decision LLM call. This includes
JSON context construction and decision parsing. It is negligible next to the
model request.

### ASK

ASK requires only the decision call because Stella returns the Brain's
clarifying content directly. Its latency is therefore approximately one model
call, with no final synthesis.

### Single TOOL

The single-tool path makes one decision call, executes the tool, and makes one
final synthesis call. The tool and dispatcher took less than 1 ms in the live
sample; the two LLM calls dominated the approximately 2.60 seconds.

### 2-step TOOL workflow

The multi-step path adds one decision call after each tool result. For two tool
steps, the current implementation made three decision calls before the final
synthesis call. This is expected from the current sequential architecture,
but it makes multi-step workflows approximately additive in provider latency.

## Current bottlenecks

1. Provider round trips dominate every LLM-backed path.
2. The ANSWER path pays for separate decision and response-generation calls.
3. Each additional tool step adds another decision-model round trip.
4. Prompt size can grow with conversation history, retrieved memories, and
   accumulated tool observations, which may increase provider processing time
   and token cost even though local serialization is cheap.

Memory retrieval, capability lookup, argument validation, risk checks,
approval construction, audit record creation, and the current local tools are
not significant latency contributors in these measurements.

## Safe opportunities now

These changes could be considered without weakening the current security
boundary, although they are not implemented in this task:

- measure token counts and provider response times over multiple runs;
- avoid redundant local serialization or repeated static prompt construction;
- keep tool observations and conversation history bounded through an explicit
  context policy, once the product defines the policy;
- use a faster or less expensive model specifically for decision calls, if its
  structured-output reliability is established;
- use provider-supported connection reuse, request timeouts, and retry policy
  deliberately at the client boundary; and
- expose per-step timings through the existing inspection/audit path rather
  than adding a new logging framework.

None of these should let the model bypass dispatcher lookup, validation, risk,
approval, or auditing.

## Optimizations that should wait

- A broader collapse of tool-result decision and final response into one call
  should wait; the ordinary-answer case is now implemented, but tool results
  do not exist until after execution.
- Parallel tool execution would complicate ordering, approval, audit records,
  and tool-result context, and is not justified by the current MVP.
- Automatic retries or speculative calls would increase side effects and make
  approval semantics harder to reason about.
- Streaming, caching model decisions, or caching tool results needs clear
  consistency and safety rules first.
- Aggressive memory summarization or retrieval changes should wait until their
  effect on correctness and persisted memory behavior is measured.

## Single-call ANSWER experiment

The proposed experiment reused the existing `LLMBrain` decision call and
treated a parsed `answer.content` as the final response, without changing the
repository during the live comparison. Three ordinary prompts were sampled:

| Prompt class | Decision | Content usable as final response | Calls in experiment |
|---|---|---|---:|
| Capital of France | `answer` | yes | 1 |
| Bounded loops | `answer` | yes | 1 |
| Arithmetic | `answer` | yes | 1 |

For the capital-of-France prompt, the existing path took 2 calls and about
2.900 s of provider time; the one-call experiment took 1 call and about
1.693 s. The decision content was a complete natural-language answer in both
the existing comparison and the experiment. The other two experiment calls
took about 1.757 s and 1.137 s respectively and also returned non-empty,
usable answer content.

The experiment did not alter tool behavior. The implementation is guarded so
only `LLMBrain` answers with non-empty content and no prior tool observations
use the one-call response. ASK, DO_NOTHING, tool execution, tool-result
synthesis, and multi-step answers retain their existing behavior. Explicit
memory writes still occur from the parsed decision before the response is
returned.

## Implemented change

`Brain.answer_content_is_final` is a small execution contract. It defaults to
false for arbitrary Brain implementations and is true for `LLMBrain`, whose
protocol now requires complete final response text in `content` for an answer.
Stella returns that content directly for an ordinary answer. If it is absent,
Stella retains a safe fallback to the old final-synthesis call. When tool
observations exist, Stella also retains final synthesis so the response can
use the actual ToolResult.

This is a latency experiment converted into a narrowly scoped implementation,
not a general reduction of LLM calls. The live sample indicates roughly one
provider round trip is removed from ordinary answers; provider variance means
these are indicative measurements rather than a performance guarantee.

## Conclusions

The current latency problem is primarily provider round trips, not Python
orchestration. Ordinary LLM-backed answers now use one structured call while
tool-result and multi-step synthesis still pay for their required follow-up
call. The decision/action boundary remains trusted-runtime controlled, and a
missing answer content falls back to the previous synthesis behavior.

Validation performed during this review:

- Pytest: 175 passed
- Ruff: all checks passed
