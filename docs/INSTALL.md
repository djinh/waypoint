# Installing Waypoint

Every setting mentioned here, with its default, is listed in [CONFIGURATION.md](CONFIGURATION.md).

This walks through a deployment on one Linux server with systemd: the daemon, its first clients,
a deliverer for the user whose agent sessions get wake-ups, the MCP server for those sessions,
backups and monitoring. Names here (`alice`, `agent-a`, `host-1`) are examples.

You need Python 3.11 or later, and `tmux` on hosts whose sessions are woken by typing.

**The order on a new host:**

1. the `waypoint` system user and the install (section 1);
2. the config (listen addresses, port, coordinator) and the state directory (section 1);
3. the first human and the coordinator, registered locally (section 2);
4. the unit, started (section 2), then every other client over the API (section 2);
5. a deliverer user unit for each Unix user whose recipients get messages (section 3);
6. the MCP server, configured once per Unix user that runs agent sessions (section 4);
7. then start the sessions, each with its client name in its environment.

## 0. Prerequisites and the code

On a Debian or Ubuntu machine (other systems: the same tools under their names):

```sh
sudo apt install python3 python3-venv git tmux curl    # Python 3.11 or later; tmux where agents run
git clone https://github.com/djinh/waypoint.git          # or unpack a release archive
```

Run the tests once if you like: `cd waypoint && python3 -m venv .venv && .venv/bin/pip install -e
'.[dev]' && .venv/bin/python -m pytest -q`.

## 1. The daemon

Create a system user and a virtual environment, and install Waypoint into it:

```sh
sudo useradd --system --home-dir /var/lib/waypoint --shell /usr/sbin/nologin waypoint
sudo python3 -m venv /opt/waypoint
sudo /opt/waypoint/bin/pip install /path/to/waypoint      # a checkout or a built wheel
```

Write `/etc/waypoint/waypoint.toml` (every key is in `config.example.toml`), in a directory you
create first (`sudo install -d -m 0755 /etc/waypoint`):

```toml
[server]
listen = ["127.0.0.1"]        # add another address of this machine to reach it from elsewhere
port = 7130

[store]
database = "/var/lib/waypoint/waypoint.db"

[site]
tz = "Europe/Paris"           # the zone people read times in
coordinator = "coordinator"   # who hears about escalations and outages (registered below)
```

Waypoint refuses `0.0.0.0` and `::`: name each address it should listen on. Every request but
`/v1/health` needs a token, so other accounts on the machine can't read it without one.

**Network exposure.** Waypoint speaks plain HTTP; tokens and item text travel unencrypted. On one
machine, loopback is enough. To reach it from other machines, listen on an address of an encrypted
network only (a WireGuard or other VPN interface), or keep it on loopback and reach it through an
SSH tunnel, or put a TLS-terminating reverse proxy on the same machine in front of `127.0.0.1`.
Never expose it to the internet, and never send its traffic across a network you don't trust.

Create the state directory (the unit would create it at its first start, but the first clients are
registered before that, in section 2) and check the configuration:

```sh
sudo install -d -o waypoint -g waypoint -m 0700 /var/lib/waypoint
sudo -u waypoint /opt/waypoint/bin/waypointd --config /etc/waypoint/waypoint.toml check --config-only
```

## 2. The first clients

Only a human may create human and coordinator clients, so on a fresh install the first human and
the coordinator are registered **locally**: `waypointd client …` works on the database directly,
with no HTTP and no token. Run it **as the `waypoint` user** (not as root: files SQLite creates
next to the database would then belong to root and lock the service out). It writes each token to
a new file, mode 0600, created before the database changes, and never prints it:

```sh
sudo install -d -o waypoint -g waypoint -m 0700 /var/lib/waypoint/new-tokens
sudo -u waypoint /opt/waypoint/bin/waypointd --config /etc/waypoint/waypoint.toml \
    client add alice human --label Alice --token-file /var/lib/waypoint/new-tokens/alice.token
sudo -u waypoint /opt/waypoint/bin/waypointd --config /etc/waypoint/waypoint.toml \
    client add coordinator coordinator --token-file /var/lib/waypoint/new-tokens/coordinator.token
```

Then hand each token file to the Unix user of that client, readable by that user only, at
`~/.config/waypoint/tokens/<name>.token` (the default the MCP server and `wp` look for):

```sh
sudo install -d -o alice -g alice -m 0700 ~alice/.config/waypoint ~alice/.config/waypoint/tokens
sudo install -o alice -g alice -m 0600 /var/lib/waypoint/new-tokens/alice.token \
    ~alice/.config/waypoint/tokens/alice.token
sudo shred -u /var/lib/waypoint/new-tokens/alice.token
```

