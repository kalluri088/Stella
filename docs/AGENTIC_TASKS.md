# Research: more agentic work in Stella

Status: research only — no behavior in this report is implemented. The
question was asked directly ("research on how to implement more agentic
tasks into Stella"), and `AGENTS.md` rule 13 says complexity is added only
when a real problem demands it. This document records where the loop
actually stops today, which of the known techniques would move that line,
and which do not survive contact with this machine. The empirical base is
an audit of the running code (file:line cited throughout) plus the
measured constraints in `docs/MODELS.md` and `docs/ROADMAP.md`.

## What "agentic" means here

A user request that needs more than one tool to finish: "read this file,
fix the typo, update the Outline page, tell me what changed." That is four
dispatches. Stella is a one-or-two-dispatch assistant, and the interesting
question is not "can we add a planning framework" but "where exactly does
the current loop break, and what is the cheapest honest fix for each
break."

## The four real ceilings, in order of how often they bite

1. **Steps per turn.** `Stella.process` runs a bounded
   observe/decide/act loop (`stella.py:385`) and the application composes
   `max_tool_steps=2` (`app.py:1258`). The bound counts tool dispatches,
   and since every dispatch follows one model call it effectively bounds
   decisions too. Hitting it is not silent: the turn returns
   `"I reached the maximum number of tool steps."` with the last tool
   result and the full `step_trace` (`stella.py:434-452`), so a four-part
   request gets two parts done and says so.
2. **One call per decision.** The native tool path takes
   `response.tool_calls[0]` and nothing else (`brain.py:475-476`), and the
   JSON protocol carries exactly one `capability` plus its `arguments`
   (`brain.py:65-74`). Parallel tool calls from one turn are simply not
   expressible.
3. **Nothing survives the turn.** Each turn builds a fresh `Context` with
   conversation history but deliberately no tool observations
   (`app.py:290-292`; pinned by `test_stella.py:908`). Within a turn
   observations do feed the next decision, bounded to 8 entries and
   4000 chars with failures first (`context.py:209-237`,
   `stella.py:540-550`). Across turns only memories and the
   metadata-only `ActionHistory` survive (`history.py:1-14`). A "task"
   spanning two utterances does not exist as an object.
4. **Approval per dangerous action.** The gate is one
   `approval_provider` call per dispatch (`stella.py:1033-1035`), so a
   four-write request asks four times (`docs/APPROVAL_BOUNDARY.md:184`
   says this out loud). There is no batch or plan-level approval anywhere.

There is no plan, task-list, goal or step-engine type anywhere in `src/` —
the closest existing vocabulary is `tool_final` ("this single tool result
is all the request needs", `brain.py:267-274`) and the reminder tick,
which pumps Outline due-dates on a timer (`app.py:2043-2066`) but takes no
tool action in its own result (`proactivity.py:78`).

## Why the ceilings are drawn there

These are not oversights; each is a measured decision.

- The brain is `qwen3:4b` at 28/30 strict decisions on the production
  path (`MODELS.md:11-21`). Every added step multiplies that error rate
  against itself, and every step re-prefills a prompt that already runs to
  several thousand tokens — decision turns were measured as
  prefill-dominated (research reports 15 and 20, `ROADMAP.md:216`).
- `num_ctx=8192` is a hard wall chosen because a *clipped* prompt was an
  observed failure (report 15): a longer chain of observations is what
  pushes a prompt over it.
- `ARCHITECTURE.md:718-719` explicitly rejects unbounded LLM-based
  planning, and rule 15 prefers a small understandable system. The
  fail-closed parser (`brain.py:678-684`, which returns a bare
  `DO_NOTHING`) exists so a confused model cannot turn a bad parse into an
  action; widening the decision schema widens exactly what that parser
  must reject.

## Techniques evaluated

Ranked by whether a demonstrated Stella weakness supports them *and* what
they would cost on this machine. External work was reviewed for context —
the field's own measurements are mostly about bigger models and cloud
tools, which is why several popular answers are rejected below.

| Technique | Demonstrated Stella need? | Cost here | Verdict |
| --- | --- | --- | --- |
| Raise `max_tool_steps` | Yes — the two-step bound is the first wall users hit; limit-hit path already reports honestly | One prefill + one model call per extra step; longer observation lists risk the 8192 clip | **Worth trying first, as a knob, not a new engine** |
| Verify-state-before-retry (checked side-effect confirmation) | Partly — a failed result already reaches the next decision intact (`stella.py:543-549`) and failures are the first observations kept (`context.py:219-223`), and Outline actions return an `ActionReceipt` (`stella.py:527-536`) | Low: the receipt machinery exists; the gap is reading it before proposing a retry | **Cheap and doctrine-aligned**; mostly a prompt/routing question |
| Multi-turn task continuation | Yes — "do the rest of it later" has no object today | Needs a real task store: schema, UI, expiry, and the authority question of a stored plan firing tools later | **Do not build a store; use Outline as the store** (below) |
| Plan-ahead / ReWOO-style batched tool calls (`tool_calls[1:]`) | Not demonstrated — no evidence the 4B model plans a correct multi-call batch; its single-decision accuracy is what was measured | Schema widening + partial-JSON risk, which `ROADMAP.md:709` forbids ("partial structured JSON must never trigger an action") | **Rejected for now** |
| Plan-level (one-shot) approval of a whole chain | Not demonstrated; owner has not complained about approval count | Blasts one approval across N dangerous actions and weakens rule 10; research on confirmation fatigue argues for batching, but those designs assume a bigger model with a measured false-positive rate | **Rejected** — stay exact-match per action |
| Self-reflection / critic pass (Reflexion-style) | No measured failure that a critic would catch better than the existing failure-feedback loop | Doubles model calls per turn on a prefill-dominated path | **Rejected as a default**; possible as an explicit opt-in capability later |
| Sub-agent / multi-agent decomposition | No — Stella is a single-user voice assistant, not a codebase worker | N× prefill on a 6 GB-VRAM-class line; multi-agent papers assume cloud models; compounding errors across agents with a 28/30 base | **Rejected** |

## The one design that fits the doctrine

The doctrine already answers "where should a multi-step task live": rule 16
makes the Outline HTTP API the single capability surface, and capabilities
grow inside `kind`/`action` values rather than by adding verbs. The
`outline_update` action set is already that wide
(`outline_tools.py:1015-1019`: complete/open/reschedule/edit/restore for a
task, activate/archive for a project). So a task that outlives a turn is not a
new engine: it is an Outline item, and the existing reminder tick
(`app.py:2043-2066`) is already the scheduler that notices it. That gives
Stella durable multi-step work with no new store, no new authority path,
and no new schema — the model reads and writes the plan through
capabilities that are already gated, and the boundary `REMINDERS.md` draws —
Stella keeps no reminders of its own, it schedules alerts in the user's own
workspace — stays intact, because the alert still comes from Outline.

What this route still lacks, and what a follow-up would have to measure
before writing code:

1. Whether the two-step bound is actually what users collide with, or
   whether they collide with step 1 (a wrong capability choice). Fixing
   the wrong wall adds latency for nothing — the existing trace
   (`trace.py`, `/history`) is the instrument that would tell.
2. Whether a raised bound stays inside `num_ctx=8192` with realistic
   observation sizes, i.e. measure the largest prompt a 3- or 4-step turn
   actually builds. `docs/MODELS.md`'s `/usage` section now reports the
   largest single prompt per session, so `/usage` is the gauge.
3. Whether a stored plan should ever be allowed to *act* when it fires, or
   only to *inform*. Today a due reminder informs (`proactivity.py:78`).
   Letting a stored plan dispatch dangerous tools while the owner is away
   would break rule 10 — and there is currently no channel that could even
   ask: a `stella voice` approval prompt stays on screen, is never narrated,
   and spoken words such as "approve it now" are explicitly ordinary
   untrusted content that cannot construct an approval
   (`VOICE.md:236`, `VOICE.md:531-533`). An unattended plan therefore has no
   way to be authorized, which is the honest reason it must inform rather
   than act until a real remote-approval design exists.

## Intentionally not implemented

No code change accompanies this report. The ceilings above are documented,
one number that was missing to reason about them — the largest single prompt
a session sends — is now readable from inside a session with `/usage`, and the
cheapest next step is an instrumented decision: count which step real
requests die on, over a week of ordinary use. Not a planning framework.
That is rule 13 applied honestly: the demonstrated weaknesses are the
two-step bound and the absent cross-turn task object, and both have
cheaper answers than an agent engine.

## Validation

This report was produced from a read-only audit of the running code, with
every claim tied to a file:line, plus the measurements recorded in
`docs/MODELS.md`, `docs/ROADMAP.md` and `docs/APPROVAL_BOUNDARY.md`. Those
citations were re-checked against the tree at this commit. No behavior was
changed, so no tests were run for it beyond the repository's standing suite
for the same session's other changes.

One limit on the grounding, stated rather than hidden: the external
techniques in the table were reviewed from search results and abstracts, not
from reproduced experiments — a paper fetch failed mid-research, and this
machine cannot rerun their rigs anyway. So no number in the "Cost here"
column comes from outside this repository; each is either a measurement
already recorded in `docs/` or a count of model calls and prefills, and the
verdicts are argued from Stella's own constraints rather than from the
literature's claims.
