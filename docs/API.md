# The Waypoint API

This is the prose for the HTTP API of `waypointd`. The contract itself is
[`api/openapi.yaml`](api/openapi.yaml) (OpenAPI 3.1, whose schemas are JSON Schema 2020-12); where
the two disagree, the YAML file wins and this page is a bug. Worked examples of the two read models
are in [`../examples/`](../examples/).

The MCP server, the `wp` command and the deliverers are thin clients of this API: everything they
do, they do through it.

## Conventions

- **JSON** in and out, UTF-8, under `/v1`.
- **A token on every request** but `GET /v1/health`: `Authorization: Bearer <token>`. Never in a
  URL. A request without a valid token gets `401 unauthorized` and nothing else, not even whether an
  item exists.
- **Times** are RFC 3339 with seconds and a zone. Waypoint stores and answers in UTC (`Z`). Input
  without a zone is refused. The deployment's time zone (`tz` in the views) is for display, and for
  days (below).
- **Unknown fields** in a request are refused (`400 invalid`), never stored or silently dropped;
  so are unknown query parameters. Bodies are JSON objects of at most 64 KiB.
- **No missing fields** in responses: a field without a value is `null` or `[]`, so readers never
  have to tell "absent" from "empty".
- **Compatibility:** within `/v1`, changes are additive (new fields, new endpoints, new enum
  values on fields documented as open). Readers ignore fields they don't know.

## Items

An item is one thing in flight: a task, a decision a human must take, an approval, or an event
that happens at a time by itself. Every item has an owner (an agent or a human, by client name) and
belongs to a track (the goal it serves; pages draw tracks as lanes).

| Field | Set by | Meaning |
| --- | --- | --- |
| `id` | Waypoint | `W-<n>`, never reused. |
| `owner` | writer | Client name of whoever acts next. Default: the caller. |
| `track` | writer | Track id. |
| `title` | writer | One line, at most 80 characters, readable without context. |
| `label` | writer | Optional short form, at most 24 characters, for tight places such as timeline stops. `null`: the page shortens the title itself. |
| `kind` | writer | `task`, `decision` (a human chooses), `approval` (a human says go), `event` (happens at a time by itself, like a soak ending). |
| `state` | both | `next`, `running`, `waiting`, `done`, `dropped`; see below. |
| `state_since` | Waypoint | When the item entered its current state. |
| `waits_on` | writer | Conditions: other items or times. All must be met before the item can move on. |
| `waited_on_by` | Waypoint | Items that have this one as a condition. |
| `question` | writer | For decisions (required) and approvals (optional): `text`, `options`, `recommendation`. |
| `running_until`, `running_overdue` | both | While `running`: until when the claim lasts, and whether the owner has been asked if it still runs (see Running is a lease). |
| `answer_at` | writer | For decisions and approvals: where the person answers, `{"issue": url}` (a comment on that issue or pull request) or `{"session": name}` (in that session; its client name, never a URL or id). Reported with `default`; see below. |
| `when` | writer | When it happens: `start`, optional `end` (a span such as a soak), and `precision`. `null`: undated. |
| `due_at` | writer | The deadline. Passing it unattended starts the overdue ladder. |
| `wake_at` | writer | When to remind the owner; may be before the due time. |
| `overdue` | writer | This item's own ladder; `[]` means the deployment's default. |
| `note` | writer | One line of current status, at most 200 characters. For a dropped item, the reason. |
| `links` | writer | Up to 10 `{url, label}`, http(s) only: issues, PRs, plans. |
| `late`, `at_risk`, `at_risk_from` | Waypoint | Derived flags; see below. |
| `overdue_steps` | Waypoint | Ladder steps taken since the current due time was set. |
| `version` | Waypoint | Goes up with every change; PATCH may send `if_version`. |
| `created_*`, `updated_*`, `done_at` | Waypoint | Who changed it when. `updated_by` is `null` when the last change was Waypoint's own (a derived state). |

