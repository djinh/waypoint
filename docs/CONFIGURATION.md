# Configuration reference

Every setting a Waypoint deployment makes, where it is set, its default and what it does. Waypoint
ships with safe defaults: it listens on `127.0.0.1` only, every request but the health check needs
a token, and it never runs a command. Your own values (addresses, names, paths, push URLs) belong in
your configuration files, never in this repository.

| Where | What | Read by |
| --- | --- | --- |
| `waypoint.toml` (`--config`) | the daemon: listening, database, time zone, timings | `waypointd` |
| `deliver.toml` (`--config`) | one deliverer: its token and how each recipient is reached | `waypoint-deliver` |
| environment `WAYPOINT_*` | a client's name, token file and the daemon's address | `waypoint-mcp`, `wp` |
| token files | each client's token | every client |
| systemd units | users, paths, start, hardening, backup schedule | systemd |
| the MCP client's config | how agent sessions start `waypoint-mcp` | e.g. Claude Code |

## 1. The daemon: `waypoint.toml`

Passed as `waypointd --config FILE …`. Every key is optional; the file may be empty or absent (then
all defaults apply). Unknown sections are refused. The commented example is
[`config.example.toml`](../config.example.toml).

| Key | Default | What it does |
| --- | --- | --- |
| `server.listen` | `["127.0.0.1"]` | The IP addresses to listen on, each named. `0.0.0.0`, `::` and host names are refused. Add a private-network address of the machine to reach Waypoint from other hosts. |
| `server.port` | `7130` | The TCP port, the same on every address. |
| `store.database` | `/var/lib/waypoint/waypoint.db` | The SQLite file. Created and migrated at start; the directory must exist and be writable by the daemon's user. |
| `site.tz` | `UTC` | IANA time zone for times shown to people and for day-precision dates (`when` with precision `day`). Stored times are always UTC. |
| `site.done_days` | `3` | How many days done and dropped items stay in the views. |
| `site.coordinator` | first client with role `coordinator` | The client told about escalations, undeliverable messages, silent deliverers and outages. If set, it must be a registered coordinator, or `waypointd serve` refuses to start. |
| `site.default_ladder` | nudge at `0m`, escalate and flag at `1h` | The overdue ladder for items without their own: a list of `{after, action[, to]}`, `after` a delay like `0m`, `90m`, `2h`, `1d`; `action` one of `nudge`, `escalate`, `notify` (needs `to`, a human client), `flag`. |
| `site.running_lease` | `2h` | How long `running` lasts unless the owner renews it with an update. |
| `site.running_grace` | `1h` | How long after the "still running?" check was delivered the item goes back to `next` without an update. |

**Fixed timings** (not configurable; listed so operators know them): scheduler pass every 15 s;
a gap of more than 60 s between passes is an outage; delivery lease 120 s; a deliverer silent for
5 minutes is reported; confirmed or withdrawn messages are pruned after 30 days.

## 2. A deliverer: `deliver.toml`

Passed as `waypoint-deliver --config FILE`. One file per deliverer, kept by the Unix user it runs
as. The commented example is [`deliver.example.toml`](../deliver.example.toml).

### `[waypoint]`

| Key | Default | What it does |
| --- | --- | --- |
| `url` | `http://127.0.0.1:7130` | The daemon's address. |
| `token_file` | required | This deliverer's token file (see section 4). |
| `prefix` | `"[waypoint] "` | Put in front of every line typed into a pane. |
| `message` | `"full"` | Default for tmux recipients: `"full"` types the message text, `"ref"` types only a fixed sentence with the item id. Use `"ref"` for agent sessions (see section 2.1). |

### `[recipient.<client name>]`

One table per recipient registered with this deliverer (`wp client route NAME DELIVERER`). The
name is the recipient's client name.

| Key | Adapter | Default | What it does |
| --- | --- | --- | --- |
| `adapter` | all | required | `tmux`, `push` or `webhook`. |
| `target` | tmux | required | The tmux target: session, window or pane (e.g. `main`, `agents:2.0`). |
| `socket` | tmux | the default server | A socket name (`tmux -L NAME`), or a socket file path starting with `/` (`tmux -S PATH`; Unix limits such paths to about 100 bytes). |
| `prefix` | tmux | `[waypoint].prefix` | Per-recipient prefix. |
| `message` | tmux | `[waypoint].message` | `"full"` or `"ref"` for this recipient. Refused on other adapters. |
| `url` | push, webhook | required | `http(s)` URL. Push: a topic URL that accepts a plain-text POST (e.g. an ntfy-style service); webhook: any endpoint that accepts JSON. |
| `title` | push | `"Waypoint"` | The push notification's title header. |
| `priority` | push | none | The push service's `Priority` header, if it has one. |

### 2.1 `message = "ref"`

A line typed into an agent's pane arrives as that agent's own input. With `"full"` it includes item
titles and notes written by other clients; with `"ref"` it is only, for example,
`[waypoint] wake-up for W-12 (reminder): read it with wp_get W-12`, and the agent reads the item
with its tools. Messages without an item then carry no details in the pane.

