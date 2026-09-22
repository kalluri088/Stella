# Real-World Memory Test

## Purpose

This record covers an attempted end-to-end test of persistent memory using the
real CLI and OpenAI-backed client. The test was intended to verify that a
memory explicitly written in one Stella process is retrieved and used by a
later process.

## Test setup

Before the manual test, the current architecture documentation and the
implementation of the CLI, Stella orchestration, LLMBrain, OpenAI client,
SQLite memory, Context, and tests were inspected.

The environment check found:

- `OPENAI_API_KEY`: set (the value was not displayed)
- `STELLA_MODEL`: unset
- `OPENAI_BASE_URL`: unset

The baseline checks were run with:

```text
uv run pytest
uv run ruff check .
```

Both checks passed. The suite reported 49 passing tests and Ruff reported that
all checks passed.

The CLI was then attempted with a temporary uv cache, using:

```text
UV_CACHE_DIR=/tmp/stella-uv-cache uv run stella --debug
```

No real database path was selected because the CLI could not be started until
the required model configuration was supplied.

## Observed behavior

The CLI exited immediately with:

```text
STELLA_MODEL is required
```

This is the configuration guard in the current CLI startup path. It runs before
the OpenAI client and SQLite memory are constructed. Consequently:

- no first conversation session was started;
- no factual preference was sent to OpenAI;
- no memory-write decision was produced or inspected with `--debug`;
- no temporary SQLite database was created or written;
- no second process was started; and
- no persistent-memory behavior was observed.

The earlier CLI attempt using the default uv cache failed before application
startup because that cache location was read-only. Retrying with a writable
temporary cache reached the application and established the actual failure
point above.

## Conclusion

This test did not establish whether real OpenAI-backed Stella can create and
reuse persistent memories across processes. It was blocked by the missing
`STELLA_MODEL` environment variable, not by a demonstrated memory failure.

Once `STELLA_MODEL` is configured, the test should be repeated with a dedicated
temporary value for `STELLA_MEMORY_DB`, using the same database path for both
CLI processes. The `OPENAI_API_KEY` value must remain private. The `--debug`
output should be recorded for both interactions so the explicit decision and
any `MemoryWriteRequest` can be distinguished from a natural-language promise
to remember.

## Latest real-world validation

The baseline checks were rerun immediately before the manual validation:

- `uv run pytest`: 51 passed
- `uv run ruff check .`: all checks passed

The real OpenAI-backed CLI was run twice with `--debug`, using a new temporary
SQLite database and the same `STELLA_MEMORY_DB` path for both processes. The
environment had `OPENAI_API_KEY` set and used `STELLA_MODEL=gpt-5.6`; the key
value was not displayed or recorded.

First process input:

```text
Please remember that my favorite color is cobalt blue.
```

Debug decision:

```text
{"arguments": null, "content": "I’ll remember that your favorite color is cobalt blue.", "kind": "answer", "memory_write": {"content": "The user's favorite color is cobalt blue."}}
```

The first session therefore produced a non-empty `memory_write`. After the
process exited, the SQLite database existed and contained:

```text
The user's favorite color is cobalt blue.
```

Second process input, without repeating the fact:

```text
What is my favorite color?
```

Debug decision:

```text
{"arguments": null, "content": "I don’t know your favorite color yet. What is it?", "kind": "ask", "memory_write": null}
```

The second response was:

```text
I don’t know your favorite color yet. What is it?
```

The memory was persisted to SQLite, but it was not retrieved and did not affect
the second response. The current retrieval implementation performs a
case-insensitive substring match against the entire user query; the stored
fact does not contain the full question string. No production code was
modified in response to this result.

An initial in-sandbox attempt reached the real OpenAI client but failed before
producing a decision because DNS/network access was unavailable
(`Temporary failure in name resolution`). The same procedure was then rerun
with network access and produced the observations above. No second process was
started for the failed attempt.

## Retrieval diagnosis and fix

The failure was caused by retrieval using the entire natural-language query as
one case-insensitive substring. The stored sentence did not contain the full
question sentence, so SQLite returned no row even though the two sentences
shared the meaningful terms “favorite” and “color.”

The retrieval implementation now uses a shared deterministic keyword-overlap
matcher in both `InMemoryMemory` and `SQLiteMemory`. It removes a small list of
common stop words, then requires two shared meaningful terms for a multi-term
query or the single available meaningful term for a one-term query. The
database and memory-writing behavior were not changed. This is a small lexical
improvement, not semantic search; it does not understand synonyms, word forms,
or context.

Deterministic tests now verify that both memory implementations retrieve the
favorite-color fact for “What is my favorite color?” and exclude an unrelated
memory. A new real OpenAI validation is still required to confirm the complete
cross-process conversation behavior with this retrieval fix.

## Latest real-world validation after keyword-overlap retrieval

The pre-validation checks were run immediately before the manual test:

- `uv run pytest`: 53 passed
- `uv run ruff check .`: all checks passed

The real OpenAI-backed CLI was run in two completely separate processes with
`--debug`, using a new dedicated temporary SQLite database and the same
`STELLA_MEMORY_DB` path for both processes. `OPENAI_API_KEY` was set but its
value was not displayed or recorded. The model was `gpt-5.6`.

First process input:

```text
Please remember that my favorite color is cobalt blue.
```

Debug decision:

```text
{"arguments": null, "content": "I’ll remember that your favorite color is cobalt blue.", "kind": "answer", "memory_write": {"content": "The user's favorite color is cobalt blue."}}
```

The first process exited normally. The SQLite database existed and contained
the stored row:

```text
The user's favorite color is cobalt blue.
```

Second process input, without repeating the fact:

```text
What is my favorite color?
```

The keyword-overlap retrieval check against that same SQLite database returned:

```text
["The user's favorite color is cobalt blue."]
```

Second-process debug decision:

```text
{"arguments": null, "content": "Your favorite color is cobalt blue.", "kind": "answer", "memory_write": null}
```

Final response:

```text
Your favorite color is cobalt blue.
```

End-to-end result: **succeeded**. The explicit first-session
`memory_write` was persisted, retrieved by the second process through the
keyword-overlap matcher, supplied to the real OpenAI-backed LLMBrain, and used
to produce the correct answer. No answer was manually injected, and no
production code was modified during this validation.

Remaining limitation: retrieval is deterministic lexical keyword overlap, not
semantic search. Questions using synonyms or substantially different wording
may still fail to retrieve the relevant memory.
