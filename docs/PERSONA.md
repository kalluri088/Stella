# Stella Persona

Stella's personality is **data, never code**. It changes how she sounds,
never what she is allowed to do. Every file involved is a plain text file
you own, and every mutation the model itself proposes passes through the
same exact-match approval boundary as a file write
(`docs/APPROVAL_BOUNDARY.md`).

## The files

| File | Default location | Written by |
| --- | --- | --- |
| `persona.md` | `~/.config/stella/persona.md` | you (editor or preset) — or Stella via an approved `persona_edit` |
| `persona.addons.md` | `~/.config/stella/persona.addons.md` | Stella via an approved `persona_edit` only |
| transcript DB | `~/.local/share/stella/stella_transcript.db` | Stella, **only if you turn recording on** |

Paths honor `STELLA_PERSONA_DIR` and `STELLA_TRANSCRIPT_DB`. With no
`persona.md` at all, the system prompt is byte-for-byte the default
persona-less prompt — installing the feature changes nothing until you
write a file.

## Trust tiers

What reaches the model, in strict priority order:

1. **The invariant** — a fixed, app-owned block injected first, above
   every persona line: Stella is an AI assistant and never claims to be a
   real person with real-life history, even in character, and no persona
   text can override it or the rules below it.
2. **`persona.md`** — your prose (backstory, voice, stance, examples).
   It is *not* content-filtered, because you own it and it sits below the
   invariant; it is size-capped at 8 KB with an honest truncation note.
   A hostile `persona.md` can lose an argument with the invariant; it
   cannot change dispatch, risk levels or approvals — those are read from
   application code, never from prompt text.
3. **`persona.addons.md`** — the learned style layer. Each line is a
   one-line style note. Before injection every line passes a
   forbidden-content filter (authority words like "approval", "policy",
   "rules", "permission", "system prompt", "ignore previous", "you
   are … overrides"): a matching line is discarded and the discard count
   is surfaced in the next `persona_edit` preview rather than hidden.
   Hard caps: 20 bullets, ~1 KB.
4. **Reflection proposals** — the lowest tier, and not injected at all
   until *you* approve them (below).

## `persona_edit` (the tool)

When you ask Stella in chat to change her style ("be drier"), she may
propose a `persona_edit`. It is classified `DANGEROUS`:

- The path must canonicalize (`realpath`) to exactly one of the two
  persona files — no traversal, no symlink escape.
- Addons content passes the same filter and caps at validation time; a
  rejected proposal is reported as rejected.
- The approval dialog shows a real unified diff of the file.
- Approval binds to the exact `(capability, arguments)` pair; after
  writing, Stella re-reads the bytes and reports
  verified / unverified / inconclusive like every file action.

You never need the tool: `stella persona` opens `persona.md` in your
`$EDITOR`, and `stella persona preset snark|warm|terse [--force]` writes
a starter persona instead of a blank page (it refuses to clobber an
existing persona without `--force`). Presets and editor writes are
human-initiated and bypass approval by design; model-initiated writes
never do.

## First-run onboarding

When a configured Stella starts the CLI with no `persona.md`, she asks
three questions (Who was she before you found her? What is your
relationship? What is the one thing she never does?), drafts a full
`persona.md` with the configured model, and *shows* the draft. Only
"yes" writes it; "edit" opens it in your editor first; anything else
saves nothing. Leaving the first answer empty skips onboarding forever.

## Transcripts and `stella reflect`

Reflection needs observed behavior, so Stella can record a **bounded,
opt-in transcript**: user and assistant turn text, cancelled flag and
duration, newest 2 000 rows kept, each row capped at 2 000 characters.
Off by default; turn it on with the Settings checkbox (saved in
`config.json` as `transcripts_enabled`) or `STELLA_TRANSCRIPTS=1|0`
(environment wins). It is a local file in your data directory — no
telemetry — and recording can never influence a response.

`stella reflect` (offline, cron-safe) reads transcript rows since its
stored watermark and derives **only observable signals**: turns you
cancelled during long replies (≥ 20 s) and your own style-worded
pushback ("too long", "less lists", …). Praise alone is deliberately not
a signal — that is the anti-sycophancy rule by construction. A bounded,
sanitized digest goes to the model, which may propose at most two
addon-line edits. Every candidate is re-checked by the app: authority
lines rejected, agreement-only drift rejected, evidence required, and at
the 20-bullet cap only *consolidating* edits survive.

**Reflection never writes.** Accepted proposals are queued in the same
local database with their full `persona_edit` arguments and shown as
evidence counts. The next interactive session (CLI or window) surfaces
each queued proposal as a real approval prompt through the normal
dispatcher — approve and it applies with verified receipts; deny and
nothing changes. With transcripts off or no signals, `stella reflect`
says so and exits without proposing anything.

## What persona cannot do

- Change risk levels, tool registration, approvals, or any dispatch
  behavior (those are application-owned code paths).
- Make Stella claim to be human: the invariant outranks everything.
- Silently grow the style notes past 20 bullets or past the filter.
- Act on unapproved learning: queued proposals are inert rows until a
  matching approval lands.
