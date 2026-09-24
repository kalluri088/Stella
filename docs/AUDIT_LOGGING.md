# Stella Audit Logging

## Current implementation

The trusted `ToolDispatcher` records one `AuditRecord` for every tool
dispatch attempt. The record is created by application code after exact
capability lookup, tool validation, trusted risk classification, approval
checking, and execution handling. The LLM cannot create, edit, or authorize an
audit record. Since Stage A the trail is backed by an injectable bounded
history store (`stella.history.ActionHistory`): the application uses
`SQLiteActionHistory` in `stella_action_history.db` (`STELLA_HISTORY_DB`) so
what Stella did survives restarts, while library users and tests default to a
process-local `InMemoryActionHistory` with the same 256-record bound.

Each record contains:

- `capability`: the requested capability, or the unavailable name;
- `arguments`: the validated structured arguments for a valid request, with
  values longer than 120 characters reduced to a `"<N characters>"` summary;
- `risk_level`: the application-owned `SAFE`, `SENSITIVE`, or `DANGEROUS`
  value, when the capability exists;
- `approval_required`: whether trusted risk required approval;
- `approval_granted`: `True` for an exact granted approval, `False` for a
  missing, rejected, invalid, or mismatched approval, and `None` when approval
  was not required or validation stopped before approval;
- `execution_success`: whether the tool returned a successful `ToolResult`;
- `action_receipt`: the tool's verified outcome receipt, when the tool
  produced one (mutations report `verified`/`unverified`/`inconclusive`,
  `network_read` reports a `fetch` receipt with the fetched byte count);
- `timestamp`: an aware UTC ISO-8601 timestamp.

The dispatcher exposes a snapshot through `audit_records`. The trail holds at
most the 256 most recent dispatch attempts (`MAX_AUDIT_RECORDS`); older
records are evicted so a long-lived desktop process never accumulates
unbounded audit state. The durable store is metadata-only: it never contains
file contents or tool output, and the desktop window shows it in the History
tab (newest first, capability plus one outcome word).

## Runtime flow

```text
LLM proposes capability + arguments
  -> trusted dispatcher lookup and validation
  -> trusted risk classification
  -> exact application approval when required
  -> tool execution
  -> ToolResult
  -> trusted AuditRecord
```

The audit record describes what the trusted runtime observed. It is not a
second authorization path and does not change whether execution is allowed.
Missing, rejected, or mismatched approval still fails closed and produces a
failure record without executing the tool.

## Sensitive arguments handling

For valid actions the record keeps short argument values verbatim so the
action stays auditable (file paths, search patterns, reminder text), but any
string longer than 120 characters — such as `filesystem_write` file content —
is stored only as a `"<N characters>"` length summary. This prevents a
long-lived desktop process from retaining sensitive payloads in its audit
trail. Non-scalar values are stored as a type summary.

Invalid or unavailable requests record an empty argument object rather than
preserving malformed input. The current audit trail has no identity, access
control, export, or tamper-evidence; the bounded retention means only the
newest 256 dispatch attempts remain, in memory or on disk.

## Intentionally not implemented

This is not a general observability framework. Stella does not yet provide
structured log sinks, user identity, authentication, configurable redaction
policies, audit search, or security monitoring. Those decisions should be
made before shared multi-user operation or broader system-affecting
capabilities are introduced.

## Validation

Focused tests cover successful safe actions, approval-required actions with
missing, denied, and granted approval, invalid and unavailable capabilities,
tool failures, risk classification, UTC timestamps, the bounded trail, and
long-value redaction. Durable-store tests cover SQLite round-trip, retention
trimming and survival across reopen, dispatcher recreation over the same
file, network `fetch` receipts inside history entries, and the History tab
refreshing after a dispatched turn. The full suite passes and Ruff passes.
