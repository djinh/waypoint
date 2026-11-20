# Waypoint

Waypoint is a small service where a group of AI agent sessions keep their open items, record what
waits on what, and get woken up at the right time. People see the same picture and are asked the
questions only they can answer.

- **Items** with owners (agents or people), conditions on other items and times, due and wake-up
  times. Waypoint works out what is `next` and what is `waiting`, what is late, and what is at risk
  because something it waits on is late or was dropped.
- **Questions for people** say where to answer; the answer unblocks exactly the work that waited
  for it.
- **Wake-ups and an overdue ladder**: reminders, "you can start", "please re-plan", nudges and
  escalations to a coordinator, caught up correctly after downtime.
- **Recurring work**: a schedule that gives its owner an ordinary item each time (`daily 09:00`,
  `every 2h`), and check-ins for jobs that stay on a timer, so a missed run is noticed.
- **Delivery** into an agent's terminal (tmux, only into an empty input box and never over a
  person's typing), as a push notification, or as a webhook.
- **Interfaces**: an HTTP API, nine MCP tools for agent sessions, and the `wp` command line.
- **Safe defaults**: listens on localhost, a token on every request, the daemon never runs a
  command (the deliverer only calls `tmux` to type a line), refuses text that looks like secrets or
  addresses.

It is one daemon (`waypointd`) with an SQLite database, written in Python. MIT licence.

## Try it in two minutes

```sh
git clone https://github.com/djinh/waypoint.git && cd waypoint
python3 -m venv .venv && .venv/bin/pip install .         # Python 3.11+
mkdir -p state && printf '[store]\ndatabase = "state/waypoint.db"\n' > try.toml
.venv/bin/waypointd --config try.toml client add alice human --token-file alice.token
.venv/bin/waypointd --config try.toml serve &              # http://127.0.0.1:7130
export WAYPOINT_NAME=alice WAYPOINT_TOKEN_FILE=alice.token
.venv/bin/wp track put demo --title "Demo"
.venv/bin/wp add "Try Waypoint" --track demo --note "my first item"
.venv/bin/wp mine                                          # W-1 [next] Try Waypoint (alice, demo) · — my first item
```

## Documentation

| | |
| --- | --- |
| [docs/DESIGN-OVERVIEW.md](docs/DESIGN-OVERVIEW.md) | How it works and why: items, conditions, time, delivery, failures, roles |
| [docs/design/RECURRING.md](docs/design/RECURRING.md) | Recurring items and check-ins: the design |
| [docs/INSTALL.md](docs/INSTALL.md) | Installing on a server from scratch: daemon, clients, deliverers, MCP, backups, monitoring, upgrades |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every setting, its default and where it is set |
| [docs/USAGE.md](docs/USAGE.md) | Using it: `wp`, the MCP tools, the HTTP API, what the adapters send, with worked examples |
| [docs/AGENTS.md](docs/AGENTS.md) | Working with Waypoint: rules for agent sessions, and a block for your agents' instructions |
| [docs/API.md](docs/API.md), [docs/api/openapi.yaml](docs/api/openapi.yaml) | The HTTP API contract |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The code |

## Developing

```sh
uv run --with-editable . --with pytest --with pyyaml python -m pytest -q
python scripts/sync_openapi.py            # after editing docs/api/openapi.yaml
python examples/make_examples.py          # after changing the example scenario
```

Every change to `src/` bumps the version in `src/waypoint/__init__.py` (a test checks it against
`origin/main`); `python scripts/sync_openapi.py` then copies it into the API contract.

Issues and pull requests are welcome; [CONTRIBUTING.md](CONTRIBUTING.md) says how they are handled.
Security problems are reported privately: [SECURITY.md](SECURITY.md).

## Licence

MIT, see [LICENSE](LICENSE).
