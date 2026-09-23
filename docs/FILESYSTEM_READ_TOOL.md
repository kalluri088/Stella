# Stella Filesystem Read Capability

## Purpose

`FileSystemReadTool` is Stella's first capability that reads real user data.
It is a narrowly scoped step toward useful data access without adding writes,
deletes, shell execution, network access, or arbitrary filesystem access.

The capability is named `filesystem_read` and accepts exactly:

```json
{"path": "notes.txt"}
```

The path is relative to the configured Stella workspace. The model receives
the capability description and schema, but the description is not
authorization. The trusted dispatcher and tool enforce the boundary.

## Workspace boundary

The tool receives a workspace path from application configuration. The CLI
uses `STELLA_WORKSPACE`, defaulting to `./stella_workspace`. The workspace is
resolved to its canonical path when the tool is created.

The requested path must be a non-empty relative string with exactly one
`path` argument. Absolute paths, drive-qualified paths, NUL characters, and
any path containing a `..` component are rejected.

Before reading, the candidate path is resolved and checked with
`relative_to()` against the resolved workspace. A symlink inside the
workspace that resolves outside it therefore fails closed with
`File is outside workspace.`. A nested path that remains inside the workspace
is allowed.

This is a lexical and canonical-path boundary checked at validation time. It
is not race-free: between the canonical check and the subsequent open, a
local process already running with the user's own filesystem write access
inside the workspace could swap an intermediate directory for a symlink
(classic resolve→use TOCTOU). Exploiting this requires an attacker who
already has the user's write permissions on the single-user desktop, so a
dirfd/`O_NOFOLLOW` component-wise resolver is deliberately out of scope for
v1 and this residual window is a documented limitation instead.

## File limits and results

Only regular UTF-8 text files are supported. The maximum file size is 1 MiB.
Directories, missing files, oversized files, invalid UTF-8, and read failures
return deterministic failed `ToolResult` values without exposing Python
exceptions or stack traces.

The tool does not intentionally inspect environment variables, API keys,
credentials, `.ssh` contents, or unrelated system paths. Files placed inside
the configured workspace are treated as user-authorized input; this MVP does
not include a general secret scanner.

## Risk and approval

The trusted `RiskLevel` classification for `filesystem_read` is
`SENSITIVE`. The model cannot provide or override this classification. The
dispatcher reads the classification from the application-owned tool after
argument validation.

This first read capability does not require interactive approval because it
is explicitly restricted to the configured workspace, performs no mutation,
and is being used as the first bounded data-access step. This is an
application decision, not a model decision. Reads of credential material,
other users' data, or locations outside the workspace are not made safe by
this choice; those cases require stronger policy before future capabilities
are added.

Filesystem delete and other future mutating operations must not reuse this
automatic behavior. `filesystem_write` is a separate create-only capability
documented in `FILESYSTEM_WRITE_TOOL.md`; it is `DANGEROUS` and requires exact
user approval before execution.

## Runtime flow

```text
LLM proposes filesystem_read + relative path
  -> ToolDispatcher exact lookup
  -> FileSystemReadTool argument validation
  -> trusted SENSITIVE risk classification
  -> canonical workspace containment check
  -> bounded UTF-8 read
  -> ToolResult
  -> final LLM response
```

The final LLM receives the tool result as data. It does not receive a file
handle and cannot request a second file operation within the same tool loop.

## Current limitations and future requirements

The tool has no secret scanning, file provenance labels, identity isolation,
approval UI, OS sandbox, or protection against a file changing between
validation and reading. Workspace contents can also contain prompt-injection
text, so the final model must not be treated as an authorization layer.

Before filesystem delete or broader filesystem mutation, Stella needs exact
operation and target policy, path/symlink race handling, least privilege,
recoverability rules, and explicit user approval. Before shared multi-user
workspaces, it needs
authenticated identity and workspace ownership isolation.

The following remain intentionally unimplemented: document/PDF parsing,
arbitrary binary reads, directory listing, writes, deletes, shell commands,
network tools, secret scanning, generic policy engines, sandboxing, and
autonomous filesystem behavior. The create-only write capability does not
provide those broader operations.
