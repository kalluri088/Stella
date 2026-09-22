# Stella Filesystem Write Capability

## Purpose

`FileSystemWriteTool` is Stella's first system-changing capability. It is
intentionally limited to creating one new UTF-8 text file inside the configured
Stella workspace. It does not overwrite, delete, inspect arbitrary paths, run
commands, or access the network.

The capability is named `filesystem_write` and accepts exactly:

```json
{"path": "notes.txt", "content": "text to write"}
```

The path must be relative to the configured workspace. The content must be a
UTF-8 encodable string no larger than 1 MiB. Existing files and symlinks are
rejected; parent directories must already exist.

## Trusted execution boundary

The tool is classified by application code as `RiskLevel.DANGEROUS`. The LLM
may propose the capability, path, and content, but it cannot authorize the
write or provide its risk classification.

The runtime flow is:

```text
LLM proposes filesystem_write + path + content
  -> ToolDispatcher exact capability lookup
  -> FileSystemWriteTool argument validation
  -> trusted DANGEROUS risk classification
  -> Stella requests an action-specific ApprovalRequest
  -> CLI/application creates ToolApproval
  -> dispatcher verifies exact capability, path, and content
  -> exclusive new-file creation
  -> ToolResult
  -> final LLM response
```

Missing, rejected, malformed, or mismatched approval fails closed before a
file is created. A model field such as `approved: true` is ignored by the
trusted runtime.

## Workspace and path security

The workspace is resolved to its canonical path when the tool is created. The
requested path must be non-empty, relative, NUL-free, and contain no `..`
component. The resolved target must remain beneath the canonical workspace.
Symlink escapes are rejected. An existing final symlink is rejected as an
existing destination, even when it points elsewhere inside the workspace.

The tool uses exclusive file creation and does not create missing parent
directories. The file is opened with owner-only initial permissions where the
platform supports the normal POSIX mode argument. This is a bounded MVP path
check, not a complete operating-system sandbox or race-free filesystem policy.

## File limits and results

Only UTF-8 text is accepted, with a maximum encoded size of 1 MiB. Empty text
is allowed. Invalid structures, non-string values, oversized or unencodable
content, missing parents, existing destinations, and runtime failures return
deterministic `ToolResult` values without exposing raw exceptions or stack
traces.

The tool does not scan for secrets. Workspace contents and requested write
content are treated as user-authorized input for this MVP; applications should
not configure a workspace containing data they do not want Stella to expose to
the model through the final response path.

## Approval decision

Filesystem writes require the existing synchronous CLI approval interaction.
The CLI displays the exact `filesystem_write` capability and validated
arguments, and only `yes` or `approve` creates an approved `ToolApproval`.
Everything else rejects the action. This keeps authorization in trusted
application code without adding a general permissions or approval framework.

## Validation

Focused tests cover valid creation, malformed arguments, traversal and
absolute paths, nested paths, missing parents, existing files, symlink escape
and existing-symlink cases, UTF-8 and size limits, trusted risk classification,
approval states, exact path/content matching, Stella orchestration, and the
final response path. The full suite currently passes with 159 tests and Ruff
passes.

Real-world `gpt-5.6` CLI validation is recorded below after running against a
temporary workspace. No API key or secret is recorded.

The live CLI request was:

```text
Create a new file named gpt_write_check.txt in the Stella workspace containing exactly this text: STELLA_WRITE_REAL_9Q4K.
```

The model selected:

```json
{
  "kind": "tool",
  "capability": "filesystem_write",
  "arguments": {
    "content": "STELLA_WRITE_REAL_9Q4K.",
    "path": "gpt_write_check.txt"
  },
  "memory_write": null
}
```

The CLI displayed the exact action, accepted `yes`, and the final response
reported that the file was created. Independent inspection found the file
inside the temporary workspace with exactly the requested 23-byte content.
The API key was not displayed or recorded.

## Intentionally not implemented

This capability does not provide file overwrite, append, delete, rename,
directory creation/listing, arbitrary binary or document handling, secret
scanning, multi-user identity, persistent approval, sandboxing, a generic
permission engine, shell/process execution, network access, retries, or
autonomous write loops.
