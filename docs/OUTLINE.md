# Outline Workspace Tools

## Purpose

Outline is a local task, calendar and notes application
(`~/Projects/Outline`: FastAPI over SQLite, bound to `127.0.0.1`, every route
guarded by a token file). `stella.outline_tools` is Stella's half of that
integration: four capabilities that let her read and change the user's real
workspace over the same HTTP API the Outline web UI drives.

Stella has no notifier, no calendar and no task table of her own. When she
needs persistent, organized state — a reminder, a project, who is connected to
what — she writes it into Outline and Outline owns it. This is AGENTS.md rule
16: the Outline HTTP API is the single capability surface, and Stella's tools
and the Outline web page are two thin clients of it.

They are strictly opt-in, they never delete anything, and everything they read
back is data rather than instruction (rules 6 and 7).

## The four capabilities

| Capability | Risk | What it is |
|---|---|---|
| `outline_search` | SAFE | the only read: a date window (today / upcoming / overdue), a keyword query, `kind=task` with no keywords for the open list, a tag filter, one person's detail, a project or person's connections as text, recently deleted items |
| `outline_create` | SENSITIVE | one new task, event, note, water log, timer, project or person |
| `outline_update` | SENSITIVE | one action on one existing item, including attaching and detaching a person |
| `outline_bulk` | SENSITIVE | one action across many tasks in a single request |

Every SENSITIVE call is approved by a human with an exact-argument summary
before it leaves (`outline_tool_summaries`), so the approval card reads "mark
the Outline task with id 7 complete" and never a raw JSON body. Editing a
record names the *fields* being written, not the values.

The family stays four. Capabilities grow as `kind` and `action` values, never
as new verbs: task `complete`/`open`/`reschedule`/`edit`/`restore`, event
`reschedule`/`edit`/`restore`, project `activate`/`archive`/`edit`/`restore`,
person `edit`/`restore`, note and tag `edit`, timer
`pause`/`resume`/`stop`/`cancel`, link `attach`/`detach`, and across a set of
tasks `complete`/`open`/`priority`/`tags_add`/`tags_remove`/`restore`.

**No delete is exposed.** Deletion is a gesture in the Outline UI. Trash items
are restorable through `outline_update action=restore`, which is the only
reason `outline_search kind=trash` exists.

## Two documented non-parities

Rule 16 says a capability is not done until Stella can drive it too. Two
exceptions are recorded, and they are the only ones:

- **Bulk export** is UI-only. Downloading whole tables into a model context is
  useless-to-harmful; the user exports from Outline.
- **Graph layout** is rendering-only. The force-directed *positions* are a UI
  concern. The connections themselves are full parity: attach and detach
  through `outline_update kind=link`, and read any person's or project's
  neighborhood with `outline_search kind=graph`.

## Gating: two conditions, both required

```
STELLA_OUTLINE (Settings checkbox or environment)  AND  a usable client
                                                        + a live /healthz
```

The client comes from the environment: `OUTLINE_TOKEN` wins, otherwise the
server's own data directory supplies `outline.token`. The address is
`OUTLINE_URL`, defaulting to `http://127.0.0.1:8741`; a value that is not
http(s) is refused before any token is spent on it. A token longer than 256
characters or carrying a control character is refused outright rather than
sanitised — a control character in a header value would be request splitting.

If either condition fails, `build_outline_tools` returns an empty list: the
model is never offered a capability that could only fail. This is the same rule
the desktop tools follow, and it is deliberate that a stopped Outline server
costs a quarter-second at startup and nothing afterwards.

The consequence is that a disabled integration and a broken one look identical
from inside the application, which is correct for the model and useless for the
person who ticked the box. `probe_outline` is the module's answer: it reports
the gate's decision and the reason for it, using the identical precedence so
the diagnosis can never disagree with the behaviour. Two things read it.

- `stella doctor` prints an `outline` line. While the gate is off it is an
  informational line and **no request is made**. Once the owner has asked for
  Outline — by the checkbox or by `STELLA_OUTLINE=1`, including on a machine
  with no config yet — a server that does not answer is reported as missing,
  naming the address probed and the directory the token was looked for in.
