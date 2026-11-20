# Waypoint: how it works

This is the overview for someone who has never run Waypoint: the problem, the ideas, and the
guarantees. The exact API is in [API.md](API.md), the code in [ARCHITECTURE.md](ARCHITECTURE.md),
installing it in [INSTALL.md](INSTALL.md), every setting in [CONFIGURATION.md](CONFIGURATION.md),
and using it in [USAGE.md](USAGE.md).

## The problem

Run several AI agent sessions for long enough and they start to depend on each other and on you. One
session waits for another's build; a release waits for your go-ahead; a reminder is needed at four
in the afternoon when a test run ends. Each session can keep its own to-do list, but then nobody can
see the whole picture, nothing tells the build session that the release is waiting for it, and a
reminder set inside a session is gone when that session restarts or its context is compacted.

Waypoint is one small shared place for that picture. Every session writes its open items there,
records what each one waits on, and is woken up when something it waits for happens, when a time
comes, or when one of its items runs late. People see the same data and can tell at a glance what is
waiting for them.

## What it is not

- **Not an issue tracker.** Issues, pull requests and documents stay where they are; an item links
  to them.
- **Not a notes store.** An item has a one-line status. The story of the work lives elsewhere.
- **Not a job runner.** Waypoint never runs a command, on its own machine or anywhere else; the
  only program a deliverer starts is `tmux`, to type a line. It records, reminds and escalates;
  acting is up to the agents and people.

## The pieces

```
  agent sessions ──(MCP tools)──┐
  scripts, people ──(wp)────────┼──>  waypointd  ──(HTTP API)──  pages, monitoring (read only)
                                │      SQLite
  deliverers <──(their queue)───┘
     │
     ├── tmux: types into an agent's terminal
     ├── push: a phone notification for a person
     └── webhook: anything else
```

- **`waypointd`**, one daemon with an HTTP API and an SQLite database. Everything goes through it.
- **The MCP server** gives each agent session nine tools (list mine, list, get, add, update, done,
  ask a person, read my messages, recurring items).
- **`wp`** offers the same on the command line.
- **Deliverers**, one per machine and user where recipients live, fetch the messages meant for them
  and hand each on: typed into a terminal, pushed to a phone, or posted to a URL.

Waypoint itself never logs in anywhere and never types into anything. Deliverers run as the user
whose sessions they reach, with their own configuration; the daemon holds no keys to other machines
or accounts.

## Items, tracks and conditions

An **item** is a single piece of open work. It comes in four kinds: a task; a decision a person has
to take; a go-ahead a person has to give; or an event that simply happens at some point (a test run
that ends). It has an owner (an agent or a person), a one-line title and status, and optionally
times and links. Items belong to **tracks**, the goals they serve; pages draw tracks as lanes.

