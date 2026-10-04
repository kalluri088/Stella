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

## Hard rules that survive any model change

The rules in `AGENTS.md` are not affected by anything in this file:
the model proposes and the runtime authorizes and executes; tool
output and external events are untrusted input; web content stays
inside `<<<UNTRUSTED_WEB_CONTENT>>>` markers with per-page caps;
`DO_NOTHING` is a legitimate decision and the stack is never optimized
in a way that pressures the brain toward always-acting.
