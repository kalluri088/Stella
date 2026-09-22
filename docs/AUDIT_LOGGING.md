# Stella Audit Logging

## Current implementation

The trusted `ToolDispatcher` now records one in-memory `AuditRecord` for every
tool dispatch attempt. The record is created by application code after exact
capability lookup, tool validation, trusted risk classification, approval
checking, and execution handling. The LLM cannot create, edit, or authorize an
audit record.

Each record contains:

- `capability`: the requested capability, or the unavailable name;
- `arguments`: the validated structured arguments for a valid request;
- `risk_level`: the application-owned `SAFE`, `SENSITIVE`, or `DANGEROUS`
  value, when the capability exists;
- `approval_required`: whether trusted risk required approval;
- `approval_granted`: `True` for an exact granted approval, `False` for a
  missing, rejected, invalid, or mismatched approval, and `None` when approval
  was not required or validation stopped before approval;
- `execution_success`: whether the tool returned a successful `ToolResult`;
- `timestamp`: an aware UTC ISO-8601 timestamp.

The dispatcher exposes a snapshot through `audit_records`. The trail is
deliberately process-local and is not persisted to SQLite or written to a
separate file.

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

## Sensitive arguments limitation

For valid actions, the current record stores the validated argument values so
that the action can be understood later. This means sensitive values can be
retained in memory. In particular, a `filesystem_write` record currently
contains the requested file content. No redaction or field-level secret policy
exists yet.

Invalid or unavailable requests record an empty argument object rather than
preserving malformed input. The current audit trail also has no identity,
durable storage, access control, rotation, export, or tamper-evidence.

## Intentionally not implemented

This is not a general observability framework. Stella does not yet provide
persistent audit storage, structured log sinks, user identity, authentication,
redaction policies, audit search, retention rules, or security monitoring.
Those decisions should be made before shared multi-user operation or broader
system-affecting capabilities are introduced.

## Validation

Focused tests cover successful safe actions, approval-required actions with
missing, denied, and granted approval, invalid and unavailable capabilities,
tool failures, risk classification, and UTC timestamps. The full suite passes
with 163 tests, and Ruff passes.