(The same for the coordinator's token and its Unix user.) Now the service can start: it refuses to
start while `site.coordinator` names a client that isn't registered, which `check` tells you first.

```sh
sudo -u waypoint /opt/waypoint/bin/waypointd --config /etc/waypoint/waypoint.toml check
sudo cp systemd/waypoint.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now waypoint
curl http://127.0.0.1:7130/v1/health      # {"status":"ok","version":"…"}
```

The unit runs as `waypoint`, can write only `/var/lib/waypoint`, sees no home folders and runs
nothing else. Schema changes are applied at start; upgrading is installing the new version and
restarting (see "Upgrading and rolling back").

From then on the human does the rest over the API with `wp`, which also writes each new token to a
new 0600 file and never prints it. A coordinator can register agents, deliverers and readers too.

```sh
export WAYPOINT_NAME=alice        # wp reads ~/.config/waypoint/tokens/alice.token
wp client add host-1 --role deliverer --token-file host-1.token
wp client add agent-a --role agent --deliverer host-1 --token-file agent-a.token
wp client add pages --role reader --token-file pages.token          # e.g. a dashboard
wp client route alice host-1      # alice's messages go out through host-1 (e.g. as push)
```

Move each token file to its client's Unix user as above, to `~/.config/waypoint/tokens/<name>.token`
(the deliverer's too: `host-1.token` goes to the user it runs as, section 3). `wp client rotate NAME --token-file F`
replaces a token (the old one stops at once); `wp client retire NAME` retires a client once its
open items are handed over or closed.

### Check that it works

With the human's token (the `wp` that comes with the install; set `WAYPOINT_URL` if the port isn't
7130):

```sh
export WAYPOINT_NAME=alice
/opt/waypoint/bin/wp health                                   # ok 0.1.x
/opt/waypoint/bin/wp track put ops --title "Operations" --lead alice
/opt/waypoint/bin/wp add "Try Waypoint" --track ops --note "first item"
/opt/waypoint/bin/wp mine                                     # W-1 [next] Try Waypoint (alice, ops) · — first item
/opt/waypoint/bin/wp done W-1 "it works"
```

[USAGE.md](USAGE.md) goes on from here: the command line, the MCP tools, the HTTP API and the
adapters, with worked examples.

## 3. A deliverer

One deliverer runs for each host and user whose recipients get messages. It needs its token and a
config naming, per recipient, how to reach it (see `deliver.example.toml`): a tmux pane for an agent
session, a push URL for a human, or a webhook.

Its token file (`host-1.token` from section 2) goes to `~/.config/waypoint/tokens/host-1.token` of
the user it runs as, mode 0600; `token_file` in the config points there.

```sh
# as the user whose sessions run in tmux
pip install --user /path/to/waypoint        # or a virtual environment of their own
mkdir -p ~/.config/waypoint ~/.config/systemd/user
cp deliver.example.toml ~/.config/waypoint/deliver.toml     # then edit it
waypoint-deliver --config ~/.config/waypoint/deliver.toml --check
cp systemd/waypoint-deliver.service ~/.config/systemd/user/
systemctl --user enable --now waypoint-deliver
sudo loginctl enable-linger $USER           # keep it running without a login
```

Register each recipient with this deliverer (`wp client route agent-a host-1`). A recipient with no
deliverer has its messages sent to the coordinator instead. The tmux adapter types a message only
when the session's input box is empty and it isn't in the middle of a turn, never over someone's
text, and checks that the line was submitted; after 30 minutes without a
chance (4 hours while the session is busy working) it reports the message undeliverable, and
Waypoint tells the coordinator.

**Use `message = "ref"` for agent sessions** (under `[waypoint]` for all of a deliverer's tmux
recipients, or per recipient). A line typed into a session's pane arrives as that session's own
user input; nothing marks it as coming from someone else. With the default `"full"`, the typed text
includes item titles and notes, which other clients wrote, so any client could put words into an
agent's mouth. With `"ref"` the deliverer types only Waypoint's own words and the item id:

```
[waypoint] wake-up for W-12 (reminder): read it with wp_get W-12
```

and the session reads the item with its tools, where it is plainly data. Messages without an item
get a fixed sentence (for example the coordinator's outage summary: "Waypoint was down for a while
… check your items with wp_mine"), so in `"ref"` mode their details are not typed; `/v1/status`
shows silent deliverers and stuck messages. `"full"` stays the default only so that existing
configurations keep working.

## 4. The MCP server for agent sessions

`waypoint-mcp` gives a session Waypoint's seven tools. It is a stdio MCP server that the agent's
client starts per session; it takes everything from its environment:

| Variable | Meaning |
| --- | --- |
| `WAYPOINT_NAME` | The session's client name (required), e.g. `agent-a`. |
| `WAYPOINT_TOKEN_FILE` | The token file; default `~/.config/waypoint/tokens/$WAYPOINT_NAME.token`. |
| `WAYPOINT_URL` | Default `http://127.0.0.1:7130`. |

Register it once per Unix user that runs sessions. For Claude Code:

```sh
claude mcp add --scope user waypoint -- /path/to/waypoint-mcp
```

or in a project's `.mcp.json`:

```json
{"mcpServers": {"waypoint": {"command": "/path/to/waypoint-mcp", "env": {"WAYPOINT_URL": "http://127.0.0.1:7130"}}}}
```

The server inherits the session's environment, so whatever starts a session sets `WAYPOINT_NAME`
for it (for example in the session's systemd unit or start script), and the session's token is at
the default path (or `WAYPOINT_TOKEN_FILE` points to it). Tell your agents how to work with
Waypoint: [AGENTS.md](AGENTS.md) has the rules and a block to paste into their instructions.

## 5. Backups

```sh
sudo cp systemd/waypoint-backup.service systemd/waypoint-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now waypoint-backup.timer
```

Every night it writes `/var/lib/waypoint/backup/waypoint-YYYYMMDD.db` (a consistent copy of the
database) and `waypoint-YYYYMMDD.json` (clients, tracks, items with their history). Neither holds a
token: the database keeps only hashes and the export not even those. Let your normal backup pick up
that folder and prune old days. To restore, stop the service, put a copy back and remove the old
write-ahead files, which belong to the database being replaced:

```sh
sudo systemctl stop waypoint
sudo install -o waypoint -g waypoint -m 0600 /var/lib/waypoint/backup/waypoint-YYYYMMDD.db /var/lib/waypoint/waypoint.db
sudo rm -f /var/lib/waypoint/waypoint.db-wal /var/lib/waypoint/waypoint.db-shm
sudo systemctl start waypoint
```

## 6. Monitoring

- `GET /v1/health` without a token: is it up.
- `GET /v1/status` with any reading token (e.g. the `pages` reader): `ok`, and the lists of what
  needs attention: a scheduler that is behind, silent deliverers, recipients nobody can reach,
  deliveries stuck for 30 minutes, late items, items whose owner is gone, items running for a day
  without an update.
- `GET /metrics` with a reading token: the same as counts for Prometheus or a compatible collector.

Waypoint also tells the coordinator itself about silent deliverers, undeliverable messages,
escalations and outages.

## Upgrading and rolling back

Each version brings its schema changes as numbered migrations. `waypointd serve` applies the ones
the database hasn't had yet when it starts, all in one transaction, so a failed migration leaves
the database as it was and the service doesn't start. A database with a newer schema than the
installed code is refused rather than misread. The other `waypointd` commands (`backup`, `client …`)
never migrate: they run beside a live daemon, so they refuse a database whose schema differs from
their own version; start the upgraded daemon first.

Before upgrading, check your configurations against the new version without touching the
installation: install it into a scratch environment and run its checks (the daemon's check opens the
database read-only and never migrates it; run each deliverer's check as that deliverer's user):

```sh
python3 -m venv /tmp/waypoint-new && /tmp/waypoint-new/bin/pip install /path/to/new/waypoint
sudo -u waypoint /tmp/waypoint-new/bin/waypointd --config /etc/waypoint/waypoint.toml check
/tmp/waypoint-new/bin/waypoint-deliver --config ~/.config/waypoint/deliver.toml --check
```

To upgrade (pip installs only a version it doesn't have yet: every release that changes the code
has a new version number, see `waypointd --version`; if you ever install a modified checkout with
the same number, add `--force-reinstall --no-deps`):

```sh
sudo -u waypoint /opt/waypoint/bin/waypointd --config /etc/waypoint/waypoint.toml backup /var/lib/waypoint/backup
sudo systemctl stop waypoint
sudo /opt/waypoint/bin/pip install --upgrade /path/to/new/waypoint
sudo systemctl start waypoint
```

To roll back, reinstall the old version and put back the database copy from just before the
upgrade (an older version can't open a newer schema). Changes made since that copy are lost, so
roll back soon or not at all; the JSON export from the same backup shows what the items looked like.

```sh
sudo systemctl stop waypoint
sudo /opt/waypoint/bin/pip install /path/to/old/waypoint
sudo install -o waypoint -g waypoint -m 0600 /var/lib/waypoint/backup/waypoint-YYYYMMDD.db /var/lib/waypoint/waypoint.db
sudo rm -f /var/lib/waypoint/waypoint.db-wal /var/lib/waypoint/waypoint.db-shm
sudo systemctl start waypoint
```

## If Waypoint is down

Agents keep working; their tools answer "Waypoint unavailable". Nothing is lost: when it is back,
the first scheduler pass catches up (each overdue item goes straight to the step it has reached,
missed wake-ups go out marked late) and the coordinator gets one summary.
