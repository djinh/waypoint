# Design: recurring items (Waypoint 2.0)

Recurring work moves into Waypoint: a schedule that turns into ordinary items for an owner, and
check-ins that let Waypoint notice when a job on a timer missed its run. Waypoint still runs no
commands. Approved for 2.0 on 2026-11-20; the contract is in [API.md](../API.md) and
[openapi.yaml](../api/openapi.yaml).

## 1. Why

Waypoint 1 left recurring work out on purpose. Today a group runs its recurring work in three
places that don't know about each other:

- **systemd timers and cron**, on the machine;
- **a session's own scheduler**, such as a scheduled task or a loop that lives only as long as the
  session;
- **people's memory.**

Session-only schedules die with the session, and nobody sees them. Timer jobs fail silently unless
each script brings its own alerting, and none of it shows up next to the open items or the plan.

Waypoint 2.0 adds **recurring items**: a schedule that turns into ordinary items at the right
moments, for an owner to do, with the same wake-ups, ladders, views and history as any other item.
Jobs that stay on a timer can **check in**, so Waypoint can tell when a run was missed.

## 2. The decision that shapes everything: what fires

There are two options.

- **A. Waypoint wakes an owner.** At each occurrence it creates an item for the owner (an agent
  session or a person) and wakes them. The owner does the work.
- **B. Waypoint runs a command.** Waypoint becomes a job runner, with an executor, sandboxing,
  output capture, secrets for the commands, and retries.

**Recommendation: A, and never B.** The reasons:

- Waypoint's safety story rests on "the daemon never runs a command". It is in SECURITY.md, the
  design overview and the systemd hardening (an empty capability set, no network for the backup
  unit, a syscall filter). An executor would undo all of it and turn a token holder into someone
  who can run code.
- systemd and cron already run commands well: users, sandboxing, persistence across reboots,
  resource limits. Waypoint shouldn't compete with them; it should *see* them.
- Agent work on a schedule ("review the open issues every morning") needs a session to do it, not
  a command. That is exactly an item with an owner.

So 2.0 covers recurring work in two ways:

1. **Recurring items for owners.** Waypoint creates an occurrence item and wakes the owner (§3).
2. **Check-ins for jobs that stay on a timer.** A script reports each run, and Waypoint alerts when a
   run is missed (§6). The command still runs from systemd or cron.

## 3. The model

A **recurrence** (id `R-<n>`) is a template plus a schedule:

| Field | Meaning |
| --- | --- |
| `owner`, `track`, `title`, `label`, `kind`, `note`, `links`, `overdue` | The template for each occurrence. The same rules as items: content check, lengths, permissions. |
| `schedule` | When it occurs (§4). |
| `window` | How long an occurrence may take. It becomes the occurrence's `due_at` (occurrence time + window), so a missed occurrence climbs the normal overdue ladder. Default `1h`. |
| `overlap` | What to do when the next occurrence falls due while the previous one is still open: `skip` (the default), `replace` or `allow` (§5). |
| `catch_up` | After Waypoint's own downtime: `latest` (the default: one occurrence for the missed span) or `none` (§5). |
| `checkin` | `true` for a job that stays on a timer and reports its runs (§6). |
| `state` | `active`, `paused` or `stopped`; plus `next_at` and `last_at`, both set by Waypoint. |

Each **occurrence is an ordinary item** (`W-<n>`). It carries two new fields: `recurrence` (its `R-<n>`)
and `occurrence_at`. Occurrences are created only at the moment they fall due, never ahead of time,
in line with "a message exists only from the moment it falls due".

- **One item per run.** This follows the "one item per unit of work" rule: each run has its own
  history, can be marked done or dropped with a note, and shows as late when it isn't done in its
  window.
- **Everything else is reused:** `wp_mine`, wake-ups ("recurring: W-17 for R-3 is due"), the
  ladder, the views, the event stream and the content check.
- **The owner closes the occurrence like any item**, with a note.

## 4. How a schedule is written

The schedule is a small, closed grammar, not cron syntax. It is easy to read in an item and to
validate, and it covers the common recurring jobs:

```json
{"every": "15m"}                                     // an interval, at least 5m; anchored to the creation time
{"every": "2h", "from": "2026-11-21T08:00:00+01:00"} // an interval with an anchor
{"daily": "09:00"}                                   // every day at 09:00
{"weekly": ["mon", "thu"], "at": "06:40"}            // on given weekdays
{"monthly": 1, "at": "04:30"}                        // on a day of the month (1-28, or "last")
```

- **Times are wall-clock times in the deployment's zone** (`site.tz`). A recurrence may name its own
  zone (`"tz": "Europe/Amsterdam"`).
- **Daylight saving:** a time that doesn't exist that day (clocks jump forward) fires at the first
  valid minute after it; a time that occurs twice (clocks go back) fires once, the first time.
  Intervals run on absolute time and ignore the change.
- **Not included:** seconds, sub-five-minute intervals, "every second Tuesday", holidays and full
  iCalendar RRULE. The scheduler passes every 15 seconds, and a five-minute floor keeps item volume
  sane.
- **The API shows the computed `next_at`** after every change, so a schedule can be checked before
  anyone relies on it.

## 5. Overlap, catch-up, pause and stop

- **Overlap:** what happens when the previous occurrence is still open as the next one falls due.
  - `skip` (default): no new occurrence. The open one gets a history line "R-3 occurrence at 09:00
    skipped: the previous one is still open", and the owner keeps one item, not a pile.
  - `replace`: the open one is dropped with the reason "superseded by the 09:00 occurrence", and a
    new one is created. This suits "latest only" work such as a report.
  - `allow`: a new occurrence every time. This suits independent runs.
