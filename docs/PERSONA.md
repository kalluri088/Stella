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
| `history/` | `~/.config/stella/history/` | Stella automatically, just before every replacement of either file above |
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

## Version history and `stella persona revert`

Every write path above — approved `persona_edit`, editor session,
preset, onboarding draft, and a revert itself — first copies the
previous bytes of the file into `history/` (full snapshots, newest 10
per file, plus a `manifest.jsonl` label of who replaced them and why).
The snapshots are plain files you own; the prompt loader never reads
that directory.

- `stella persona revert` lists what is undoable, newest first.
- `stella persona revert <number>` shows a unified diff against the
  current file, asks you to confirm (`--yes` to skip), restores those
  exact bytes and verifies the read-back. The restore is snapshotted
  too, so reverting a revert is one more command.
- Reflection still has no write path at all: approved proposals become
  ordinary `persona_edit` calls and inherit snapshots exactly like any
  other approved edit.

Two honest limits: a snapshot that fails never blocks the write you
already approved (the result then says the change cannot be reverted),
and the editor copy is taken *before* the `$EDITOR` session starts —
mid-edit states are not versioned, only the file as it was when you
opened it (identical content is deduped, so repeat visits cost
nothing).

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

## The persona is meant to be *used*

A persona file that only sits at the top of the prompt is decoration, so the
stable core prompt now says plainly that it is voice, applied to every
user-facing sentence: Stella keeps the persona's tone, directness and word
choices rather than drifting back into neutral assistant phrasing, and stock
filler ("Certainly", "I'd be happy to", "Great question") is treated as a lapse
in voice rather than politeness. That instruction is paired with the length
contract in the same prompt — lead with the answer, size the reply to the
question, never pad and never answer so thin that the user has to ask again —
because terse-in-character and vague-in-content are the two ways a personality
disappears.

The change is phrasing-only by construction. The invariant block above the
persona (`persona.py:PERSONA_INVARIANT`) still says the persona grants no
authority, and the prompt line repeats it: the persona never alters what Stella
may do, what needs approval, or what it reports honestly about a result.

## What persona cannot do

- Change risk levels, tool registration, approvals, or any dispatch
  behavior (those are application-owned code paths).
- Make Stella claim to be human: the invariant outranks everything.
- Silently grow the style notes past 20 bullets or past the filter.
- Act on unapproved learning: queued proposals are inert rows until a
  matching approval lands.
