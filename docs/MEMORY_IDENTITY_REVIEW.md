# Memory Identity Review

## Scope

This review covers the current memory model and its relationship with the
orchestration layer. It is based on:

- `src/stella/memory.py`
- `src/stella/context.py`
- `src/stella/stella.py`
- `src/stella/brain.py`
- `src/stella/cli.py`
- `tests/test_memory.py`
- memory-related tests in `tests/test_stella.py` and `tests/test_brain.py`
- `docs/ARCHITECTURE.md`
- `docs/ARCHITECTURE_REVIEW.md`
- `docs/REAL_WORLD_MEMORY_TEST.md`

No production code was changed for this review.

## Observed facts

### Current ownership model

`MemoryItem` contains only `content`. It has no user, account, session, or
namespace identifier. `MemoryWriteRequest` and `MemoryWriteResult` likewise
carry only the item and write status.

The provider-neutral `Memory` interface exposes:

```text
store(item)
retrieve(query=None)
```

There is no identity or scope parameter in either operation. `Stella` retrieves
using the current user input and writes the exact item supplied by an explicit
decision. The Brain does not know about a global memory instance, and Memory
does not infer ownership.

`SQLiteMemory` stores all items in one `memories` table with only an auto-
incrementing id and content. The CLI constructs one SQLite memory object for a
session using `STELLA_MEMORY_DB`, defaulting to `stella_memory.db` in the
current directory. Nothing in the current CLI establishes a user identity.

The current tests prove explicit writes, retrieval, SQLite persistence, and
cross-instance reuse. They do not test two users sharing one database or
isolation between identities.

### Consequence for multiple users

If multiple users eventually use the same database, all explicitly written
memories will share the same namespace. A later retrieval can return another
user's item whenever the lexical query matches it. The current system has no
way to distinguish that result from the current user's own memory.

This is an ownership and privacy boundary, not merely a retrieval-quality
issue. The simple keyword matcher can make the collision more or less likely,
but it cannot prevent it because it has no ownership data to filter on.

The current single-user/local-CLI setup does not demonstrate this as a present
runtime failure. It does establish that the design is single-owner by
assumption: the configured database is treated as Stella's memory for whoever
is using that local session.

## Analysis

### Would identity improve the current MVP?

Introducing identity now would address a real future requirement, but it would
also introduce an identity source that the current product does not have. The
CLI has no login, account, authentication, or user-selection concept. Adding a
`user_id` without such a source would create an API field whose value is either
hard-coded, manually supplied, or trusted from unvalidated input.

That would make the MVP more complicated without establishing meaningful
security or ownership guarantees. It could also distract from the currently
demonstrated core: explicit memory writing, local persistence, lexical
retrieval, and reuse in a later response.

### Minimal future identity boundary

When Stella gains a real session or account boundary, the smallest useful
identity design would be an explicit memory scope supplied by the composition
layer. Conceptually:

```text
MemoryScope(identity)
Memory.store(item, scope)
Memory.retrieve(query, scope)
```

The SQLite schema would then include a scope or owner column, and every read
and write would filter by that scope. The CLI or application boundary would
choose the scope; Brain and the LLM would not invent it. A separate database
file per user could provide isolation, but an explicit scope column is more
flexible if a shared database is eventually required.

This is a boundary proposal, not an implementation recommendation for the
current task. The exact name and shape should be chosen alongside the first
real identity/session requirement.

### Would identity require changing `Memory`?

If multiple identities share one `Memory` instance or database, yes: the
current interface has no way to express the owner on a read or write. A
minimal compatible evolution could add a scope parameter with an explicit
single-user default, but that would make the default easy to misuse and would
need a migration strategy.

Another option is to bind a scoped Memory object at construction time, for
example a memory instance created for one identity. That could preserve
`store(item)` and `retrieve(query)` at the call sites, but the factory or
composition layer would need to enforce the binding. It would be a useful
option only once the application has a trustworthy identity source.

Identity should not be added to `Context` merely because Context is available:
that would make a user identity carried in request data without defining who
is allowed to set or change it. Ownership belongs at the application/memory
boundary first.

### Could identity be added later?

Yes, but the migration should be deliberate. The current SQLite schema has no
owner column, so existing rows would need a defined owner during migration.
For a single-user database, assigning all legacy rows to the original local
owner may be reasonable. For a database whose history is unknown or shared,
there is no factual way to reconstruct ownership; those rows would need to be
kept in a legacy/unassigned scope or handled explicitly.

The current `MemoryItem` is a small value object and the SQLite schema is
minimal, so adding a scope later is technically straightforward. The difficult
part is not the column: it is defining identity, authentication, legacy-data
ownership, and isolation semantics. Deferring until those requirements exist
avoids pretending that a field alone provides user separation.

## Recommendation

Postpone user identity for the current MVP.

The present system is intentionally a single-user/local-memory foundation,
and its documented scope does not include multi-user data management. Adding
identity now would add API and schema complexity before Stella has a real
identity boundary to enforce. Keep the current provider-neutral Memory
interface and SQLite schema unchanged until a concrete multi-user or
authenticated-session requirement appears.

Before enabling shared use, identity must become a deliberate architectural
change. At that point, define the identity source, isolation guarantees,
legacy database migration, and whether memory is scoped by user, account, or
another explicit owner. Then update the Memory interface and both memory
implementations together, with isolation tests.

## Out of scope for now

- Authentication or authorization
- Accounts, login, or user selection in the CLI
- Multi-user database sharing
- Cross-user memory access or sharing
- Memory ownership inference from prompts or conversation text
- Per-user embeddings, ranking, or semantic retrieval
- Conversation-history persistence
- Migration of existing SQLite rows before an ownership policy exists

The most useful next identity-related evidence is not an identity
implementation. It is a concrete product requirement that specifies who the
user is and whether one database is intended to be shared. Until then, the
single-owner assumption should remain explicit and documented.