- **Catch-up after downtime.** Waypoint already knows its own outages, from the scheduler's record
  of passes.
  - `latest` (default): at most one occurrence for the whole missed span. It is created at once,
    marked late with the outage note (as the ladder does today), and says how many occurrences were
    missed.
  - `none`: nothing for the missed span; the next one comes on schedule.
- **A late occurrence** (not done within its window) climbs the ladder like any item: a nudge to the
  owner, then the coordinator. A session that starts an occurrence (`running`) and then goes silent
  is caught too: its lease lapses, and a lapse re-arms the ladder, so the escalation goes on instead
  of stopping at the owner's last update. This is the **missed-run alert**. The recurrence's own
  `overdue` ladder, or the deployment's default, decides the steps.
- **Pause and stop.**
  - `paused`: no new occurrences; open ones stay as they are. Resuming starts again from the next
    scheduled time, without catching up the paused span.
  - `stopped`: final, and kept for the history, like a retired client. A changed schedule takes
    effect from the next occurrence.
- **Recurrences owned by a person** are created and changed only by that person or a coordinator:
  an agent can't put a schedule on a human's list (a five-minute one would mean hundreds of items a
  day). For agent-owned recurrences the item rules apply.
- **Retired owner:** a recurrence whose owner is retired pauses itself and tells the coordinator.
  This matches the retirement rules for clients.

## 6. Check-ins: jobs that stay on a timer

For a script that runs from systemd or cron (a backup, a prune, a collector), a recurrence with
`checkin: true` describes when it *should* run. The script reports each run:

```sh
wp recur done R-7 --note "backup ok, 41 MB"            # or POST /v1/recurrences/R-7/checkin
wp recur failed R-7 --note "disk full"                 # optional: an explicit failure
```

- **A check-in closes the oldest open occurrence**, the run that is overdue first, so a late run is
  never credited to a newer one (this matters with `overlap: allow`).
- **Setting one up:** the recurrence's owner is whoever acts when a run is missed (the job's
  maintainer, or the coordinator), and `checkin_by` names the client the script checks in as. That
  client may check in and do nothing else with the recurrence; the owner, coordinators and humans may
  check in too.
- **A run that doesn't report** within the window leaves the occurrence open and late, and the
  ladder tells the owner and then the coordinator. That is a dead man's switch for every timer, with
  one shared alert path.
- **An explicit failure** closes the occurrence as dropped, with the note, and tells the owner at
  once.
- **Authentication:** a script uses an ordinary client token, for example one `agent` client per
  host or per group of jobs, named in the recurrence's `checkin_by` by whoever may change the
  recurrence. A least-privilege right: it covers only `POST …/checkin`. No new role.
- **Waypoint still runs nothing.** The schedule in Waypoint is only an expectation; systemd or cron
  remains the source of truth for when the command runs. Keeping the two in step is the operator's
  job, and a mismatch shows up as missed runs.

## 7. How it shows

- **`wp_mine`:** occurrences are listed as ordinary items, with "(R-3)" after the title. A new
  section, "Recurring", lists the recurrences you own with their `next_at`, and paused ones marked.
- **MCP:** one new tool, `wp_recur`, to create, change, pause, resume and stop a recurrence. `wp_get`
  accepts `R-<n>`. That makes nine tools (`wp_messages` is the eighth).
- **`wp`:** `wp recur add|ls|show|update|pause|resume|stop|done|failed`.
- **API:**
  - `GET`/`POST /v1/recurrences`, `GET`/`PATCH /v1/recurrences/{id}` and
    `POST /v1/recurrences/{id}/checkin`;
  - `Item` gains `recurrence` and `occurrence_at`;
  - the event stream gains `event: recurrence`.
- **Views and the dashboard:**
  - the timeline view gains `upcoming`: the next occurrences of each active recurrence within the
    view's span, computed and never stored, which a page can draw as faint markers;
  - the per-human view lists that person's recurrences;
  - `/v1/status` gains the missed check-ins.

## 8. Contract and database

- **API compatibility:** every change is additive within `/v1` (new endpoints, new fields, a new
  event and delivery kind), so existing clients keep working. "2.0" is the product version: it
  marks the move of recurring work into Waypoint, not a breaking API change.
- **Migration 7:**
  - a `recurrences` table (template fields, schedule as JSON, window, overlap, catch-up, check-in,
    state, `next_at`, `last_at`, version, created and updated);
  - `items.recurrence` and `items.occurrence_at`, with an index;
  - `recurrence_history` for changes to a recurrence;
  - no change to existing rows.
- **Scheduler:** one more step per pass. For active recurrences with `next_at <= now`, apply the
  overlap and catch-up rules, create the occurrence in the same transaction, and advance `next_at`.
  Tests use `FakeClock` across DST changes, outages, pauses and every overlap policy.
- **Delivery kind:** `recurring` (wording in ref mode: "recurring"). `KINDS` and the contract enum
  move together, as a test enforces.

## 9. What it deliberately does NOT do

- **It runs no commands, ever.** Timers and cron keep running jobs; Waypoint only expects and
  watches them.
- **It doesn't replace systemd timers or cron.** Waypoint is not a scheduler for the machine.
- **No sub-five-minute schedules, no seconds, no calendars or holidays, no full RRULE.**
- **No occurrences created ahead of time,** and no editing of a single future occurrence. Pause or
  change the schedule instead.
- **No retries, output capture or logs of jobs.** A check-in note (one line, 200 characters,
  content-checked) is all it keeps.
- **No secrets in templates or notes.** The content rules apply unchanged.