- `/status` lists capabilities the dispatcher would actually accept, taken
  from the same registry the model's own prompt is built from. A ticked box
  with no running server does not appear there; it appears on a separate
  `not connected:` line.

Neither ever prints the token. `OutlineClient` carries an explicit `__repr__`
for exactly that reason, since a frozen dataclass would otherwise name every
field in any log line, assertion or exception that mentions a client.

## Environment

| Variable | Meaning | Default |
|---|---|---|
| `STELLA_OUTLINE` | ask for the tools (`1`/`0`; overrides the saved checkbox for one launch) | off |
| `OUTLINE_URL` | the address the server listens on | `http://127.0.0.1:8741` |
| `OUTLINE_TOKEN` | the request token, winning over the file | unset |
| `OUTLINE_DATA_DIR` | where to read `outline.token` from | this platform's per-user data directory for the app `outline` — on Linux `$XDG_DATA_HOME/outline`, else `~/.local/share/outline` |

`OUTLINE_DATA_DIR` is the one name the two programs share: Stella resolves the
token directory the same way `outline_server.config` does, so a token the server
wrote at its first run is found by Stella without either one being told twice.
The other two are Stella's own. The server *generates* its token and never reads
`OUTLINE_TOKEN` — that variable exists to let Stella talk to a server whose data
directory she cannot see. And the server takes its address as `OUTLINE_HOST` plus
`OUTLINE_PORT`, not a URL, so moving its port means setting `OUTLINE_URL` here
too; Stella does not follow `OUTLINE_PORT`. The port is otherwise fixed by
configuration and never auto-scanned, the same discipline the llama-server port
follows.

No Stella release ships an Outline server. A user without `~/Projects/Outline`
simply gets an informational line and no tools; the integration is inert rather
than imitated.

## Reminders

`REMINDERS.md` is the authority on the flow; the part that lives here is the
transport. A real transport build of `build_outline_tools` — one with no
injected fake — also arms `OutlineReminderPump`, making Stella a third client
of the same claim funnel the web UI pumps: `GET /api/v1/reminders/due` followed
by `POST /api/v1/reminders/fire`.

`/reminders/fire` is the server's single acknowledgement endpoint, so whichever
pump claims an item first is the one that delivers it and the other gets
nothing. That is how "exactly once across both surfaces" is achieved without
either client knowing the other exists. The pump claims at most once a minute
and backs off for two after any failed cycle, because the GUI ticker fires far
more often than a reminder poll needs and an unreachable server must cost one
probe, not a request per tick.

Response rows are untrusted data: a row missing a valid `kind`, `id` or
`remind_at`, or a blank title, is dropped rather than delivered.

## What comes back

Every result is framed with a header naming it as stored data, and bounded —
`MAX_OUTPUT_LINES`, `MAX_LINE_CHARS`, `MAX_TITLE_CHARS`, and a
`MAX_BODY_CHARS` ceiling on anything written. A task titled like a command is
therefore read as a title, not as an instruction, which is rule 6 stated as a
rendering decision rather than a hope.

## The first live turns (#241, 2026-10-13)

Until this week the Outline surface had never been driven by a real model turn
on this machine — the `Validation` list below is honest about how long that
stayed true. Running it needed a server, and the server that *should* answer is
the one that cannot start: `~/Projects/Outline` carries migrations to `003`
while `~/.local/share/outline/outline.db` is at schema `5`, so that checkout
refuses to boot rather than downgrade her data. The newer build sitting beside
it, `~/Projects/outline-graph` (migrations to `005`), is what wrote that
database. Nothing was migrated, and her real database was never opened for
writing: the test server ran the newer build against a scratch
`OUTLINE_DATA_DIR` on a spare port, with four throwaway tasks created in it
through its own API, and Stella's client was pointed there by environment. Two
real turns later, three defects had surfaced, all of them fixed here.

