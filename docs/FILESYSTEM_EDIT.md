# Filesystem Edit Capability

## What was added

`filesystem_edit` (`FileSystemEditTool`) is Stella's bounded file-modification
capability. It replaces the full UTF-8 content of one existing regular file
below the configured `STELLA_WORKSPACE`. It extends `FileSystemWriteTool`, so
it reuses the same argument schema, path resolution, size limit, and
dispatcher/approval architecture rather than adding a second file-operation
path.

```json
{"path": "relative/path.txt", "content": "replacement text"}
```

## Boundary and approval

The tool is classified `RiskLevel.DANGEROUS`. It requires an application-owned
`ToolApproval` whose request exactly matches the validated capability, path,
and content; the CLI prompt describes the replacement in plain language.
Model-generated approval fields, file contents, and tool output never satisfy
approval.

It rejects absolute paths, traversal, paths resolving outside the workspace,
symlinks, directories, and oversized or unencodable content. It opens the
target without `O_CREAT`, so it cannot create files — missing targets honestly
return `File was not found.` and nothing is written.

## Verified outcome

After truncating and writing, the tool re-reads the file with trusted
application code and confirms the exact expected bytes. Only then does it
report `File edited and verified.` with an `edit`/`verified` `ActionReceipt`
and the resulting size. A contradictory re-read yields a failed result with an
`unverified` receipt and an uninspectable result an `inconclusive` one. The
receipt is bounded metadata recorded in the `InteractionTrace` as an
`ActionReceiptEvent`; file contents are never placed in the trace, and a
verified receipt grants no authority for any later action.

## Validation

Focused tests cover successful verified edits, argument-validation parity with
`filesystem_write`, missing and directory targets, symlink and workspace
escape, absolute paths, exact approved path/content matching, unverified
outcome reporting, and the adversarial cases in
`tests/security/test_adversarial_boundaries.py`.
