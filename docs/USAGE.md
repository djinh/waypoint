# Using Waypoint

How to talk to a running Waypoint: the command line (`wp`), the MCP tools agents use, the HTTP API
underneath both, and what the delivery adapters send. The examples follow one small story: an agent
`builder` prepares a release that waits on a decision by a person, `alice`. The outputs are real.
`wp` and the MCP tools show times, and read times typed without a zone, in the zone of the machine
they run on; messages from Waypoint itself use the deployment's zone (`site.tz`). Installing and configuring are in
[INSTALL.md](INSTALL.md) and [CONFIGURATION.md](CONFIGURATION.md); the full API contract is
[API.md](API.md) and [api/openapi.yaml](api/openapi.yaml).

## The command line: `wp`

`wp` reads `WAYPOINT_URL`, `WAYPOINT_NAME` and `WAYPOINT_TOKEN_FILE` (or `--url`, `--name`,
`--token-file`). Text by default, `--json` for the API's answers; exit codes 0 done, 1 refused by
Waypoint, 2 usage or input, 3 Waypoint unavailable. `wp --help` and `wp <command> --help` list
every option with the length limits.

A track to put the work in (its lead may change it later):

```console
$ wp track put release --title "Release 2.4" --lead builder
release: Release 2.4
```

As `builder`: an item with a due time (a time without a zone is read in the local zone), then a
question for `alice` that the item waits on:

```console
$ wp add "Production release of 2.4" --track release --label "production release" --due "2026-11-21 17:00"
W-1 [next] Production release of 2.4 (builder, release) · due Sat 17:00
$ wp ask alice "Choose the migration strategy" "Online, or in an offline window?" \
      --option online --option "offline window" --recommend online --for W-1
W-2 [next] Choose the migration strategy (alice, release)
W-1 waits on it.
$ wp mine
W-1 [waiting] Production release of 2.4 (builder, release) · due Sat 17:00 · waits on W-2
```

As `alice`, what is asked and where to answer:

```console
$ wp show W-2
W-2 [next] Choose the migration strategy (alice, release)
question: Online, or in an offline window?
answer: in session builder (default: the asking session)
waited on by: W-1
  23:28 builder created
```

When the answer is given and the question closed (by `alice`, a coordinator, or `builder` with a
note that quotes the answer and says where it was given), `W-1` becomes `next` and `builder` gets
an "unblocked" wake-up:

```console
$ wp done W-2 "online, after nine"
W-2 [done] Choose the migration strategy (alice, release) · — online, after nine
```

Other commands: `wp ls` (filters `--owner`, `--track`, `--state`, `--waiting-on`, `--late`,
`--at-risk`), `wp update ID …` (state, note, due, wake, when, conditions, `--running-until`,
`--answer-issue`/`--answer-session`), `wp drop ID REASON`, `wp tracks`, `wp client …` (humans and
coordinators), `wp health`.

## The MCP tools (for agent sessions)

`waypoint-mcp` gives an agent seven tools. Answers are short text, one line per item, as above.
Line breaks and other control characters in text other clients wrote are shown as spaces (in `wp`
too), so such text never starts a line of its own.

| Tool | Parameters | Does |
| --- | --- | --- |
| `wp_mine` | none | My open items, what waits on them, and what is due or wakes in the next 24 hours. Call it after every start or context loss. |
| `wp_list` | `owner`, `track`, `state` (comma list or `all`), `waiting_on` | Anyone's items, filtered. |
| `wp_get` | `id` | One item in full: question, where to answer, links, risks, recent history. |
| `wp_add` | `title`, `track`, `kind`, `owner`, `label`, `waits_on` (ids or times), `due`, `wake`, `when`, `until`, `note`, `links`, `running_until` | A new item; with `running_until` it starts running. |
| `wp_update` | `id`, `state`, `note`, `due`, `wake`, `when`, `until`, `add_waits_on`, `waits_on`, `title`, `label`, `owner`, `answer_at`, `running_until` | Change one of my items; `''` clears a field. |
| `wp_done` | `id`, `note` | Mark it done; Waypoint wakes whoever waited on it. |
| `wp_ask` | `human`, `question`, `title`, `options`, `recommendation`, `for_item`, `track`, `due`, `answer_at`, `links` | Put a decision to a person and make my item wait on it, in one step. |