The first turn asked *"Do I have anything in my Outline about car insurance?"*
while the scratch database held a task titled exactly that. The brain called
`outline_search` with a window nobody had asked for — `when=upcoming` — and the
window answered "(nothing matched)", because an upcoming window read the
**events** list only and a task with no due date is in no date window at all.
Her words to the owner were a clean, well-mannered lie: *"No, there is nothing
in your Outline about car insurance."* The two halves of that failure are now
separately impossible: **`upcoming` merges future-dated open tasks with
events**, ordered by when they fall (the API pages both, so this is the server
being used rather than a second sort invented here), and **an empty window
names itself** — it says it is the `upcoming` window, that a window lists only
items dated inside it, and that the same query alone searches everything. An
empty search with no window still says the plain `(nothing matched)`, because
there the plain answer is the true one.

The second turn asked *"What is on my Outline list right now?"* and went
differently: the call was rejected as invalid, the identical call was made
again, and the owner got *"The tool call failed because the arguments provided
were invalid… could you please clarify?"* The trace names the fields the model
sent (`kind, query, when`), so the rejection was about a **value** — a schema
slot filled with an empty string, which validation read as a violation. A blank
optional string now means *not given*, the same rule `_text` already applied
when reading; the open list also became reachable, because `kind=task` with no
keywords was refused by a validator sitting in front of a listing branch that
was already written and could never run. The re-run of that exact sentence
landed in one call and returned *"Your Outline list today has 4 open tasks and
no water entry"* — four being the number in the observation, which had never
been checked against a real brain before.

What the two turns did **not** fix is the thing they both started with: the
brain volunteers a date window that nobody asked for, so the plain keyword
search never runs and the titles never reach her. The empty-window sentence made
the resulting answer honest — *"…for today; a broader search would be needed to
find older or undated items"* — but did not, in that sample, make her take the
cheaper second call the hint offered. That is prompt and persona work, it joins
the other named model-behaviour residuals rather than being fixed sideways
inside this change, and one sighting each way is not a rate.

## Security notes

- Loopback HTTP with a bearer-equivalent header, over stdlib `urllib` only: no
  new dependency, no direct database access, and no path by which a model
  argument becomes a shell command or a file path.
- The token never enters `argv`, `config.json`, a log line or the audit trail.
  It is read at process start and held only on the client object.
- Model-supplied arguments are validated on this side before anything leaves,
  and the server validates again. Stella mirrors Outline's recurrence and tag
  token validators from `outline_server/api/__init__.py`; those two sides change
  together or the mismatch shows up as a rejected request.
- Tool output is framed as data and is never an instruction; a reminder title
  that reaches the user passes through the same untrusted-content rendering as
  everything else on screen.

## Validation

- Offline: `tests/test_outline_tools.py` (gating, the probe and its agreement
  with registration, every kind and action, argument rejection, bounds, hostile
  stored content, approval summaries) and
  `tests/test_outline_reminder_delivery.py` (claim-once semantics and a hostile
  title delivered as data). `tests/test_doctor.py` holds the doctor line
  through all four gate states and asserts the token reaches neither output
  form. `tests/test_commands.py` pins `/status` against a real dispatcher.
  `tests/test_paths.py` pins the token-directory resolution.
- Live, on 2026-10-06 against the real machine: with `STELLA_OUTLINE=1` and
  nothing listening on `8741`, `stella doctor` reported the outline line as
  missing, naming `http://127.0.0.1:8741` and
  `~/.local/share/outline`, and a scripted search of both output forms for the
  real token found zero occurrences. That is the state this machine is actually
  in: the workspace, database and token exist and the server is not running, so
  the tools are correctly dark.
- Live, on 2026-10-13: the newer Outline build (`~/Projects/outline-graph`)
  served a scratch data directory on `127.0.0.1:8742` with four seeded tasks,
  and real `qwen3:4b` turns through `bin/stella-dev chat` drove
  `outline_search` over it. That is the source of the three fixes in *The first
  live turns* above, and of the re-run that finally listed the list. The owner's
  real database was not written to, migrated or downgraded at any point.
- Not yet validated live: a turn against a running server **holding the
  owner's own data** — which needs her version skew resolved first (see
  `RESEARCH-RUNNING-OUTLINE.md`) — and a due reminder arriving through the pump
  with the browser closed. Both need the server started against the real data
  directory, which is the owner's call.
