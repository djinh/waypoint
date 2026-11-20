# Architecture

Waypoint is one daemon, `waypointd`, with an HTTP API. Everything else (the MCP server, `wp`,
the deliverers, pages) is a client of that API. This page describes the code; the API itself is
in [API.md](API.md) and [api/openapi.yaml](api/openapi.yaml).

## Modules (`src/waypoint/`)

| Module | Does |
| --- | --- |
| `server.py` | `waypointd`: loads the config, opens the database, binds the listen addresses, runs uvicorn. Also `waypointd client add/rotate` for registering clients locally (tokens go to a 0600 file, never to the terminal). |
| `config.py` | The TOML configuration and its safety checks (loopback by default, wildcard addresses refused, a valid time zone and ladder). |
| `api.py` | The Starlette app: the token guard (pure ASGI middleware), the routes, query parsing, error responses. Handlers stay thin; the store does the work. |
| `store.py` | Clients, tracks and items in SQLite; validation beyond the schema; permissions; conditions and loop refusal; derivation of states and flags; history. |
| `monitor.py` | `/v1/status` (what needs attention), `/metrics` (Prometheus counts), and `waypointd backup` (a consistent database copy and a JSON export). |
| `events.py` | `/v1/events`: the broadcaster that turns committed changes into server-sent events. |
| `views.py` | The two read models, built from serialized items. |
| `scheduler.py` | The scheduler: a pass every 15 seconds re-derives what time changes, sends wake-ups, climbs overdue ladders and catches up after an outage. |
| `deliveries.py` | Handing messages to deliverers: the queue per deliverer, leases, the long-poll and its thread-safe signal, acknowledgements, withdrawal of stale messages, silent deliverers. |
| `client.py` | A small client for the API (standard library only), shared by the deliverer, the MCP server and `wp`. The token comes from a file and goes only into the Authorization header. |
| `deliver.py` | `waypoint-deliver`, the deliverer: long-polls its queue, hands each message to its recipient's adapter, retries, gives up after 30 minutes, confirms. |
| `mcp_server.py` | `waypoint-mcp`, the MCP server for one agent session: seven tools over the API (official MCP Python SDK, stdio). |
| `wp.py` | `wp`, the command line: the same operations for scripts and humans, plus tracks and clients. |
| `present.py` | Shared by the MCP server and `wp`: reading times, conditions and ids people type; one-line items. |
| `adapters.py` | The adapters: `tmux` (types into an empty input box only), `push` (the summary only), `webhook` (the delivery as JSON). |
| `messages.py` | `Outbox`: composes messages (text for agents, a title-and-owner summary for push) and writes them; turns `unblocked` and `replan` transitions into messages. |
| `schema.py` | Validates request bodies against the contract's own schemas (`openapi.json`, a copy of `docs/api/openapi.yaml`). |
| `content.py` | The content check on free text. |
| `db.py` | SQLite connection settings and the numbered migrations. |
| `clock.py`, `timeutil.py` | The time source (faked in tests) and timestamp/day parsing. |
| `errors.py` | `ApiError` and the error codes. |

## Data

One SQLite file in write-ahead mode, foreign keys on. `PRAGMA user_version` holds the schema
version; `db.MIGRATIONS` is the ordered list of migrations, each run once; all pending ones run in
one transaction, so a failure leaves the database as it was. An applied migration is never edited.

- `items`: one row per item. Ids are `W-<num>` over an `AUTOINCREMENT` key, so they are never
  reused. Derived values (`state` for next/waiting, `late`, `at_risk_from`) are stored, so reads
  and filters are cheap and every change to them is in the history.
- `conditions`: an item's conditions, each either another item (`dep`) or a time (`at`), with
  `met_at` once met.
- `history`: every change, by a client or by Waypoint itself (`by` is NULL).
- `clients`: name, role, label and the SHA-256 of the token (never the token).
- `messages`: every message Waypoint sends, written the moment it falls due, in the same
  transaction as its cause (deliveries hand them out; a later part).
- `wakes`: which wake-up (item, `wake_at`) was handled, and how: sent, dropped, cancelled.
- `ladder_steps`: the overdue steps taken (or skipped in a catch-up) per item and due time.
- `passes`: the time of every scheduler pass of the last week.

