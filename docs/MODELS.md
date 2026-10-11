# Agreed model stack (decided 2026-09-27)

A registry of the model choices agreed from measured research (research
reports 05–23). These are decisions, not open questions: do not
re-litigate them without new evidence, and do not silently swap any of
them. A swap requires the same production path, the same 30-case
decision corpus, and 6 passes per case before it can ship.

## Decision brain

- **Shipped:** `qwen3:4b` Thinking-2507 through Ollama (system
  service), `num_ctx=8192`, temperature 0, per-call-kind output-token
  caps 8192 decision / 2048 answer (report 45). `STELLA_OLLAMA_THINK`
  is a tri-state knob that sends nothing by default: measured on this
  line, `think:false` was slower and answered with ~1 kB of prose
  ramble per decision — the reasoning channel stays (report 26's
  kill-gate doctrine, now with provider-line evidence).
  It wins because it scores
  28/30 strict decisions on the production path, its quality comes
  from its reasoning channel, and Ollama beat a self-run llama.cpp
  line on identical weights (report 20).
- **Fallback / architectural option only:** `gpt-oss-20b-mxfp4` via
  `llama-server -ngl 99 -cmoe -c 8192`. Best raw quality, but
  context-starved on this machine's 6 GB VRAM. It stays a self-owned
  process option, not the default.
- **Rejected as brain:** LFM2.5-8B-A1B (12/30; fails reminder-argument
  construction), `qwen3.5:4b` (faster but never reached 9/30 passes,
  and leakier injection behavior), and no-thinking variants of
  `qwen3:4b` — deleting the reasoning channel is a downgrade, not a
  speedup.

### The stable part of the prompt

A turn's decision prompt has two halves, and the capability registry now
lives in exactly one of them. The system prompt — persona, the rules, the
routing check and the reply-format instruction — is 4,096 tokens and no
longer carries a copy of the tool list; the registry travels once, as the
provider's native `tools` array: 1,176 tokens for the shipped fifteen
capabilities, 2,139 once the OS, shell and browser families are
registered. Until #229 both copies went out on every turn — the same
`ToolDispatcher.describe()` rendered into the prompt as prose **and**
handed to the provider as the array — which made the system prompt 5,301
tokens (6,275 with the families on) to say nothing the array had not
already said. A client that cannot put the array on the wire keeps the
prose list instead; the Brain asks the client which it is (`LLMClient
.declares_tools_natively`) and never sends both.

Reading all of it from nothing costs ~3.0 s on this machine;
reading it again from a server that cached the prefix costs ~0.07 s. That
cache is the whole difference between a turn that starts instantly and one
that stalls, and it keys on the *beginning* of the prompt — change one
early byte and everything after it is re-read.

So the rule is that nothing a real next turn changes may live in the
system prompt. The clock used to: a minute-granular timestamp closed the
system prompt, and because Ollama serializes the tool array after it, every
new minute re-read the tools and the tail — measured 1.03 s for a turn whose
only difference was the minute. The clock now travels in the request payload
with the user's words, the conversation history and the retrieved memories,
which are already different every turn and sit after the cached prefix; the
same next-minute request costs 0.11 s. The invariant is pinned by
`tests/test_brain.py`.

Dropping the prose registry moved the same rule in a second direction: the
system prompt is now 4,096 tokens whether the OS, shell and browser
families are registered or not, because the only text that changed when a
family was toggled *was* the registry copy. Toggling a capability no
longer re-reads the prefix.

This says nothing about *what* the model is told — the runtime reference is
still context data and never authority, and the datetime tool is still the
only way Stella answers "what time is it".

### Where a turn's seconds actually go

Measured with real `qwen3:4b` on Ollama through the production client, timing
every provider request a turn makes and counting the tokens the provider
itself reported: **a trivial turn is exactly one request.** Not a
plan-then-answer pair, not a retry — one call whose whole cost is decoding.

The prompt work above did its job. Prefill is cached and contributes about
0.07 s, so #229 could never have been a latency fix — it could recover at
most the ~0.9 s of a *cold* prefix and cannot fix a turn that already reads
warm. (It turned out to be a *capacity* fix; see *Token accounting* below.)
Everything else is decode, at roughly 44 tokens per second on this card. Two
arms of the same script, the same resident model, the same two questions:

| Question | Thinking tokens decoded | Wall clock |
| --- | --- | --- |
| "what is 2 plus 2?" | 272 – 301 | 10.4 – 11.0 s |
| "who is the prime minister of India?" | 1,385 – 2,956 | 31.4 – 67.6 s |

So the honest description of tonight's latency is not "16.8 seconds" but
**unbounded and unpredictable**: the same factual question cost 31 s once and
68 s later, because how long Stella thinks before answering is the model's
choice, and the user watches a spinner the entire time.

The output-token cap is *not* the lever people would hope. The default is 8192
because a legitimate decision is allowed to carry a 20k-character Outline body
(~5k tokens), so the cap is a runaway backstop that never binds on honest work —
the two turns above finished far under it. Sweeping it down neither sped turns
up reliably nor ended them safely. How much a model thinks is stochastic, so a
tighter cap can cost time: the trivial question answered in 10.4 s at 8192 took
18.7 s at 1024, where the model decoded 820 tokens of thinking instead of 272.
And whenever the cap actually bound — the token count landing exactly on 1024,
exactly on 512 — the decision was cut off mid-thought, no JSON arrived, and the
turn ended with **no words at all** while reporting success.
Deleting the channel is worse still: `think:false` measured slower and rambled
(reported above), which is why the reasoning stays.

