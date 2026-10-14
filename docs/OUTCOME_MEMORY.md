# Outcome-to-Memory Flow

This document records the constrained MVP bridge from a tool outcome to one
durable memory. It does not introduce a learning model or change the Memory
interface.

## Contract

The Brain may propose a `MemoryWriteRequest` after seeing a successful tool
observation when the output contains a durable, user-relevant fact or lesson.
The trusted runtime persists that proposal only when:

- the related `ToolResult` is successful;
- its output is non-empty; and
- no earlier memory write has already been performed in the interaction.

The existing explicit write path remains unchanged for ordinary user-directed
memory requests. Tool results without a proposal are not stored. Failed tools,
empty results, and irrelevant results without a proposal do not become memory.

## Flow

```text
Brain proposes TOOL
        -> trusted dispatcher executes
        -> successful ToolResult
        -> Brain proposes ANSWER + optional MemoryWriteRequest
        -> trusted runtime gates and stores at most one item
        -> later process retrieves the item
```

Tool output remains untrusted model input. The runtime does not infer a fact
from arbitrary output, execute instructions found in it, or store every result.
The Brain's proposal is the relevance signal; the application-owned success,
non-empty, and one-write gates control persistence.

## Deliberate limits

- Only successful tool outcomes are eligible.
- Empty output is not eligible.
- One explicit write is allowed per interaction.
- Raw tool output is not automatically stored.
- Retrieval stays deterministic keyword overlap, with the semantic index on
  by default since #245 (2026-10-14; the Settings checkbox or
  `STELLA_SEMANTIC_MEMORY=0` still turns it off, and an install that says no
  never creates the file). With it enabled, at most two extra items may be appended
  after the keyword matches, each reported with its own provenance
  (`RetrievalSource`: `keyword` plus lexical score, or
  `local-hash-embedding` plus cosine score). The scales are never
  compared, and a word-shape hit is presented to the Brain as a weaker
  hint than a keyword match — never as understanding.
- No learned embeddings, personality system, or proactive behavior is
  involved.

## Proof required

The focused tests establish that a successful outcome can produce a write,
failure and irrelevant outcomes do not automatically write, SQLite persistence
survives a fresh Stella instance, and retrieved outcome memory changes a later
response.

## The day-one test, run for real (2026-10-10)

Commitment 4 of `docs/STELLA_VISION.md` asks for an experience, not a unit
test: *tell her one small true fact about yourself on Monday. Ask something
related to it on Friday, in a fresh session, without repeating it.* That had
never been done. It was run today through `bin/stella-dev chat` against the
tree's own `.dev` state — a new process per "day", the fact stored by one and
asked about by the next — against this machine's local Ollama brain, because
the brain the configuration names (the router on `localhost:3001`) answered
nothing all day.

**The mechanism works.** Told *"Just so you know, I am allergic to
penicillin"* — no request to remember, just a statement — the Brain volunteered
`memory_write` with the compact third-person form *"The user is allergic to
penicillin."*, the runtime asked for a `yes`, and on approval the row landed in
`stella_memory.db`. A second process, asked *"Am I allergic to anything in the
penicillin family?"*, reported `memory retrieved 1` and answered *"Yes, you
are allergic to penicillin based on your stored memory"* — the fact was used,
not merely found. Nothing about the store, the write gate, the restart, or the
Brain's willingness to notice a durable fact was the problem.

Three things stood in the way, and they are separate:

- **One approval per fact.** The write is gated (`rule 10`: the model proposes,
  the owner authorizes), so the Monday sentence produces a prompt — *"Type
  'yes' to allow this"* — not a silent store. A test that pipes one line and no
  answer records `approval denied` and stores nothing. This is the design
  working, and it is the reason the commitment is not yet invisible.
- **A paraphrase finds nothing at shipped settings.** *"Can I take amoxicillin
  for my sinus infection?"* retrieved zero: recall without the opt-in is
  lexical, and `relevance_score` needs **two** shared normalized terms, so even
  *"Is penicillin something I should avoid?"* — one shared word — found
  nothing and got a generic leaf about penicillin being "generally safe for
  most people". With the existing `STELLA_SEMANTIC_MEMORY=1` opt-in
  (`local-hash`, no model to load) the same paraphrase retrieved the allergy
  and Stella answered *"No… Amoxicillin is a penicillin-type antibiotic"*. The
  Vision's test is written as a paraphrase, so as shipped it fails; one switch
  makes it pass, and as of 2026-10-14 the owner has decided that switch is on
  by default (#245).
- **On a local brain, a recalled memory can be the token that overflows the
  context.** With OS, shell and browser capabilities enabled, this machine's
  decision prompt measures **8,151 tokens against `num_ctx=8192`** (41 tokens
  of room; the same turn with those families off measures 6,191). One recalled
  memory adds **58**. So the recall turn sends 8,209 and Ollama refuses it —
  `exceed_context_size_error` — while the identical turn that retrieves
  nothing succeeds. Until today the client discarded the server's reason and
  the window said only *"Ollama native chat request failed: HTTP 400"*, which
  is why this took measurement rather than reading. It now repeats the bounded
  reason (`native_error_detail`, `src/stella/ollama_client.py`).

**What this does not prove.** No calendar time passed — "Friday" was the next
process, and nothing in the store ages, expires or decays, so the days-between
half of the test is untested rather than passed. The desktop window, where the
owner actually says things, was not used; the CLI was. The router path was
never exercised, because it was down, so whether the same recall turn survives
against a cloud brain is unknown. And the store held one fact: what recall
looks like with fifty of them is `test_memory_scale.py`'s claim, not this run's.

## Real `gpt-5.6` validation

The complete flow was run with two explicit tool steps, a fresh temporary
SQLite database, and two processes. The API key was not displayed or recorded.

First process:

- `gpt-5.6` selected `echo` with the message `The user prefers tea. This is a
  meaningful durable preference.`
- The successful EchoTool result was returned to the next Brain decision.
- The Brain returned an `ANSWER` plus `memory_write: "The user prefers tea."`.
- Stella returned `MemoryWriteResult(written=True)` and the SQLite row was
  present.

Fresh second process, using the same database:

- Retrieval returned `The user prefers tea.` for
  `What does the user prefer about tea?`.
- `gpt-5.6` answered: `The user prefers tea, but no specific type or
  preparation preference is given.`

This confirms that the successful outcome produced one explicit write, the
write persisted across processes, and retrieval changed the later response.
The normal CLI is configured for two bounded tool steps so the outcome can
reach a second Brain decision. Direct `Stella` callers still default to one
step unless they explicitly opt into multi-step behavior.

## Independent `filesystem_read` validation

A second real two-process validation used the existing `filesystem_read`
capability and a harmless temporary workspace file containing:
`The user prefers jasmine tea in the evening.` The user request asked Stella
to read `profile.txt` and act on any durable preference, but did not include
the preference text.

The first process read the file, the next Brain decision proposed
`The user prefers jasmine tea in the evening.`, and the runtime persisted one
memory row. A fresh second process retrieved that row for
`What tea do I prefer in the evening?` and responded:
`You prefer jasmine tea in the evening.` The file was not needed for the
second interaction.

This establishes that the current outcome-to-memory flow can preserve a fact
that came from an existing trusted tool rather than merely echoing a fact from
the user request. The focused filesystem-read test and existing failed/
irrelevant outcome tests provide the deterministic companion coverage.