`GET /v1/items/{id}` adds `history`: every change as an event (`created`, `changed`, `derived`,
`condition_met`, `ladder_step`, `message`), with who (`null` for Waypoint itself), when, and the
changed fields as `[old, new]`.

**Content rules.** Many agents and pages read every field, so no field may hold secrets, tokens,
signed URLs, network addresses, device ids or personal data. Waypoint refuses obvious ones (long hex
strings, addresses and the like) with `422 content`, naming the field and never echoing the text.
UUIDs and MAC addresses count as device and session ids.
A four-part dotted number counts as a package version, not an address, only with a Debian epoch
right before it (`8:7.1.1.43`) or a suffix right after it that starts with `+` or `~`
(`7.1.1.43+dfsg1-1`); `installed 10.0.0.1-1` is still refused.
Two exceptions in a link's URL: a git commit id (7–40 or 64 hex characters) as the path segment
right after `/commit/`, `/commits/`, `/blob/` or `/tree/`, and a UUID as a whole path segment (a
page's id); anywhere else in the URL, the query string included, they are refused as above.

### Conditions

On input a condition is `{"item": "W-120"}` or `{"time": "2026-03-12T08:00:00Z"}`. Waypoint
reports each with `met` and `met_at`:

- an item condition is met when that item is `done` (`met_at` = its `done_at`);
- a time condition is met when the time has passed (`met_at` = the time).

A dropped item never meets a condition: whatever waits on it stays `waiting` and becomes at risk,
and its owner is told to re-plan.

**Asking a human.** `POST /v1/items/{id}/ask` `{human, title, question, label?, track?, due_at?,
note?, links?}` creates a `decision` owned by that human and makes item `{id}` wait on it, in one
transaction, and answers `{asked, item}` (201). The caller needs the right to change `{id}`. The same
question again (same human, title and question text, still open, and `{id}` still waiting on it)
answers the existing decision with 200 and changes nothing, so a retry never puts a question on a
human's list twice.

**Where the answer goes (`answer_at`).** Every decision and approval says where the person answers.
Set it on create, on ask or with a PATCH (`null` goes back to the default):
`{"issue": "https://…/issues/31"}` (an `https` URL of an issue or pull request, without a `#…`
fragment or `?…` query, so `…/issues/31#issuecomment-5` is given as `…/issues/31`; `…/pull/`,
`…/pulls/` and `…/merge_requests/` count too) or `{"session": "agent-b"}` (an agent's or coordinator's client
name). Waypoint reports it with `"default": false`. When none was set and the item was created by an
agent or coordinator, it reports `{"session": <creator>, "default": true}`: the asking session. A
new decision or approval whose `links` include an issue or pull request must say where the answer
goes: without `answer_at` it is refused (`invalid`, field `answer_at`), because the person couldn't
tell whether to comment on the issue or answer in the session. Other kinds have `answer_at: null`
and refuse one.

`waits_on` in a PATCH replaces the list; `add_waits_on` adds to it. A write that would close a loop
(A waits on B waits on A, of any length, including an item waiting on itself) is refused with
`409 loop`, and `cycle` names the items of the loop.

### States

| State | Set by | Meaning |
| --- | --- | --- |
| `next` | Waypoint | Every condition is met; it can start. |
| `waiting` | Waypoint | At least one condition is open. |
| `running` | owner | Being worked on now. Stays `running` whatever its conditions do, as long as its lease lasts (below). |
| `done` | owner | Finished. Stays in the views for `done_days`, then only in the history. |
| `dropped` | owner | Not going to happen; `note` holds the one-line reason. |

On create, `state` may only be `running`; otherwise Waypoint derives `next` or `waiting`. In a
PATCH, `next` and `waiting` both mean "not running (any more)": Waypoint derives which applies. On a
done or dropped item they reopen it. Reopening a done item opens its condition again in every item
that waits on it: dependents that were `next` go back to `waiting` (`running` ones stay `running`),
and the owners of dependents that leave `next` get a `replan` message.

### Running is a lease

`running` means someone is working on the item now, and Waypoint doesn't take that on trust
forever:

- Setting `running` sets `running_until`: the time given (`running_until` in the same create or
  PATCH, in the future) or now plus the deployment's lease (`site.running_lease`, default 2 h).
- Every update of the item by its owner while it runs renews it: to the time given, or now plus
  the lease. A coordinator's or a human's update leaves it alone unless it gives a time. A PATCH
  that changes nothing (only `if_version`, or fields set to their current values) still counts as
  an update: it renews the lease and raises `version`.
- When `running_until` passes, the owner gets one `running_check` message ("W-12 was expected to
  finish running at 14:00. Still running? …") and `running_overdue` turns true, so a page can draw
  "running?".
- If the owner doesn't update the item within `site.running_grace` (default 1 h) after that check
  was **confirmed by its deliverer** (delivered, or given up on), Waypoint sets the item back to
  `next` (or `waiting` if a condition is open) with the note "running lapsed at HH:MM, no update",
  recorded in the history as Waypoint's own change. Counting from the confirmation means an owner
  whose pane was busy isn't punished for the delivery's delay; time Waypoint was down doesn't count.
- Leaving `running` clears all of it.

### Flags

Both flags are derived on every change and every scheduler pass, and both carry their reason, so a
page can say *why* without another request:

- **`late`**: past `due_at` and not done or dropped. The reason is the item's own `due_at`;
  `overdue_steps` shows how far the ladder has climbed (nudge, escalate, notify, flag).
- **`at_risk`**: the item is open and waits, directly or further back through open conditions, on
  something late or dropped. `at_risk_from` lists each such item upstream with `why` (`late` or
  `dropped`) and, for a late one, its `due_at`.

### Permissions

| Role | Reads | Writes |
| --- | --- | --- |
| `agent` | everything | its own items, and items it hands to another owner |
| `human` | everything | everything |
| `coordinator` | everything | everything |
| `deliverer` | its own delivery queue | confirmations of its own messages |
| `reader` | everything | nothing |

**Items handed over.** The agent that created a task or an event and handed it to another agent
(`owner` set on create, or changed later) keeps writing it, state included, while it is open: that
is how a service that files items for others keeps them current and closes them. Not when the item
is now owned by a human or a coordinator, and not after it was closed or dropped: then the owner's
decision stands. The creator may set it `done`, `dropped`, `next` or `waiting`, never `running` (nor
`running_until`): only the owner knows it is working on it. Its changes are in the history under its own name. An agent that only passed on an
item someone else created has no such right.

Only the owner, a coordinator or a human changes an item's state, besides the creator of a handed-over
task or event (above). One more exception: the agent that
asked a person a question (a decision or approval owned by a human, with a `question`, created by
that agent) may mark it `done` once answered, while it is still open, and nothing else, with a note that quotes the answer
in double quotes and says where it was given (a session or an issue), e.g.
`answered in session agent-b: "online, after nine"`. Without both, it is `400 invalid` (field
`note`). Anyone may add conditions to
their own items, on anyone's items. The caller's identity always comes from its token.

## Tracks

`GET /v1/tracks` lists tracks by `order`, then `id`; `?archived=true` includes archived ones.
`PUT /v1/tracks/{id}` creates or replaces one track (`title`, `lead`, `note`, `order`,
`archived`). Tracks are what pages draw as lanes; `lead` is the client that keeps the track's plan.

Who may write a track: any agent, human or coordinator may create a new one; an existing track is
replaced only by its lead, a coordinator or a human. Readers and deliverers get `403 forbidden`.

## Views

The views are read models for pages: one request returns everything a page draws, with every flag,
edge and reason already worked out. Both share these fields:

- `generated_at`, `now`: when it was computed, and the "now" its flags refer to.
- `tz`: the deployment's IANA time zone, for display and for days.
- `owners`: every client name used in the view (owners, leads, `created_by`, `updated_by`) with its
  `role` and display `label`. A name without a client (an owner that was retired) has role
  `unknown`. This is how a page knows which owners are humans.
- `tracks`, `items`, `edges`.

An **edge** `{from, to, met}` means `to` waits on `from`. Both ends of every edge are in `items`, and
every item an included item names in `at_risk_from` is in `items` too.

### `GET /v1/views/timeline`

For the time page: lanes per track, items placed by time, undated items in dependency order. The
page does the bucketing (24, 12, 6 or 3 hours) itself.

Query: `from` (default now minus `done_days`), `to` (default now plus 9 days), `track`
(comma-separated; default every track that is not archived).

Which items are in it:
- every open item (`next`, `running`, `waiting`) of the tracks shown, dated or not, also when its
  time is after `to` (the page folds those into a "later" column);
- done and dropped items whose `done_at` is at or after `from`;
- every item an included item waits on, also from other tracks or done long ago, so every edge can
  be drawn, and every item named in an `at_risk_from`.

`items` are ordered by track order, then dependency order (an item after what it waits on), then
id. `window` echoes `from` and `to`; `done_days` says how long closed items stay.

Placing items in time is the page's job, from these fields:
- `when.start`/`when.end` with `precision`: `minute` and `hour` are timestamps; **`day` is a plain
  date** (`"2026-03-12"`) in `tz`, meaning "some time that day", and is never drawn at 00:00;
- `due_at` for deadlines; `wake_at` is the owner's own reminder and usually not drawn;
- an item with neither `when` nor `due_at` is **undated**: it is drawn in dependency order only,
  never at a time. A page that wants to place it after what it waits on can follow the edges.

### `GET /v1/views/for/{human}`

For the "you first" page. `human` must be a client with role `human` (`404 not_found` otherwise).

- `entries`: that human's open items. First the ones that are their turn (`ready`: `next` or
  `running`), longest-waiting first by `since` (when it became their turn); then the ones not their
  turn yet, by `since` = created.
- Each entry has `unblocks`: the open items waiting on it, directly or further on, with `depth`
  (1 = direct), nearest first. Pages draw these as the chain the human's answer sets free.
- An entry that is not ready has `on_the_way`: the open items it still waits for, directly or
  further back, with `depth`. Empty for ready entries.
- `items` holds every item the entries name (the human's own, their chains, and any at-risk
  reasons), each once; `edges` are the dependencies among them; `tracks` are those items' tracks.

This view is deliberately only about the human's items. A page that also shows every owner's next
dated items, or all tracks with their progress, reads `GET /v1/views/timeline` as well; the two
views share item shapes and ids, so they combine without translation.

### `GET /v1/events`

A server-sent event stream (`text/event-stream`) for pages that update live. After every committed
change Waypoint sends one event per changed item and per changed track:

```
id: 1731571200000-42
event: item
data: {"id":"W-12","version":5,"state":"next","updated_at":"2026-03-10T10:31:00Z"}

id: 1731571200000-43
event: track
data: {"id":"release-2-4"}
```

The page then reloads what it shows (a view, or `GET /v1/items?changed_since=…`). Lines starting with
`:` are heartbeats (about every 25 seconds). Ids are `<run>-<seq>`: `run` changes when Waypoint
restarts. A client that reconnects with `Last-Event-ID: 1731571200000-42` gets the events after it,
or `event: reset` when they are no longer kept (the last 1000), when the id is from an earlier run
or unreadable, or when it read too slowly; after a reset, reload everything. Any reading role; the token goes in the
Authorization header (browsers' `EventSource` can't send one, so a page reads the stream through
its own backend).

## Deliveries

Waypoint decides *what* falls due; small deliverers decide *how* it reaches a recipient. Every
recipient is registered with one deliverer; the deliverer's token says who it is, and it only ever
sees its own recipients' messages.

### `GET /v1/deliveries?wait=60`

Returns the messages that fell due, are not confirmed, and are not under an unexpired lease (never
handed out, or handed out more than `lease_seconds` ago), oldest `fell_due_at` first. If there
are none, Waypoint holds the request for up to `wait` seconds (0–60, default 60) and answers as soon
as a message falls due, or with `"deliveries": []` at the end. The deliverer asks again straight
away. A message exists only from the moment it falls due: nothing is queued ahead of time.

**At least once.** A message handed out and not confirmed within `lease_seconds` is handed out
again, with `attempt` increased and the same `id`. A deliverer keeps the ids it is working on and
ignores repeats; after a crash it simply gets them again. If a deliverer is down, its messages wait
in Waypoint.

**Withdrawn messages.** An unconfirmed `wake`, `nudge` or `at_risk` message whose item becomes done
or dropped, or is changed by its owner, is withdrawn: it is never handed out (again), and a
confirmation of it is accepted but changes nothing. So is an `at_risk` warning when the late item
it names is changed, and an `unblocked` message whose item went back to `waiting` (something it
waits on was reopened). This is how a done item's wake-ups are
cancelled, and why a host that slept through a reminder doesn't type a stale one when it wakes.

A message (`Delivery`):

| Field | Meaning |
| --- | --- |
| `id` | `D-<n>`, stable across attempts. |
| `recipient` | The client it is for. |
| `kind` | What it is about (table below). |
| `item`, `items` | The item it is about, and other items it names (for `unblocked`, the item that was done). |
| `fell_due_at` | When it fell due. |
| `attempt` | 1, or more when handed out again. |
| `late` | `null`, or `{due_at, reason, down_from, down_until}` when it goes out later than due for a known reason: `waypoint_down` (Waypoint was down from–until) or `recipient_away` (the deliverer did not fetch it in time). |
| `text` | The full message for an agent, ready to type in, at most 1000 characters. |
| `summary` | The item's title and owner only, at most 140 characters. **The only text a push adapter may send**, because push channels may be public. |
| `links` | The item's links. |

| `kind` | Sent to | When |
| --- | --- | --- |
| `wake` | owner | The item's `wake_at` passed. |
| `unblocked` | owner | A condition was met (an item done, a time passed) and the item is now `next`. |
| `replan` | owner | An item it waits on was dropped. |
| `at_risk` | owner | Something it waits on reached the ladder's `flag` step. |
| `nudge` | owner | Ladder step: the item is late. |
| `escalate` | coordinator | Ladder step: the item is late and needs chasing or re-planning. |
| `notify` | a named human | Ladder step: a push with title and owner only. |
| `undeliverable` | coordinator | A deliverer gave up on a message. |
| `deliverer_silent` | coordinator | A deliverer has not fetched for 5 minutes. |
| `outage` | coordinator | One summary after Waypoint was down: how long, and what was caught up. |
| `running_check` | owner | A running item's `running_until` passed: still running? (Withdrawn by the owner's next update.) |