Confirmed or withdrawn messages are deleted after 30 days (each left a line in its item's history);
unconfirmed ones are kept however old, and so are items, their history, wake-ups and ladder steps:
the file grows with use (about a few kilobytes per item with its history), which SQLite handles
well into hundreds of thousands of items.

The database runs with `synchronous = NORMAL`: a crash of Waypoint loses nothing that was
committed, and the file stays consistent after a power loss or an OS crash, but the last
transactions before such a loss may be missing. A message is written in the same transaction as
its cause, so either both are kept or neither is.

## Writes and derivation

Every write runs in one transaction (`BEGIN IMMEDIATE`), and inside it, after the change,
`Store.recompute()` brings everything derived in line with the data and the clock:

1. conditions: an item condition is met when its item is done; a time condition when the time
   has passed;
2. states: `next` when every condition is met, else `waiting` (`running`, `done`, `dropped`
   are left alone);
3. `late`: past `due_at` and still open;
4. `at_risk_from`: open items reached upstream through open item conditions that are late or
   dropped.

It recomputes the whole graph each time. That keeps the rules in one place and the picture
consistent; a group of agents has hundreds of open items, not millions. Each change is written
with a history event, and the changes someone should hear about come back as `Transition`s
(`unblocked`, `replan`, `late`, `at_risk`). Listeners registered on the store get them inside the
same transaction, so a wake-up commits or rolls back together with the change that caused it.

## The scheduler

`Scheduler.run_pass()` is one transaction, run every `pass_seconds` (15) by a task in the app's
lifespan (the first pass runs before the app takes requests):

1. Record the pass. If the previous pass is more than `outage_seconds` (60) ago, Waypoint was down
   from that pass until now: the **outage**.
2. `recompute()`: time conditions met, `late` set; transitions become `unblocked`/`replan`
   messages.
3. **Wake-ups** whose time has come and that weren't handled: a `wake` message to the owner. An
   item that is done or dropped has its wake-up cancelled. A wake-up that fell due during an outage
   carries a late note, or is dropped if a client changed the item after its time.
4. **Ladders**: for each open item past `due_at` that its owner hasn't changed since then (after
   the change that set this due time; a coordinator's or a human's note doesn't count), the steps whose time has come and that weren't taken. A step's
   time is `start + after`, where `start` is the due time, or the moment the due time was set if
   that was later (an item created or re-dated already late starts with a nudge). After an
   outage only the steps at the furthest time are taken and earlier ones are recorded as skipped:
   no burst of every missed step. Steps:
   `nudge` → owner, `escalate` → coordinator, `notify` → the named human (title and owner only),
   `flag` → an `at_risk` message to the owner of every open item downstream.
5. **The running lease**: a running item whose `running_until` passed gets one `running_check`
   message to its owner (the message's number is kept on the item: that is `running_overdue`).
   When the deliverer confirms the check, the item's lapse time is set to that moment plus the
   grace; a pass after an outage moves pending lapse times later by the outage's length. At its
   lapse time the item goes back to `next`/`waiting` as Waypoint's own change. The owner's
   updates renew the lease and forget the check (and withdraw it if it's still queued). Items
   that were running before leases existed get one at the first pass (now plus the lease).
6. After an outage, one `outage` summary to the coordinator.

Each step is recorded with its messages in the same transaction, so it runs once, also across
restarts. Delays are absolute durations (`1d` is 24 hours, also across a daylight-saving change);
times in message texts are local to the deployment's zone.

## Deliveries

A recipient is registered with one deliverer (`clients.deliverer`, set with `waypointd client
route` or `client add --deliverer`). A deliverer's queue is every message of its recipients that is
not confirmed, not withdrawn and not under a lease. `fetch()` hands them out oldest first, taking a
lease (`lease_seconds`, 120) and raising `attempt`; it also records the deliverer's last contact.

`GET /v1/deliveries` is async: it registers with a `Signal`, looks, and if nothing is ready waits on
the signal until the wait is over or the earliest lease runs out. `Outbox.add()` fires the signal
for every new message, from whatever thread wrote it (`call_soon_threadsafe`), so a long-poll
answers within moments of a message falling due and holds no thread while it waits. Nothing is
handed out until the answer is being built, so a client that disconnects loses nothing: its
messages come again when their leases run out.

Withdrawal is hooked to the store: `Store.change_listeners` (the owner changed an item, or
someone closed it) and the
transitions (an item went back to `waiting`). A scheduler pass hook reports deliverers that serve
someone and haven't fetched for `silent_seconds` (300), once per silence.

Loop refusal happens in the same transaction as the write: after the new conditions are
inserted, a search from the item through item conditions looks for a way back to it; if there is
one the transaction rolls back and the API answers `409 loop` with the path.

## The deliverer

`waypoint-deliver --config deliver.toml` runs once per host and user where recipients live (see
`deliver.example.toml`). Its configuration says, per recipient, which adapter reaches it and how
(a tmux target, a push URL, a webhook URL); Waypoint never holds any of that.

The loop: fetch (`wait=60`, or 5 seconds while something is pending), take in the new messages
(ignoring ids already pending, and confirming without delivering again ids it delivered before
whose confirmation was lost), try each pending message whose retry time has come, and confirm what
is settled. An adapter answers delivered, retry, busy, or undeliverable with a short reason. A
message still not delivered 30 minutes after its first hand-out (4 hours while the recipient is
busy working) is confirmed `undeliverable` (Waypoint
escalates it); after a restart the first hand-out is estimated from `attempt` and the lease.
Waypoint being down means back-off (1, 2, 4 … 60 seconds); a host that slept just carries on.

**The tmux adapter** captures the pane with its colours (`capture-pane -p -e`), takes the input box
as the lines between the last two horizontal rules (a rule may carry a short label, such as the
session name Claude Code draws into the top rule of a named session), removes dim text (Claude Code's prompt
suggestion, `ESC[2m … ESC[0m` or `ESC[22m`) and colours, and types only if what is left after the
prompt sign is blank. No box (a menu, a dialog, another program) means no typing. It looks again
right before typing. Before each look it also asks tmux whether the pane is in a mode
(`#{pane_in_mode}`): in copy mode the screen still shows an empty box, but keys go to the mode and
the message would be lost. It also waits while a turn is running (Claude Code's status line above
the box reads "<verb>… (elapsed"): a line typed then can be taken as a paste and lose its Enter, and
the session would only read it at the end of the turn anyway. It sends the message as literal text (`send-keys -l --`, control characters and
newlines replaced by spaces; a trailing `;`, which tmux would read as a command separator, gets a
space after it) and then a separate `Enter`. About a second later it looks at the box again: if it
still holds exactly the typed line, it sends `Enter` once more; if the line is still there, it
clears it (`C-u`, then checks the box is empty) and tries the message again after at least a
minute (so a pane where Enter keeps failing isn't retyped every few seconds), so its own text never
blocks the pane. The first such case per message is logged, and the total when the message is
settled. Before typing, a box that holds exactly a line this adapter typed earlier
(remembered, or in "ref" mode one of its fixed sentences, which survives a restart) and nothing
else is cleared the same way. Any other text in the box (a person's, or ours with something added)
is never touched, and delivery waits. Each case is logged, without the text. (A line someone
recalls from the input history with the Up arrow is exactly ours too, so the next delivery clears
it; that is our own text, so nothing of theirs is lost.) In "ref" mode a line counts as ours only if
it names the same item twice, as the adapter writes it. With `message = "ref"` the typed text is
not the message's text but a fixed sentence (`adapters.ref_line`) built from a fixed word for the
message kind and the item id, checked against `W-<n>`; anything unexpected gives "check your items
with wp_mine". No client-written text (titles, labels, notes, owners) is ever typed in that mode.
A `socket` starting with `/` is a socket file (`-S`),
anything else a socket name (`-L`). Every tmux call has a timeout; a missing
tmux, server or pane means "try again later". Failure reasons never quote the pane.

## The MCP server

`waypoint-mcp` is started by the agent's client for each session, over stdio. It reads the
session's client name from `WAYPOINT_NAME`, the API's address from `WAYPOINT_URL` and the token
from `WAYPOINT_TOKEN_FILE` (default `~/.config/waypoint/tokens/<name>.token`). Its seven tools
(`wp_mine`, `wp_list`, `wp_get`, `wp_add`, `wp_update`, `wp_done`, `wp_ask`) each make one to a
few API calls and answer in short text, one line per item; descriptions stay under 200 characters
because they cost context in every session. Its only conveniences are on input: item ids and times
as plain strings (a time without a zone is read in the machine's local zone), a day for `when`, and
an empty string to clear a field. Every rule is the daemon's. Errors are the API's code and message,
or "Waypoint unavailable".

## The event stream

`Store.tx()` notes which items (every history event) and tracks a transaction touched, and after
the commit hands them to `Store.commit_listeners`. The `Broadcaster` turns each into one event with
a sequence number, keeps the last 1000 for clients that reconnect with `Last-Event-ID`, and puts it
on every subscriber's asyncio queue with `call_soon_threadsafe`. A full queue (a reader too slow)
is replaced by a single `reset`. Each stream checks every second whether its client went away, so
connections are let go at once; `waypointd serve` gives open streams 5 seconds when it stops.

## Requests

`TokenGuard` checks the bearer token before routing, so a request without a valid token learns
nothing (not even whether a path exists). Bodies are limited to 64 KiB, must be JSON objects, and
are validated against the contract's schemas, which refuse unknown fields. Error messages name the
field, never the value. Store calls run in a thread pool; the store serializes its own access to
the database connection.

## Tests

`tests/` uses pytest. Servers bind 127.0.0.1 on a port the OS hands out, databases live in
`tmp_path`, time comes from a `FakeClock`. `test_views_examples.py` replays the invented scenario
of `examples/make_examples.py` through the store and checks that the daemon's views equal the
committed examples.
