# Stella Filesystem Capabilities

One document for the four filesystem capabilities — `filesystem_read`,
`filesystem_write`, `filesystem_edit`, `filesystem_delete` — because they share
one argument shape, one workspace boundary, one dispatcher/approval/verification
architecture, and one documented limitation (resolve→use TOCTOU). Directory
listing and search (`workspace_list`, `workspace_find`, `workspace_search`) are
separate safe capabilities documented in `WORKSPACE_ASSISTANT.md`.

Implementations: `FileSystemReadTool`, `FileSystemWriteTool`,
`FileSystemEditTool` (subclass of Write), `FileSystemDeleteTool` in
`src/stella/tools.py`. Approval mechanics: `APPROVAL_BOUNDARY.md`; audit:
`AUDIT_LOGGING.md`.

## Shared contract

All four accept exactly one relative path (writes/edits also take `content`):

```json
{"path": "relative/path.txt"}
{"path": "relative/path.txt", "content": "text"}
```

- The workspace comes from application configuration (`STELLA_WORKSPACE`,
  default `./stella_workspace`) and is resolved to its canonical path when the
  tool is created. The model receives the capability description and schema,
  but the description is not authorization; the trusted dispatcher and tool
  enforce the boundary.
- Paths must be non-empty relative strings with no NUL characters and no `..`
  component. Absolute and drive-qualified paths are rejected. The resolved
  candidate must remain beneath the canonical workspace (`relative_to()`
  containment); a symlink that resolves outside fails closed with
  `File is outside workspace.`, and final-path symlinks are rejected outright.
- Only regular UTF-8 text files, capped at 1 MiB (reads return at most
  `MAX_READ_CHARACTERS = 8_000` characters; truncation is announced in the
  result). No binary, document/PDF parsing, recursive, wildcard or glob
  behavior.
- Errors return deterministic failed `ToolResult` values without exposing
  Python exceptions or stack traces.
- Workspace contents are treated as user-authorized input; there is no secret
  scanner. Do not configure a workspace with data you do not want exposed to
  the model through the final response path.

### The resolve→use TOCTOU limitation (canonical statement)

Path validation is a lexical and canonical-path check at validation time. It is
not race-free: between the canonical check and the subsequent open/unlink, a
local process that already has the user's own write access inside the workspace
could swap an intermediate directory for a symlink. Exploiting this requires an
attacker who already holds the user's write permissions on a single-user
desktop, so a dirfd/`O_NOFOLLOW` component-wise resolver is deliberately out of
scope for v1. This residual window is a documented limitation; all four
capabilities defer to this one statement rather than repeating it.

## filesystem_read (SENSITIVE, no interactive approval)

```text
LLM proposes filesystem_read + relative path
  -> ToolDispatcher exact lookup
  -> argument validation
  -> trusted SENSITIVE risk classification
  -> canonical workspace containment check
  -> bounded UTF-8 read (≤ 1 MiB file, ≤ 8,000 chars returned)
  -> ToolResult -> final LLM response
```

Reads require no interactive approval because they are explicitly restricted to
the configured workspace and mutate nothing — an application decision, not a
model decision. The final LLM receives the result as data and cannot request a
second file operation within the same tool loop. Reads of credential material or
other users' data are not made safe by this choice; locations outside the
workspace require stronger policy before any future capability.

## filesystem_write (DANGEROUS, exact approval, create-only)

Creates one new file; refuses existing files and directories, and never creates
missing parents. Files open with exclusive creation and owner-only initial
permissions where the platform supports it. Overwriting an existing file is not
a write — it is `filesystem_edit`.

```text
LLM proposes filesystem_write + path + content
  -> exact capability lookup -> argument validation
  -> trusted DANGEROUS classification
  -> action-specific ApprovalRequest -> application-owned ToolApproval
  -> dispatcher verifies exact capability, path, and content
  -> exclusive new-file creation
  -> trusted re-read of the expected bytes
  -> ToolResult (+ verified/unverified/inconclusive ActionReceipt)
```

## filesystem_edit (DANGEROUS, exact approval, replace-in-place)

Replaces the full content of one existing regular file. Extends
`FileSystemWriteTool`, reusing its schema, path resolution, size limit and
approval architecture. Opens without `O_CREAT`, so it cannot create files; a
missing target honestly returns `File was not found.` and nothing is written.
After truncating and writing, trusted code re-reads the exact expected bytes
before reporting `File edited and verified.` with an `edit`/`verified`
`ActionReceipt` recorded in the `InteractionTrace`. File contents never enter
the trace, and a verified receipt grants no authority for any later action.

## filesystem_delete (DANGEROUS, exact approval, one file, verified absence)

```text
LLM decision -> exact lookup -> strict validation -> trusted DANGEROUS
  -> exact CLI/application approval -> canonical + symlink checks
  -> one-file deletion -> trusted verification the path is absent
  -> ToolResult (+ delete ActionReceipt)
```

The canonical check happens at validation time, not at unlink time (see the
TOCTOU limitation above). Deletion is not recursive and expands no globs.
After unlinking, the tool independently confirms absence and only then reports
`File deleted and verified to be absent.` — success is never claimed from the
unlink return alone.

## Approval and verification rules (all mutating capabilities)

Risk classification comes from application code; the LLM cannot provide or
override it, and a model field such as `approved: true` is ignored by the
trusted runtime. The CLI displays the exact capability and validated arguments;
only `yes` or `approve` creates an approved `ToolApproval`. Missing, rejected,
malformed, or mismatched approval fails closed before execution, and every
attempt is recorded in the audit trail with risk, approval outcome, execution
outcome, arguments, and timestamp. A success claim always means the resulting
state was verified by trusted re-reading; contradictory results report
`unverified` or `inconclusive` receipts honestly.

## Intentionally not implemented

Append, rename, directory creation, arbitrary binary/document handling, secret
scanning, multi-user identity, persistent or general permissions frameworks,
sandboxing, shell/process execution, network access, retries, autonomous file
loops, and protection against a file changing between validation and use.
Before broader filesystem mutation Stella needs exact operation/target policy,
path/symlink race handling, least privilege, and recoverability rules; before
shared workspaces, authenticated identity and ownership isolation.