A message for a recipient that has no deliverer goes to the coordinator, its text starting
"For <recipient> (no deliverer registered): "; if the coordinator can't be reached either, it waits
for the recipient. A message first handed out more than 5 minutes after it fell due, because its
deliverer wasn't fetching, gets `late` with reason `recipient_away` and " (delivered late, due
14:00)" at the end of its text.

### `POST /v1/deliveries/{id}/ack`

Body `{"result": "delivered"}` or `{"result": "undeliverable", "detail": "..."}`. `undeliverable`
means the adapter gave up (the tmux adapter after 30 minutes without an empty input box); Waypoint
then escalates it, quoting `detail`, so `detail` passes the content check like any other field
(`422 content`; the message stays unconfirmed, so confirm it again without the detail). Confirming
is idempotent: the same message confirmed again returns the first confirmation. Another
deliverer's message is `404 not_found`.

## Clients

Every caller is a registered client with a name, a role and a token. `/v1/clients` is for
coordinators and humans only (everyone else gets `403 forbidden`). A coordinator manages agent,
deliverer and reader clients; human and coordinator clients only a human may create, rotate,
re-route, relabel or retire (a coordinator gets `403`).

- `GET /v1/clients`: every client, retired ones included, with its deliverer and, for a
  deliverer, when it last fetched.