An item can **wait on** conditions: other items (anyone's) and times. The owner says only what it
alone knows: that it is working on the item, that it is done, or that it was dropped. Waypoint works
out the rest, so no two views of an item can disagree:

- **next**: everything it waits for is there; it can start;
- **waiting**: something it waits for is still open.

"Working on it" is a lease, not a promise: a running item says until when, any update by its owner
extends that, and when the time passes the owner is asked whether it still runs. With no update an
hour after the question arrived, the item goes back to "next", so a timeline never shows work as
running all evening that nobody is doing.

Two warnings are worked out too: an item is **late** when its due time has passed, and **at risk**
when something it waits for, directly or further back, is late or was dropped. Both come with their
reason, so a page can say *why*.

Waypoint refuses any condition that would make work wait on itself, however indirectly (A on B, B on
A), because such a plan could never finish.

## Asking a person

A question for a person becomes a separate item that the person owns, carrying the question, the
options and the asking agent's recommendation. The agent's own item waits on it. When the person
decides and the item is marked done, exactly the items that were waiting on that answer are woken
up. Nobody has to poll for answers, and the person sees in one list everything that is waiting for
them, oldest first, with what each answer will unblock.

## Time

Every item can carry three kinds of time:

- **when** it happens, with an honest precision (a minute, an hour, or just a day: an item due "some
  time Tuesday" is never shown at midnight). Items without a time are drawn in the order of their
  dependencies only.
- **wake**: a reminder for the owner at a moment of its choosing.
- **due**: a deadline, together with what should happen when it passes and nobody has acted.

That answer is the **overdue ladder**: a short list of steps, each some time after the due time. The
steps are a fixed set: remind the owner, tell the coordinator (whoever in the group chases and
re-plans), push a notification to a named person, and flag the item so that everyone waiting on it
hears it is at risk. A group sets a default ladder; an item may bring its own. The ladder stops as
soon as the owner updates the item or anyone closes it; a new due time starts it afresh, and so does
an owner who claimed the item as running and then went silent until the claim lapsed. Every step
taken is recorded, so the history shows which kinds of work tend to slip.

## Recurring work

Work that comes back on a schedule is a **recurrence**: a template and a schedule (`daily 09:00`,
`weekly mon,thu 06:40`, `every 2h`) in the group's time zone. Each time it falls due, Waypoint creates
an ordinary item for its owner and wakes them, so recurring work has the same wake-ups, ladder,
history and pages as everything else, and survives restarts. If the previous one is still open, the
next is skipped by default. After Waypoint's own downtime, one late occurrence stands for the missed
ones. A job that stays on a system timer (a backup, a prune) **checks in** after each run; a run that
doesn't check in within its window is late and climbs the ladder like any item. Waypoint itself still
runs nothing. The details are in [design/RECURRING.md](design/RECURRING.md).

## Messages and delivery

Waypoint decides *what* someone needs to hear and *when*: a reminder, "the thing you waited for is
done", "please re-plan, something you waited on was dropped", a step of a ladder. A message comes
into existence only at the moment it falls due; nothing is queued for Friday before Friday.

Each recipient is registered with one deliverer. A deliverer's request for its queue is held open
until something falls due or a minute has passed. Messages therefore leave within seconds, and a
deliverer with nothing to do makes about one request per minute. Each message has a stable id and
must be confirmed; until it is, it keeps coming back, so a crash loses nothing and a repeat is
recognisable. A message that went stale before anyone confirmed it, because its item was closed or
its owner changed it meanwhile, is withdrawn instead of delivered. A deliverer that can't get a
message through within half an hour (or within a few hours, while the recipient is busy working)
says so, and Waypoint tells the coordinator.

The **terminal adapter** deserves a word: it types a message into an agent's terminal only when the
agent's input box is empty (a greyed-out suggestion counts as empty), checks again right before
typing, never types over what someone else is writing, and sends the text literally so that it can
never act as key presses. The **push adapter** sends only an item's title and owner, because a push
channel may be readable by others.

## When things go wrong

- **Waypoint is down.** Agents keep working; their tools say "Waypoint unavailable". Because the
  scheduler records when each of its passes ran, a restarted Waypoint can tell exactly how long it
  was away. Each late item then jumps to the furthest ladder step its due time has reached, and
  takes that step once, rather than firing every missed step; missed reminders go out marked "late,
  Waypoint was down from–until", or are dropped if its owner changed the item meanwhile; and the
  coordinator gets one summary.
- **A recipient's machine is asleep.** Its messages wait and arrive when its deliverer is back,
  marked as delivered late. The ladder keeps running meanwhile, so a sleeping machine never hides a
  late item: the coordinator still hears about it.
- **A deliverer goes quiet** for five minutes: the coordinator is told.
- **A recipient has no deliverer**: its messages go to the coordinator.

## Who may do what

Every caller, person or program, is a registered client with a name, a role and a token. What a
request may do follows from its token alone; the address it comes from grants nothing.

| Role | May read | May change |
| --- | --- | --- |
| agent | everything | the items it owns; it may also create items for others |
| human | everything | everything, including clients |
| coordinator | everything | every item; agent, deliverer and reader clients |
| deliverer | its own queue | confirmations only |
| reader | everything | nothing (for pages and monitoring) |

Tokens are stored only as hashes, sent only in a request header, never printed or logged, and can be
replaced at once. Everything but the health check needs a token, because a listening address is open
to everyone who can reach it. Waypoint listens on the local machine only unless told otherwise,
refuses to listen on every address at once, and refuses text that looks like a secret, a network
address or personal data in any field, because many agents and pages read every item.

## Pages

Two read-only views give pages everything they draw in one request: a **timeline** (items placed in
time where they have a time, in dependency order where they don't, with the dependency edges and the
warnings) and a **for one person** view (what waits for that person, longest-waiting first, and for
each item the work that its answer sets free). A stream of change events lets pages refresh as soon
as something changes.

## Running it

One daemon started at boot, one SQLite file in its own folder, a nightly copy for the host's backup,
and a status endpoint that lists what needs attention: a scheduler that stopped, quiet deliverers,
stuck messages, late items, items whose owner is gone, and items that have said "running" for a day
without a word. [INSTALL.md](INSTALL.md) has the steps.
