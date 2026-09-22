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
- Retrieval remains deterministic keyword overlap.
- No embeddings, semantic ranking, personality system, or proactive behavior
  is involved.

## Proof required

The focused tests establish that a successful outcome can produce a write,
failure and irrelevant outcomes do not automatically write, SQLite persistence
survives a fresh Stella instance, and retrieved outcome memory changes a later
response.

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
