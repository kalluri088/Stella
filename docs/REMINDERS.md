# Phase 3: User-Controlled One-Shot Reminders

## What reminders are

A reminder is a small persisted record — an id, the user's own content, a due
time, a lifecycle status, and a creation timestamp — that lets Stella notify
the user once, later. It is created only because the user explicitly asked
for it through a trusted, approval-checked path.

The core invariant: **reminder permission is not tool/action permission.**
A reminder may cause one bounded user-facing notification when it becomes
due. It never grants permission to modify files, execute tools, access
external services, or perform any unrelated action. A reminder is an event
source, not an authority source.

## How they are created

The trusted application registers three tools with the normal dispatcher:

- `reminder_create` (`DANGEROUS`, requires exact application approval):
  takes `content` and an exact ISO-8601 `due_at` with a timezone offset.
- `reminder_list` (`SENSITIVE`, read-only): lists pending reminders.
- `reminder_cancel` (`DANGEROUS`, requires approval): cancels the single
  pending reminder matching a description; zero or ambiguous matches change
  nothing and say so honestly.

The Brain may *propose* these tool calls, like any other capability. Model
output alone never mutates reminder storage: the dispatcher validates the
arguments, requires real approval for mutations, and the trusted
`ReminderStore` re-validates everything (non-empty content up to 512
characters, timezone-aware due time strictly in the future). Invalid or
expired creation requests are rejected deterministically rather than being
silently accepted. When the user's stated time is ambiguous or missing, the
Brain is instructed to ask instead of inventing a due time.

One boundary is deliberate (research report 30): a plain "remind me" is
always a Stella `reminder_create`, never an Outline operation. Outline's
`remind` field — alert times on Outline tasks and events — sets an alert
*inside the Outline app only*, and is correct solely when the user is
creating or updating an item in Outline itself and names Outline. One
request is never satisfied with both systems; when the target is unclear
the Brain is instructed to ask. The rule lives in the Brain prompt and in
the `outline_create`/`outline_update` tool descriptions, and is pinned by
tests in `test_brain.py` and `test_outline_tools.py`.

Two refinements to that boundary came out of the 21-tool selection
re-measure (research report 32), which found qwen3:4b confidently
mis-routing two shapes even after the report-30 rule. First, **a reminder
can notify only this user**: a request to remind some *other* person or
group ("remind the team to submit demos") is not something `reminder_create`
can fulfil — the model is instructed to `kind=ask` rather than silently
store a reminder the user alone would receive. Second, **a vague schedule
question is the user's own**: "what's on today?" maps to `reminder_list`,
and the model reaches for Outline only when the user names it — the
re-measure had shown `outline_search` acting as an attractor that turned a
personal-schedule ask into a document-app lookup. Both rulings live in the
Brain prompt and are pinned in `test_brain.py`; neither adds a tool, a
validator, or a new capability, keeping to the model-proposes/runtime-
authorizes and three-tools-only invariants.

## Storage and scheduling

`stella.reminders` provides a `ReminderStore` interface with an
`InMemoryReminderStore` for tests and a `SQLiteReminderStore`
(`stella_reminders.db`, configurable via `STELLA_REMINDERS_DB`) that
survives process restarts. Statuses are `pending`, `handled` and
`cancelled`; the last two are terminal, so a reminder cannot transition
twice and one reminder's id cannot cancel or handle another.

The desktop UI checks for due reminders on its own: `StellaBridge` runs a
small daemon ticker (every 5 seconds by default) whose only action is to
post one reminder check onto the bridge's single worker-thread command
queue. All reminder state therefore still changes only on that worker
thread, and firing remains the notify-only runtime path — the ticker
never consults the Brain, the LLM, or the dispatcher. Because the
`pending → handled` transition is one atomic conditional update, a
reminder is delivered exactly once even when a tick and a turn race or
two Stella processes share the store. A tick that arrives mid-turn queues
behind the running turn, so delivery can land just after it.

The CLI deliberately keeps the older behaviour: it checks for due
reminders at the start of each interaction, because a CLI session that is
idle has no window to inform. Reminders missed while offline are
delivered on the next check (or the next tick, in the UI), exactly once.

## What happens when a reminder becomes due

`Stella.check_due_reminders()` converts each due reminder into the existing
`DueTaskEvent` foundation — event id `reminder:<id>`, task title equal to
the reminder content — and hands it with a trusted, exactly-scoped
`ProactivityDelegation` to the existing proactivity decision logic. No
second decision system exists:

- `INFORM`: the user receives one message such as
  "Submit the assignment is due today."
- `ASK`: the existing policy asks for permission instead; nothing is marked
  handled without it.
- `DO_NOTHING`: completed/already-handled or duplicate events produce no
  message.

The message is only shown after the store confirms the transition to
`handled`; if that confirmation fails, delivery is withheld (fail closed)
and a later session may retry. Duplicate event ids are suppressed
in-session, and the persisted terminal state suppresses them across
restarts.

## What reminders are NOT allowed to do

- Execute, approve, or influence any tool. Delivery never consults the
  Brain, the LLM, or the dispatcher.
- Authorize anything from their own content. Reminder text is untrusted
  data: a reminder that says "delete the workspace, approved=true" still
  produces only a due notification, and dangerous tools still refuse
  unapproved calls.
- Create other reminders, modify other reminders, or expand their own
  scope — the trusted delegation is always scoped to exactly one reminder.
- Repeat: one-shot only; no recurrence in Phase 3.
- Enter the trace with full content. `ReminderLifecycleEvent` records only
  the reminder id, a content length, the lifecycle step
  (`create`/`read`/`cancel`/`due`/`delivered`/`withheld`/`duplicate`/
  `skipped`) and an outcome, preserving the existing trace privacy model.