- `POST /v1/clients` `{name, role, label?, deliverer?}`: registers a client and answers
  `{client, token}`. The token is in this answer only; Waypoint keeps its hash. Store it in a file
  only the client's user can read (`wp client add` does that).
- `POST /v1/clients/{name}/rotate`: a new token, in the answer only; the old one stops working at
  once.
- `PATCH /v1/clients/{name}` `{label?, deliverer?, retired?: true}`: change the label, register
  the client with a deliverer (`null`: none; its waiting messages then go to the coordinator), or
  retire it. Retiring is refused (`invalid`, field `retired`) while the client owns open items
  (hand them over or close them first), and for the coordinator: the configured one, or the last
  coordinator when none is configured. A retired client's token stops working and its deliverer
  registration ends: messages still waiting for it go to the coordinator if a deliverer reaches the
  coordinator, and are withdrawn otherwise. Its name stays taken, views show it with role
  `unknown`, and a question it asked no longer names it as the place to answer.

## Monitoring

`GET /v1/health` answers "ok" and the version without a token, for "is it up". Everything else
needs one; any role that reads (agent, human, coordinator, reader) may read these two:

- `GET /v1/status`: what needs attention. `scheduler` (the last pass, and `behind` when there was
  none for a minute), `deliverers` (each that serves someone: how many recipients, its last fetch,
  `silent` after 5 minutes), `recipients_without_deliverer`, `stuck_deliveries` (not confirmed 30
  minutes after they fell due), `late_items`, `unknown_owners` (open items whose owner isn't a
  registered client any more) and `running_stale` (running for more than a day without an
  update). `ok` is true only when all of these are empty and the scheduler isn't behind.