**Fixed timings of the deliverer**: it waits up to 60 s per long-poll (5 s while it has work);
retries a message every 5 s; gives up after 30 minutes of being blocked (input box not empty, a
menu, no pane) or 4 hours of the recipient being busy; waits 60 s after clearing its own line that
wasn't taken; checks 1 s after Enter that the line was submitted.

## 3. Clients: environment for `waypoint-mcp` and `wp`

| Variable | `wp` flag | Default | What it does |
| --- | --- | --- | --- |
| `WAYPOINT_NAME` | `--name` | none; required by `waypoint-mcp` and `wp mine` | The client's name (an agent session's name, a person's name). |
| `WAYPOINT_TOKEN_FILE` | `--token-file` | `~/.config/waypoint/tokens/$WAYPOINT_NAME.token` | The client's token file. |
| `WAYPOINT_URL` | `--url` | `http://127.0.0.1:7130` | The daemon's address. |

## 4. Clients and tokens

Every caller is a registered client with a name (lower case letters, digits and `-`, up to 48
characters), a role and a token:

| Role | For |
| --- | --- |
| `human` | a person: reads and writes everything, manages clients |
| `coordinator` | the agent that chases and re-plans: writes every item; manages agent, deliverer and reader clients |
| `agent` | an agent session: writes its own items and items it creates for others |
| `deliverer` | one per host and Unix user that delivers messages |
| `reader` | a page or a monitor: reads only |

- **The first human and coordinator** are registered on the database directly, as the daemon's Unix
  user: `waypointd --config FILE client add NAME human|coordinator --token-file PATH`. Every other
  client is registered over the API by a human or coordinator: `wp client add NAME --role ROLE
  --token-file PATH [--deliverer DELIVERER]`.
- **Token files** are created new with mode 0600 and never overwritten; the token is never printed.
  Waypoint stores only a hash. Put each file where its client's Unix user, and only that user, can
  read it; by default `~/.config/waypoint/tokens/<name>.token` (directory mode 0700).
- **Routing**: a recipient's messages go through the deliverer it is registered with
  (`wp client route NAME DELIVERER`, `-` for none). Messages for a client without a deliverer go to
  the coordinator.
- `wp client rotate NAME --token-file NEWFILE` replaces a token; the old one stops at once.
  `wp client retire NAME` retires a client once its open items are handed over or closed.

## 5. The MCP server for agent sessions

`waypoint-mcp` is a stdio MCP server; the agent's client starts it per session. It reads the three
variables of section 3. Register it once per Unix user, for example in Claude Code:

```sh
claude mcp add --scope user waypoint -- /path/to/waypoint-mcp
```

and make sure whatever starts each session sets `WAYPOINT_NAME` (the server inherits the session's
environment), with the session's token at the default path or named by `WAYPOINT_TOKEN_FILE`.

## 6. systemd units

Examples in [`systemd/`](../systemd/); adjust them to your installation.

| Unit | Settings you are likely to change |
| --- | --- |
| `waypoint.service` (system) | `User=`/`Group=` (default `waypoint`), `ExecStart=` (path to `waypointd`, `--config` path), `StateDirectory=waypoint` (the database folder, `/var/lib/waypoint`), `ConfigurationDirectory=waypoint` (`/etc/waypoint`). Hardening options are meant to stay as they are. |
| `waypoint-deliver.service` (user) | `ExecStart=` (path to `waypoint-deliver`, `--config` path, default `~/.config/waypoint/deliver.toml`). Run `loginctl enable-linger USER` so it runs without a login. |
| `waypoint-backup.service` + `.timer` (system) | `ExecStart=` (the backup folder, default `/var/lib/waypoint/backup`); `OnCalendar=` in the timer (default 03:30 daily). |

## 7. Backups

`waypointd --config FILE backup DIR` writes `waypoint-YYYYMMDD.db` (a consistent copy of the
database) and `waypoint-YYYYMMDD.json` (clients without token data, tracks, items with history) into
`DIR`, mode 0600, replacing the same day's pair. Run it as the daemon's user; it never migrates the
database, and refuses one whose schema differs from its own version. Prune old days with your
normal backup tooling.

## 8. A worked example

A small setup: one server with the daemon, one Unix user `agents` running two agent sessions in
tmux, and a person who gets push notifications.

`/etc/waypoint/waypoint.toml`:

```toml
[server]
listen = ["127.0.0.1", "192.0.2.10"]   # loopback and the machine's private-network address
port = 7130

[site]
tz = "Europe/Paris"
coordinator = "coordinator"
running_lease = "3h"
```

`~agents/.config/waypoint/deliver.toml`:

```toml
[waypoint]
url = "http://127.0.0.1:7130"
token_file = "~/.config/waypoint/tokens/host-1.token"
message = "ref"

[recipient.builder]
adapter = "tmux"
socket = "builder"      # tmux -L builder
target = "main"

[recipient.reviewer]
adapter = "tmux"
socket = "reviewer"
target = "main"

[recipient.alice]
adapter = "push"
url = "https://push.example.org/alice-topic"
```

Clients: `alice` (human), `coordinator`, `host-1` (deliverer), `builder` and `reviewer` (agents,
routed to `host-1`), and `alice` routed to `host-1` as well. Each agent session is started with
`WAYPOINT_NAME=builder` or `WAYPOINT_NAME=reviewer`, its token in
`~agents/.config/waypoint/tokens/`.
