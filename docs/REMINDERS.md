# Reminders: where they live now

Stella does not keep reminders. It once did — a `stella.reminders` store,
three capabilities (`reminder_create`, `reminder_list`, `reminder_cancel`), and
a desktop panel listing them. All of that has been removed. A scheduled alert
is now a property of the notes workspace the user already keeps, reached
through the single `outline` capability pair; Stella's job is to write the
alert into that application, not to run a competing one. What survives is only
the part that carries an alert back to the user: the due-check and the desktop
ticker now read Outline's own reminder list instead of a local table (see
"What was actually deleted").

## What "remind me" means now

The decision-prompt routing, restated because it is the part a reader is
most likely to get wrong:

- A plain "remind me to X at 18:00" is *scheduling work in the connected
  workspace*: `outline_create` with `kind=task` (or `event`) carrying a
  `remind` time. Outline is what alerts the user, in Outline.
- When no workspace capability is available — Outline not configured, not
  running, not named — Stella says plainly that it cannot schedule a
  notification. It never invents a reminder, and never claims one exists.
  This is the honest-failure rule, not a fallback: a confident lie about a
  notification that will never arrive is worse than a refusal.
- An alert reaches **only this user's own workspace**. "Remind the team to
  submit demos" is not something any capability can do, so the Brain must
  `kind=ask` rather than store an alert the user alone would receive.
- A vague schedule question ("what's on today?") is about the user's own
  schedule, read from the connected workspace when one is available, and
  asked about when none is. It is not an invitation to search unrelated
  documents.
- A relative or partial time is converted against the trusted runtime clock
  reference in the decision prompt. When no due time can be determined the
  Brain asks instead of inventing one — the rule that survived from the old
  `reminder_create` validator.

These rulings are pinned by `test_llm_brain_prompt_routes_remind_me_to_the_
connected_workspace` and `test_llm_brain_prompt_routes_team_reminders_and_
vague_asks` in `tests/test_brain.py`, and by the `remind`-field boundary test
in `tests/test_outline_tools.py`.

## The invariants that survived the removal

The old feature was built on rules that still hold, and the removal did not
touch them:

- **A scheduled alert is not authority.** Nothing whose job is to notify
  later may modify files, execute tools, or reach external services. Stella's
  remaining notification path is a read that becomes one line of chat: the
  due sweep claims items from Outline and turns each claim into a delivered
  message, and it cannot reach the Brain, the LLM, or the dispatcher. That is
  tested directly — `test_the_sweep_never_consults_the_brain_or_the_llm` and
  `test_claimed_content_cannot_grant_tool_authority` in
  `tests/test_outline_reminder_delivery.py`, which delivers a hostile title
  and then proves a dangerous tool still refuses.
- **Proactivity remains one-directional.** `DueTaskEvent`,
  `ProactivityDelegation` and `Stella.handoff_due_task_event()` stay, and
  they stay generic: their remaining producer is Outline's own overdue data,
  surfaced while a tool result is read or claimed by the due-reminder pump.
  What was deleted is only the adapter that turned a due row of Stella's
  reminder table into such an event. Rule 8 — proactivity may raise
  awareness, never authority — is unchanged and still tested
  (`tests/test_proactivity.py`).
- **Tool output and event content remain untrusted.** A task title coming
  back from Outline is information, not an instruction (rules 6 and 7).
- **`DO_NOTHING` remains legitimate** for a due item the policy declines to
  announce.

## The approval change this makes

The old path asked first. `reminder_create` was `DANGEROUS`, so every
"remind me" produced an exact-argument approval card before a row was
written. The new path does not: `outline_create` is `SENSITIVE`, which is
Stella's recorded classification for Outline writes
(`docs/APPROVAL_BOUNDARY.md`), and a `SENSITIVE` call executes without an
interactive prompt. So one specific sentence — "remind me to X at 18:00" —
now has *less* confirmation than it had, because it is an ordinary
workspace write rather than a gated local one.

That is accepted, for three reasons: the write is bounded to the workspace
the user already connected, it is visible and reversible inside that
application, and the alternative would be to make every Outline mutation,
including the ones users ask for constantly, wait behind a dialog. If this
trade turns out to be wrong, the fix is not a restored reminder store —
`Tool.argument_risk()` already elevates a single call from its arguments,
so `outline_create` can be turned back into an asking call whenever the
payload carries a `remind` time. That is one method and one test, added
when there is evidence it is needed rather than pre-emptively.

One claim in the routing list deserves a precise reading. "An alert reaches
only this user" is Stella's decision rule, not a guarantee about Outline:
who sees an item is the notes application's own business, and a shared
collection can expose it. The rule is therefore phrased around intent — do
not store an alert that was meant for someone else — which is the part
Stella actually controls.

## What was actually deleted

`src/stella/reminders.py`; the three reminder tools, `ReminderAction` and
`ToolResult.reminder_action`; the reminder store behind
`Stella.check_due_reminders()`; `ReminderPanel` and `ReminderScheduler` from
the application layer; the panel's reminder commands; the Reminders section of
the window and its navigation entry; `STELLA_REMINDERS_DB`,
`default_reminders_db()` and `StellaSettings.reminders_db`; and
`stella_reminders.db` from the backup manifest set.

What did **not** go is the delivery side, because deleting it would have made
"remind me" a silent no-op whenever the browser happens to be closed.
`Stella.check_due_reminders()`, `ReminderDelivery` and `ReminderLifecycleEvent`
survive in Outline-only form: they ask the Outline reminder pump for the items
*this process just claimed* and surface each as one chat line. The pump
(`stella.outline_tools.OutlineReminderPump`) owns the schedule and the network
call; `GET /api/v1/reminders/due` followed by `POST /api/v1/reminders/fire` is
the claim, so the server — not a Stella-side clock — decides that an alert
reaches the user exactly once. The desktop ticker returns as `ReminderTicker`,
whose only job is to call `post_reminder_check()` on an interval, which posts
onto the bridge's single command queue so every read still happens on the one
worker thread that owns Stella. It arms only when a real Outline transport
exists (`active_reminder_pump()` returns `None` otherwise), and the pump
rate-limits its own HTTP cycle, so ticking often costs nothing.

So Stella still wakes itself, but what it wakes into is a read of someone
else's alert list — never a tool call, never a model turn. That distinction is
the whole shape of this design.

When speech output is on, that one line is also spoken: the alert reaches the
ear as well as the chat, in its own single slot, dropped unheard if a reply or
a cancel takes the speaker first, and silent entirely when the user never
switched speech on. See `VOICE.md` ("Spoken alerts").

`stella_reminders.db` was never deleted from disk and no user data was
destroyed: the file simply stopped being opened, written, or backed up. A user
who wants those rows can read them by hand from the old state directory.

## What is intentionally not implemented

- No Stella-owned reminder clock, and no due-item list that outlives one
  request. The wake-up exists only to ask Outline what Outline already
  decided is due; if Outline says nothing, nothing happens. Anything richer
  should be designed as workspace-driven proactivity, not as a revived
  reminder store.
- No local notification bridge (DBus, `notify-send`, cron). Those are system
  changes with their own trust surface and are outside the "model proposes,
  runtime authorizes" boundary as currently drawn.
- No reminder-shaped memory. Memory is persistence, not authority (rule 5)
  and does not fire.