A typical start of a session's turn after a restart:

```text
wp_mine()
→ Open items of builder:
  W-1 [waiting] Production release of 2.4 (builder, release) · due Sat 17:00 · waits on W-2
```

(Anything due or waking within 24 hours is listed after the items under "Next 24 hours".)

Errors come back as the API's code and message, e.g. `invalid (note): note: 230 characters, at most
200`, or `Waypoint unavailable` when the daemon can't be reached.

## The HTTP API

Everything above is a thin client of the HTTP API: JSON under `/v1`, a bearer token on every
request but `GET /v1/health`.

```console
$ TOKEN=$(cat ~/.config/waypoint/tokens/builder.token)
$ curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:7130/v1/items/W-1
{"id": "W-1", "state": "waiting", "owner": "builder",
 "waits_on": [{"item": "W-2", "met": false, "met_at": null}],
 "due_at": "2026-11-21T16:00:00Z", "late": false, "running_until": null, …}
$ curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
       -d '{"track": "release", "title": "Release notes for 2.4", "kind": "task", "note": "draft half done"}' \
       http://127.0.0.1:7130/v1/items
{"id": "W-3", "state": "next", "version": 1, …}
$ curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
       -d '{"note": "…230 characters…"}' http://127.0.0.1:7130/v1/items/W-3
{"error": {"code": "invalid", "message": "note: 230 characters, at most 200", "field": "note"}}
```

The main endpoints (all in [API.md](API.md)):

| Endpoint | For |
| --- | --- |
| `GET /v1/items`, `POST /v1/items`, `GET`/`PATCH /v1/items/{id}` | Items: list with filters, create, read with history, change |
| `POST /v1/items/{id}/ask` | Ask a person, in one step |
| `GET /v1/tracks`, `PUT /v1/tracks/{id}` | Tracks |
| `GET /v1/views/timeline`, `GET /v1/views/for/{human}` | Read models for pages |
| `GET /v1/events` | Server-sent change events |
| `GET /v1/deliveries`, `POST /v1/deliveries/{id}/ack` | Deliverers: fetch (long-poll) and confirm |
| `/v1/clients` | Register clients, rotate tokens (humans and coordinators) |
| `GET /v1/status`, `GET /metrics`, `GET /v1/health` | Monitoring |

## What the deliverers send

When a message falls due (a wake-up, "can start", "please re-plan", an overdue step, "still
running?"), the recipient's deliverer hands it to the adapter its configuration names.

**tmux**, typed into an agent's terminal, only when the input box is empty and the session isn't in
the middle of a turn, then submitted with Enter and checked. With `message = "full"`:

```text
[waypoint] W-2 (Choose the migration strategy) is done; W-1 (production release) can start
```

With `message = "ref"` (recommended for agents; no text written by other clients reaches the pane):

```text
[waypoint] wake-up for W-1 (can start): read it with wp_get W-1
```

**push**, a plain-text POST with the item's title and owner only (push channels may be public),
with a `Title` header (and `Priority` if configured):

```text
Production release of 2.4 (builder)
```

**webhook**, a JSON POST of the whole delivery:

```json
{"delivery": {"id": "D-4", "recipient": "builder", "kind": "unblocked", "item": "W-1", "items": ["W-2"],
              "fell_due_at": "2026-11-19T22:28:41Z", "attempt": 1, "late": null,
              "text": "W-2 (Choose the migration strategy) is done; W-1 (production release) can start",
              "summary": "Production release of 2.4 (builder)", "links": []}}
```

An endpoint that answers 2xx has it; 4xx (except 408 and 429) makes it undeliverable at once; other
failures are retried, and after 30 minutes (4 hours while a tmux session is busy) the message is
reported undeliverable and the coordinator is told.

## Writing your own deliverer or client

Anything that speaks HTTP can be a client. A deliverer is a client with role `deliverer`: it
long-polls `GET /v1/deliveries?wait=60`, hands each message on, and confirms it with
`POST /v1/deliveries/{id}/ack` (`{"result": "delivered"}` or `{"result": "undeliverable",
"detail": "…"}`). A message it doesn't confirm comes back after its lease (120 s) with the same id,
so keep the ids you are working on and ignore repeats. `src/waypoint/client.py` is a small client in
the standard library only, and `src/waypoint/deliver.py` a complete deliverer to start from.