What the measurement does establish, and what was fixed: an unreadable
decision is not a `do_nothing`, and it no longer gets to be silent
(`docs/ARCHITECTURE.md`, `Decision.unreadable`). Of the two candidates that
this left, streaming the answer (#230) has now been measured and declined —
see *The tail a stream could have handed over* below — so the one still open
is a faster brain for the turns that need no reasoning at all (#237), not a
smaller budget. That one has now been measured too, and the speed is real;
what remains open about it is the owner's decision, not the number (see *The
faster brain, measured on the turns it would have to answer*).

This table is also why the turn budget that shipped (#231) stops the loop
*between* steps rather than inside a call. A 90-second bound that could only
act at safe points would be a lie if a single call can burn 68 of its 90
seconds: the runtime cannot cancel a decode it has already asked for, and
aborting one mid-flight would throw away the answer that is arriving. So the
clock decides whether another step begins, a call already in flight is read to
the end, and a turn may therefore finish later than the number in Settings.
That is written into the Settings hint and `docs/ARCHITECTURE.md`
(*Turn budgets*) so the figure never promises a deadline it does not keep.

### The tail a stream could have handed over (#230, measured and declined)

Streaming the answer was the last latency idea that the table above did not
already kill, so it was measured before anything was built. A real `LLMBrain`
over the shipped 15-tool set produced the exact decision prompt of ten turns;
those bytes were replayed against the resident `qwen3:4b` with `"stream": true`
and every Ollama delta timed against the request, with the same bytes
unstreamed as a control. The spans, in seconds:

| Turn | decision | reply chars | thinking chars | first JSON byte | first reply char | last delta | the tail | content deltas |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| "what is 2 plus 2" | answer | 18 | 1,410 | 6.57 | 6.77 | 6.94 | **0.16** | 19 |
| prime minister of India | do_nothing | 0 | 5,920 | 28.04 | — | 28.19 | — | 8 |
| why the sky is blue | answer | 277 | 2,810 | 12.81 | 13.02 | 14.14 | **1.12** | 65 |
| explain a KV cache | answer | 820 | 7,717 | 33.25 | 33.47 | 36.42 | **2.95** | 147 |
| what to pack for Kyoto | answer | 241 | 4,674 | 21.50 | 21.71 | 22.73 | **1.02** | 59 |
| what do you remember | tool | 0 | 6,468 | 28.64 | — | 29.01 | — | 18 |
| summarise a file read | answer | 43 | 1,105 | 5.87 | 6.08 | 6.28 | **0.21** | 21 |
| how are you today | answer | 34 | 2,229 | 10.40 | 10.60 | 10.79 | **0.19** | 20 |
| what time is it | tool (native call) | 0 | 2,507 | — | — | — | — | 0 |
| remember that Ada… | tool | 0 | 8,299 | 40.80 | — | 41.48 | — | 32 |

**The reply is the last 5% of the wait.** The first byte of the decision JSON
arrives at a median of 95% of the turn's total time, because everything before
it is reasoning being decoded — the span #243 measured and the span no
transport can shorten. `tail` is the whole of what streaming could have handed
to the user early, and its median is **0.62 s**. The turns a spoken
conversation actually makes (the arithmetic, the greeting, the summary of a
read) have a tail of 0.16 – 0.21 s; only the 820-character reply crossed 2 s.

The gate was written before the run: build the streaming path only at a median
tail of **≥ 2.0 s**, and — the voice form of the same question — a first-audio
gain of **≥ 0.8 s**. Both failed. The first sentence of a short reply ends
within about a tenth of a second of the closing brace, so starting to
synthesise it against the rest of the decode buys nothing: subtracting the
measured render cost of that first chunk (0.03 s per character) from the decode
it would have overlapped gives a median gain of **−1.34 s** across the five
turns whose reply contains a sentence boundary, and not one of them is
positive. There was no code to write, so none was written.

What the run did settle, and what a future reader should not re-measure:

- **The transport is not the obstacle.** Deltas are genuinely incremental on
  Ollama 0.33.3 — 147 of them on the longest reply, arriving every ~20 ms — and
  streaming carried no systematic cost over the unstreamed control (paired
  difference median +0.41 s, with both large negatives and one +29.9 s outlier
  that is thinking variance, not the transport).
- **A safe tap is possible; a useful one is not.** On every answer turn the
  JSON arrived with `kind` before `content`, so a reader really can know the
  reply is an answer before it shows a character of it, which was the
  precondition for ever emitting streamed text. The mechanism works and the
  prize is missing.
- **A `tool` decision has no text to stream.** The native tool-call channel
  carried zero content deltas, and the wait it belongs to is the next step's.
- **Separating decide from answer — the design `docs/ROADMAP.md` names as the
  condition for re-opening streaming — was measured and does not pay.** A third
  arm fired a plain prose-only call after the decision completed. Its first
  character landed *later* than today's entire reply on every turn, and the
  reply finished at a median 33.0 s against the fast path's 18.5 s: a second
  call re-decodes the same text, so the beginning moves barely and the end
  moves a lot.

Honest limits: this replays decision prompts through the production client's
wire format rather than running a full Stella process, so no audio was
synthesised and the first-audio figure is arithmetic on measured character
timings, not a listening test. Ten turns settle a 0.6-versus-2.0-second gate
and would not be enough to rank two designs against each other. And the
`thinking` text Ollama sends alongside the decision stays dropped: it is the
cost of a turn rather than a way to hide it, its only speed-adjacent use would
be speaking untrusted model prose, and with no streaming reader there is
nothing to attach it to.

### The faster brain, measured on the turns it would have to answer (#237)

This is the one latency lever the table above leaves standing, and as of
2026-10-11 it can finally be run: `stella doctor` (#240) had been reporting the
dead `localhost:3001`, the owner brought the router container up, and the A/B
went through the **production decision path** — `build_application` then
`application.session.run_turn(text)` — rather than replaying prompts at a
client. That distinction is the whole point: each turn really built the brain,
really chose whether to call a capability, really executed it and really
produced the words she would have heard.

Six questions, chosen to cover both halves of a decision: three that must be
answered without touching a tool (arithmetic, a one-sentence explanation, a
current-affairs fact) and three that must not be answered without one (the time
→ `datetime`, CPU count → `system_info`, the third line of a fixture file →
`filesystem_read`). A turn counts as passed when the final decision kind is the
one expected *and* the expected capability actually fired *and* some words came
out. Wall clock is the turn's own `duration_seconds`.

| Brain | answered correctly | median | worst | how it failed |
| --- | --- | ---: | ---: | --- |
| local `qwen3:4b` (incumbent) | 6/6 | 22.27 s | 59.02 s | — |
| router, `auto` | 6/6 | **3.20 s** | 7.46 s | — |
| router, `gemini-3.1-flash-lite` | 6/6 | 7.79 s | 10.63 s | — |
| router, `gpt-oss-20b` | 0/6 | — | — | `413` on every turn: too large for every candidate's context window |
| router, `qwen3.7-flash` | 0/6 | — | — | `429` "All models exhausted: 1 route checked (1 rate-limited or on cooldown)" |

**The speed is real and it is roughly sevenfold at the median** — 3.20 s against
22.27 s on the same six questions, with no routing loss: the `auto` arm called
`datetime`, `system_info` and `filesystem_read` exactly when it should and
answered the three direct questions directly. The incumbent reproduced what
this file has been saying all along: the same trivial turn cost 11.7 s once and
59.0 s later, because the length of the thinking channel is the model's choice.

**And the tail is rarer, not gone.** An earlier run of the same `auto` arm, same
question set, put one `system_info` turn at **63.98 s** — longer than anything
the local brain managed. Two dead arms also failed *all six* turns, and both
failures are the router's, not Stella's: the shipped decision prompt does not
fit every candidate's window, and one route can be rate-limited with nothing
behind it. A brain whose speed depends on someone else's availability is a
different trade from a slow one that is always there. Both of them ended with
words naming the cause rather than silence, which is #244 doing its job.

Honest limits, stated before any conclusion: **n=6, one machine, one evening.**
Two earlier outputs of the same script are in `~/bench-out/` and are *not* these
numbers — one crashed on `StellaApplication.run_turn`, and one carried a judge
that called every turn a tool call because it counted `step_trace` entries
instead of their decision kind. Only the paired run is quoted, and six turns
would not be enough to rank two brains against each other the way ten turns
settled the streaming gate. What the run does settle is cheaper and more useful
than a ranking: the router is reachable, it answers this prompt correctly, and
it answers it several times faster.

**What this deliberately does not do is switch anything.** The router is a
container on this machine whose candidates are models this machine does not
run, so a turn through it sends the owner's words — and whatever the tool
results that follow them put in the prompt — to whoever serves them. That is her
decision under the vision's tier two (written or sent to a hosted service is
done visibly and audited), and it is not a default a benchmark gets to flip.
So #237's measurement half is closed here and its policy half stays open, in
Settings, where the owner picks a provider and a model per launch. Bench script
`~/tools/bench_router_ab.py`, raw JSON `~/bench-out/router_ab_*.json` — outside
the tree, with the other benches.

## Token accounting (`/usage`)

Every provider client can carry one `UsageRecorder` (`stella.llm`), and
`build_application` attaches exactly one per session. Each *completed*
request tallies the counts the provider itself reported: Ollama's native
`/api/chat` `prompt_eval_count` / `eval_count`, and the OpenAI-shaped
`usage` block on every other path (compatibility endpoint, llama-server,
OpenAI, the chat-dialect presets, the Responses API). `/usage` prints the
call count, tokens in and out, and the largest single prompt this session
sent — that last number is the one to watch against `num_ctx=8192`, since
a clipped prompt was measured as a real failure mode here (report 15).

Measured on this machine on 2026-10-10 with the local brain, that number is
not comfortable: **6,191 tokens** with the desktop capability families off
and **8,151** with OS, shell and browser tools on — 41 tokens of headroom
before the ceiling. One recalled memory costs 58. So a turn that *finds
something in memory* is the turn that can overflow, and Ollama answers it
with `exceed_context_size_error` and nothing else. The client now repeats
that reason (`HTTP 400 (request (8209 tokens) exceeds the available context
size (8192 tokens) …)`, bounded to 220 characters of the server's own
body); before that it said only `HTTP 400`, which named no cause at all.

The ceiling was a choice, and the cheaper side of it has now been taken
(#236's follow-up, closed 2026-10-11): **the prompt got smaller**, `num_ctx`
did not move. What was removed was not a trim — it was a *duplicate*.
`ToolDispatcher.describe()` was being transmitted twice on every turn, once
rendered into the system prompt as a "Currently available tools:" block and
once as the provider's native `tools` array. Both copies came from the one
source, so one of them carried no information at all. The routing rules then
pointed at the prose copy ("one of the currently available tools listed
below", "requires one of the tools listed immediately above"); those two
phrases now name "the capabilities currently available to you" and reach the
model whether or not the prose list exists.

Measured with the same ~4-chars-per-token estimator the reports above use:

| Tool set | prose registry | system prompt, both copies | system prompt, one copy |
| --- | ---: | ---: | ---: |
| shipped (15 capabilities) | 1,198 tok | 5,301 tok | **4,096 tok** |
| + OS, shell, browser (22) | 2,172 tok | 6,275 tok | **4,096 tok** |

Applying that to the two readings the owner's session produced predicts
**8,151 → ~5,979** with the families on and **6,191 → ~4,993** without them —
which puts the recall turn that used to overflow back inside `num_ctx=8192`
with room for the memory it found. The prediction was then checked on the
wire: two real CLI sessions of one shape (store a fact, then ask the question
that retrieves it), from this tree with only the client's promise flipped,
`num_ctx=8192` untouched. **7,757 tokens before, 5,949 after — 1,808 real
tokens gone**, and the retrieval turn answered in both arms. Those sessions
register fewer capabilities and no persona file, so they read smaller than
the owner's 8,151 on both sides; what carries across is the saving, which the
provider measured. The estimator over-states it, as any ~4-chars-per-token
figure does.

A second, unplanned benefit: with the block gone, the cached prefix no
longer changes when a capability family is toggled — the system prompt is
4,096 tokens either way, so registering the desktop tools mid-session stops
costing a full re-read.

A client whose transport cannot carry the `tools` array keeps the prose list,
because for it the text protocol is the only thing naming the capabilities;
the Brain asks the client (`LLMClient.declares_tools_natively`) rather than
guessing from its class.

What it does *not* change: `_SYSTEM_PROMPT` itself is now the big fixed cost
(~4,100 tokens of rules, persona and reply format on top of whatever the
request carries), and cutting it would be a separate decision with its own
evidence requirement, not a saving smuggled into this one.

### The routing A/B that let this ship

A shorter prompt is only a win if the model still knows what it can do. The
base capabilities are *also* described in the rules prose, but the desktop,
shell, browser, web and Outline families are described **only** by the
registry — so deleting the prose copy could, in principle, delete the model's
whole idea that `screen_read` or `shell_run` exists. That asymmetry is the
reason this change was gated on a real-model run rather than on unit tests.

17 tool-dependent turns, at least one per enabled family, run twice per arm
against real `qwen3:4b` on the local Ollama through the production native
client, 24 capabilities registered, a fresh temp memory/history/workspace in
each run. Both arms come from this one tree: the only thing that changes is
the client's `declares_tools_natively`, so the prose-copy arm is exactly the
request Stella used to send.

| Arm | decisions | turns whose capability differed between the arms |
| --- | --- | --- |
| declared (ships today) | 33 tool, 1 answer | — |
| prose-copy (before #229) | 34 tool | 2 |

The two disagreements were not wins for the old behaviour:

- `network_read` ("what does that page say?"): the prose-copy arm answered
  `web_fetch` on **both** passes, the declared arm `network_read` on both.
  The duplicate was already mis-leading the model; removing it fixed it.
- `memory_list`: the declared arm answered instead of calling once, in pass
  two only. Replaying that single turn five times per arm gave
  `tool:memory_list` **5/5 in both arms** — it is qwen3's own variance, not
  an arm effect, and it is why the ship gate is written as "no arm loses a
  capability the other keeps" rather than "no turn ever differs".

Nothing was executed and nothing was approved during the run: it measured
decisions only, in a temp state directory removed at the end.

Its per-arm `largest_prompt` is *not* evidence of size: Ollama reports the
tokens it actually evaluated, so an arm whose prefix is already warm reports
less than it sent (the run read 6,210 for the declared arm and 4,098 for the
prose-copy arm — the ordering is a cache artifact, not a measurement). The
two-session run above, where each arm began cold, is the number that means
something.

The rules that keep the numbers honest: nothing is persisted, nothing is
estimated, a cancelled or failed request records nothing, and a provider
that reports no counts still counts the call and then says so out loud on
`/usage` instead of showing a quiet zero.

## Tier-0 pre-router (experimental, NOT shipped)

Cactus Needle 3 (121M) with a LoRA trained on the 49-turn routing
corpus. Zero-shot it is disqualified (it calls a tool on 49/49 turns);
with the LoRA, capability selection is demonstrably trainable (24/28
hits — report 23 says GO). It is blocked on: false-call rate (corpus
v2 rebalance in flight), the absence of a usable confidence gate (the
head is policy-suppressed post-tune and uninformative anyway), and a
latency re-measurement on an idle machine. **It must never be wired
into `src/` before the decision card exists.**

## Perception stack

| Job | Agreed choice | Notes |
| --- | --- | --- |
| Memory embeddings | all-MiniLM-L6-v2 on CPU | 24 ms default, zero VRAM. `nomic-embed-text` via Ollama is the zero-torch optional provider; the client MUST apply `search_query:`/`search_document:` prefixes (Ollama does not). Store dimension + model id per index; reject mixed dims. |
| Speech-to-text | `voxtype` with whisper `base.en`, on CPU | The engine actually installed and wired. Stella auto-detects `voxtype` and names it on screen when a voice turn starts; an explicit `STELLA_TRANSCRIPTION_COMMAND` wins, cloud STT only as a configured fallback. Bigger engines (`small.en`, `parakeet`) are a measured question for the round-trip harness, not a row to edit. |
| Text-to-speech | Kokoro v1.0 ONNX (`af_heart`), CPU, behind a resident worker | Kept after a head-to-head; no replacement agreed. `~/tools/stella-speak-server` answers one line-JSON request per sentence, so the model loads once per session; it is auto-detected before the `espeak-ng` fallback. |
| Voice activity | silero-vad v6 via onnxruntime only | The pip `silero-vad` packaging drags a 5.4 GB torch stack — use the bundled ONNX file. |
| Echo | webrtc AEC3 | Via the PipeWire echo-cancel module pair. |
| Screen read | grim + `tesseract --psm 6` | ~1.4 s, zero GPU. OCR-first is the decision; there is no local vision model in the stack. |
| Standing-intentions gate | Laya 0.3.20 | GPU-resident sidecar; Stella spawns and owns its lifecycle like any sidecar. |

## Web capability (decided, awaiting `src/` implementation)

Backend: TinyFish Search+Fetch (free tier; key in the environment only,
`TINYFISH_API_KEY`; live-verified 5/5). The keyless ddgs+stdlib
fallback is demonstrated but secondary. The provider budget (1000
fetch-urls/day, 500 searches/hour) is enforced by the **runtime**,
never by the model. `fetch` builds on the existing `NetworkReadTool`
machinery (pinned DNS, peer-validated), not the spike's regex guard.
Ticket: research report 22.

## Hosted provider presets (decided 2026-09-29)

The shipped model stack above is unchanged; this is about the *optional*
hosted endpoints a user may pick in setup. Every hosted preset except
OpenAI itself (Claude, Grok, Groq, OpenRouter, Gemini, FreeLLMAPI,
custom) is a thin
client of the OpenAI SDK against that provider's OpenAI-compatible
endpoint, so its `chat_with_tools` dialect is Chat Completions function
tools — only OpenAI serves the Responses API, and it keeps using it
(reasoning-endpoint rejection documented in `ARCHITECTURE.md`). No native
provider client classes: the dialect is a constructor argument on the one
`OpenAILLMClient`, and both dialects are conformance-tested as equivalent
implementations of the same `LLMClient` boundary. Keys are verified
before storage and live in the private `api_keys.json` (see
`SECRETS.md`), never in `config.json` or `StellaSettings`.

FreeLLMAPI is the odd one out: it is a *self-hosted* OpenAI-compatible
router (github.com/tashfeenahmed/freellmapi) that pools the user's own
free-tier provider keys behind one local endpoint,
`http://localhost:3001/v1`, authenticated by a locally generated
`freellmapi-…` unified token. Stella ships it as a preset because the
free-tier landscape is a real answer to "which model, cheaply" — but it
inherits the free tiers' caveats: prompts leave the machine through
whoever upstream provider the router picks, some of which log them, and
rate limits are per-provider and shifting. Users running the router on a
different port use "Other OpenAI-compatible" with the URL instead; the
preset's endpoint is fixed, like every preset except custom.

Which server answers that endpoint is one rule, not two. A saved
configuration deliberately holds no base URL for a preset that ships with
one, so every path that opens a client — the Brain's and the setup
dialog's "Test connection" probe alike — resolves
`settings.openai_base_url or base_url_for(preset)` and reaches the server
the user picked. When the probe skipped that step it left the URL empty,
the SDK substituted its own default of `api.openai.com`, and a valid local
router token came back reported as "the API key was rejected".

Pasting that token is forgiving in one direction only: a value copied from a
terminal usually carries the newline it ended on, and Stella trims leading and
trailing whitespace before it goes into an `Authorization` header and again
before it is stored. A token with whitespace *inside* it is refused rather than
repaired — it is not a key, and silently deleting characters from a credential
would make a copy-paste error impossible to see.

## Hard rules that survive any model change

The rules in `AGENTS.md` are not affected by anything in this file:
the model proposes and the runtime authorizes and executes; tool
output and external events are untrusted input; web content stays
inside `<<<UNTRUSTED_WEB_CONTENT>>>` markers with per-page caps;
`DO_NOTHING` is a legitimate decision and the stack is never optimized
in a way that pressures the brain toward always-acting.