- `GET /metrics`: the same as counts in the Prometheus text format (`waypoint_items{state=…}`,
  `waypoint_items_late`, `waypoint_items_at_risk`, `waypoint_messages_waiting`,
  `waypoint_deliveries_stuck`, `waypoint_deliverer_last_seen_seconds{deliverer=…}`,
  `waypoint_scheduler_last_pass_seconds`, …).

## Errors

Every error is `{"error": {"code", "message", ...}}`. `message` is for humans and never echoes the
request body or a token.

| Status | `code` | When |
| --- | --- | --- |
| 400 | `invalid` | Malformed request, bad value or query parameter, unknown field. `field` names it; for a value that is too long or too short the message gives its length and the limit ("note: 260 characters, at most 200"), never the value. |
| 401 | `unauthorized` | No token, or not a valid one. |
| 403 | `forbidden` | The caller's role may not do this. |
| 404 | `not_found` | No such item, track, human, or not the caller's delivery. |
| 409 | `loop` | The write would close a loop of conditions; `cycle` names it. |
| 409 | `version_conflict` | `if_version` did not match. |
| 422 | `content` | A field looks like a secret, an address or personal data; `field` names it. |
| 503 | `unavailable` | Waypoint is starting or its database is busy; try again. |
| 500 | `unavailable` | An internal error; the details are in Waypoint's log, never in the answer. |

## Not in this document yet

Nothing at the moment: every endpoint of the design is described above.

## Examples

- [`examples/timeline.json`](../examples/timeline.json): `GET /v1/views/timeline`.
- [`examples/for-alice.json`](../examples/for-alice.json): `GET /v1/views/for/alice`.

Both come from one invented scenario, a small team shipping release 2.4: a staging soak running
until Wednesday, Alice's go for production and her choice of migration strategy, a production
release that waits on an approval, on another agent's migration tool and on late release notes (so
it, and the announcement after it, are at risk), a day-precision release date, an event expected
"about Friday", a time condition, an undated docs track, and a done and a dropped item. The scenario
and the derivations are in [`examples/make_examples.py`](../examples/make_examples.py); run it to
regenerate the files after changing either.
