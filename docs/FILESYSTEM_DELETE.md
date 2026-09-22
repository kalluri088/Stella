# Filesystem Delete Capability

## What was added

Stella now has one narrowly scoped `filesystem_delete` capability implemented
by `FileSystemDeleteTool`. It deletes one existing regular file below the
configured `STELLA_WORKSPACE` directory.

The capability uses the existing trusted path:

```text
LLM decision
  -> exact ToolDispatcher lookup
  -> strict argument validation
  -> trusted DANGEROUS risk classification
  -> exact CLI/application approval
  -> canonical workspace and symlink checks
  -> one-file deletion
  -> trusted verification that the path is absent
  -> ToolResult (+ delete ActionReceipt)
  -> final response
```

## Argument and filesystem boundary

The tool accepts exactly:

```json
{"path": "relative/path.txt"}
```

It rejects missing or extra fields, non-string or empty paths, absolute paths,
parent traversal, wildcard characters, missing files, and directories. The
path is resolved canonically and must remain below the configured workspace.
Symlink paths are rejected, including symlinks that point elsewhere or point
to another file inside the workspace.

Deletion is not recursive and does not expand wildcards or globs. Errors return
deterministic `ToolResult` messages without exposing raw exceptions or stack
traces. After unlinking, the tool independently confirms with trusted
application checks that the path no longer exists and only then reports
`File deleted and verified to be absent.` with a `verified` receipt. If
absence cannot be confirmed the result is an honest failure with an
`unverified` receipt; success is never claimed from the unlink return alone.

## Risk and approval

`filesystem_delete` is classified as `RiskLevel.DANGEROUS` by the trusted tool
implementation. The LLM cannot provide a risk or approval value. The
dispatcher requires an application-owned `ToolApproval` whose request exactly
matches the validated capability and path. Missing, rejected, malformed, or
mismatched approval fails closed before execution.

The normal CLI displays the exact action and asks for approval. Only `yes` or
`approve` is accepted. The dispatcher creates the existing in-memory audit
record for every attempt, including risk, approval outcome, execution outcome,
arguments, and timestamp.

## Validation

Focused tests cover successful deletion, invalid structures, absolute and
traversal paths, wildcard rejection, missing files, directories, symlink
escapes, internal symlinks, exact approval, rejection, and no execution
without approval. Brain schema coverage confirms that the model sees the
capability and relative-path argument contract.

A real CLI validation used `gpt-5.6` with an isolated temporary workspace and
SQLite memory database. The model proposed:

```json
{"kind":"tool","capability":"filesystem_delete","arguments":{"path":"delete-me.txt"}}
```

The CLI displayed:

```text
Approval required for action: capability='filesystem_delete', arguments={"path": "delete-me.txt"}
```

After explicit `yes`, the file was deleted and Stella responded that it had
deleted `delete-me.txt`. The validation file was confirmed absent afterward.
No API key or secret was displayed.

## Intentionally not implemented

This capability does not add recursive deletion, wildcard or glob support,
filesystem write changes, shell execution, subprocesses, network access,
permissions, authentication, identity, sandboxing, or a general policy
engine. Workspace contents remain user-authorized input; no secret scanner was
added. Approval remains synchronous, action-specific, and process-local.
