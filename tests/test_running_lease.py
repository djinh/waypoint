"""running is a lease (#46): running_until, a running_check when it passes, a lapse back to next an
hour after the check was confirmed without an update by the owner."""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from mcp import Client

from waypoint import config, wp
from waypoint.adapters import ref_line
from waypoint.client import WaypointClient
from waypoint.deliveries import Deliveries
from waypoint.errors import ApiError
from waypoint.mcp_server import build
from waypoint.scheduler import Scheduler
from waypoint.store import Client as C
from waypoint.timeutil import fmt

HOST = C("host-1", "deliverer")


@pytest.fixture
def sched(store, tokens, tracks):
    store.settings.outage_seconds = 10**9
    for name in ("agent-a", "agent-b", "coordinator"):
        store.route(name, "host-1")
    return Scheduler(store)


@pytest.fixture
def dl(store, sched):
    return Deliveries.of(store)


def at(store, **kw) -> str:
    return fmt(store.clock.now().replace(**kw))


def checks(store) -> list[dict]:
    return [dict(r) for r in store.conn.execute("SELECT * FROM messages WHERE kind = 'running_check' ORDER BY num")]


# ---------------------------------------------------------------------- the lease


def test_running_sets_the_default_lease(store, mk, who):
    it = mk("agent-a", "Build", state="running")
    assert it["running_until"] == "2026-03-10T11:30:00Z" and it["running_overdue"] is False
    other = mk("agent-a", "Other")
    assert other["running_until"] is None
    out = store.patch_item(who("agent-a"), other["id"], {"state": "running"})
    assert out["running_until"] == "2026-03-10T11:30:00Z"


def test_a_given_lease(store, mk, who):
    it = mk("agent-a", "Soak", state="running", running_until="2026-03-10T18:00:00+01:00")
    assert it["running_until"] == "2026-03-10T17:00:00Z"
    for body, field in (({"running_until": "2026-03-10T09:00:00Z"}, "running_until"),
                        ({"running_until": "2026-03-10T12:00:00Z", "state": "next"}, "running_until")):
        with pytest.raises(ApiError) as e:
            store.patch_item(who("agent-a"), it["id"], body)
        assert e.value.field == field
    with pytest.raises(ApiError) as e:
        mk("agent-a", "Not running", running_until="2026-03-10T12:00:00Z")
    assert e.value.field == "running_until"


def test_the_owners_updates_renew_it_others_dont(store, mk, who):
    it = mk("agent-a", "Build", state="running")
    store.clock.advance(hours=1)
    assert store.patch_item(who("agent-a"), it["id"], {"note": "half way"})["running_until"] == "2026-03-10T12:30:00Z"
    store.clock.advance(minutes=30)
    assert store.patch_item(who("coordinator"), it["id"], {"note": "checking"})["running_until"] == "2026-03-10T12:30:00Z"
    out = store.patch_item(who("coordinator"), it["id"], {"running_until": "2026-03-10T20:00:00Z"})
    assert out["running_until"] == "2026-03-10T20:00:00Z"  # an explicit time from someone else is used
    out = store.patch_item(who("agent-a"), it["id"], {"running_until": "2026-03-11T08:00:00Z", "note": "overnight"})
    assert out["running_until"] == "2026-03-11T08:00:00Z"


@pytest.mark.parametrize("state", ["next", "waiting", "done", "dropped"])
def test_leaving_running_clears_it(store, mk, who, state):
    it = mk("agent-a", "Build", state="running")
    out = store.patch_item(who("agent-a"), it["id"], {"state": state, "note": "stopped"})
    assert out["running_until"] is None and out["running_overdue"] is False
    row = store.conn.execute("SELECT running_until, running_check, running_lapse_at FROM items").fetchone()
    assert tuple(row) == (None, None, None)


# ---------------------------------------------------------------------- the check


def test_one_check_when_the_lease_passes(store, sched, dl, mk):
    it = mk("agent-a", "Build", label="build", state="running")
    store.clock.set(store.clock.now().replace(hour=11, minute=29, second=59))
    sched.run_pass()
    assert checks(store) == []
    store.clock.advance(seconds=1)
    sched.run_pass()
    (m,) = checks(store)
    assert (m["recipient"], m["item"]) == ("agent-a", 1)
    assert m["text"] == ("W-1 (build) was expected to finish running at 12:30. Still running? Extend it (any update), "
                         "mark it done, or set next or waiting.")
    assert store.get_item(it["id"])["running_overdue"] is True
    store.clock.advance(minutes=10)
    sched.run_pass()
    assert len(checks(store)) == 1
    assert ref_line({"kind": "running_check", "item": "W-1"}) == "wake-up for W-1 (still running?): read it with wp_get W-1"


def test_lapse_counts_from_the_confirmation(store, sched, dl, mk):
    it = mk("agent-a", "Build", state="running")
    store.clock.set(store.clock.now().replace(hour=11, minute=30))
    sched.run_pass()  # the check
    store.clock.advance(hours=3)  # the owner's pane was busy: not delivered yet
    sched.run_pass()
    assert store.get_item(it["id"])["state"] == "running"
    (d,) = [x for x in dl.fetch(HOST) if x["kind"] == "running_check"]
    dl.ack(HOST, d["id"], {"result": "delivered"})  # 14:30
    store.clock.advance(minutes=59, seconds=59)
    sched.run_pass()
    assert store.get_item(it["id"])["state"] == "running"
    store.clock.advance(seconds=1)
    sched.run_pass()
    got = store.get_item(it["id"], history=True)
    assert got["state"] == "next" and got["running_until"] is None and got["running_overdue"] is False
    assert got["note"] == "running lapsed at 16:30, no update"
    assert got["updated_by"] is None
    last = got["history"][-1]
    assert last["by"] is None and last["event"] == "derived" and last["changes"]["state"] == ["running", "next"]


def test_an_undeliverable_check_also_starts_the_grace(store, sched, dl, mk):
    it = mk("agent-a", "Build", state="running")
    store.clock.set(store.clock.now().replace(hour=11, minute=30))
    sched.run_pass()
    (d,) = [x for x in dl.fetch(HOST) if x["kind"] == "running_check"]
    dl.ack(HOST, d["id"], {"result": "undeliverable", "detail": "no chance within 30 minutes"})
    store.clock.advance(hours=1)
    sched.run_pass()
    assert store.get_item(it["id"])["state"] == "next"


def test_an_update_after_the_check_keeps_it_running(store, sched, dl, mk, who):
    it = mk("agent-a", "Build", state="running")
    store.clock.set(store.clock.now().replace(hour=11, minute=30))
    sched.run_pass()
    (d,) = [x for x in dl.fetch(HOST) if x["kind"] == "running_check"]
    dl.ack(HOST, d["id"], {"result": "delivered"})
    store.clock.advance(minutes=20)
    out = store.patch_item(who("agent-a"), it["id"], {"note": "still going, tests are slow"})
    assert out["running_overdue"] is False and out["running_until"] == "2026-03-10T13:50:00Z"
    store.clock.advance(hours=1)
    sched.run_pass()
    assert store.get_item(it["id"])["state"] == "running"


def test_an_update_before_delivery_withdraws_the_check(store, sched, dl, mk, who):
    it = mk("agent-a", "Build", state="running")
    store.clock.set(store.clock.now().replace(hour=11, minute=30))
    sched.run_pass()
    store.patch_item(who("agent-a"), it["id"], {"note": "still going"})
    assert [x for x in dl.fetch(HOST) if x["kind"] == "running_check"] == []


def test_lapse_goes_to_waiting_when_something_is_open(store, sched, dl, mk, who):
    dep = mk("agent-b", "Dependency")
    it = mk("agent-a", "Build", state="running", waits_on=[{"item": dep["id"]}])
    store.clock.set(store.clock.now().replace(hour=11, minute=30))
    sched.run_pass()
    (d,) = [x for x in dl.fetch(HOST) if x["kind"] == "running_check"]
    dl.ack(HOST, d["id"], {"result": "delivered"})
    store.clock.advance(hours=1)
    sched.run_pass()
    assert store.get_item(it["id"])["state"] == "waiting"


def test_an_outage_pauses_the_grace(store, sched, dl, mk):
    store.settings.outage_seconds = 60
    it = mk("agent-a", "Build", state="running")
    store.clock.set(store.clock.now().replace(hour=11, minute=30))
    sched.run_pass()
    (d,) = [x for x in dl.fetch(HOST) if x["kind"] == "running_check"]
    dl.ack(HOST, d["id"], {"result": "delivered"})  # 11:30: lapse due 12:30
    store.clock.advance(minutes=10)
    sched.run_pass()  # 11:40, the last pass before Waypoint is down
    store.clock.advance(minutes=40)  # down until 12:20
    sched.run_pass()
    store.settings.outage_seconds = 10**9  # from here the test jumps the clock; those jumps aren't outages
    store.clock.set(store.clock.now().replace(hour=12, minute=59))
    sched.run_pass()
    assert store.get_item(it["id"])["state"] == "running"  # 12:30 + 40 minutes = 13:10
    store.clock.set(store.clock.now().replace(hour=13, minute=10))
    sched.run_pass()
    assert store.get_item(it["id"])["state"] == "next"


def test_items_running_before_the_upgrade_get_a_lease(store, sched, mk):
    it = mk("agent-a", "Build", state="running")
    store.conn.execute("UPDATE items SET running_until = NULL")  # as migration 5 leaves them
    store.clock.advance(hours=5)
    sched.run_pass()
    assert store.get_item(it["id"])["running_until"] == "2026-03-10T16:30:00Z"
    assert checks(store) == []  # nothing lapses or is checked at once


def test_config(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[site]\nrunning_lease = "90m"\nrunning_grace = "2h"\n')
    s = config.load(p).settings
    assert (s.running_lease_seconds, s.running_grace_seconds) == (5400, 7200)
    p.write_text('[site]\nrunning_lease = "soon"\n')
    with pytest.raises(config.ConfigError):
        config.load(p)


# ---------------------------------------------------------------------- clients


def test_mcp_and_wp(server, store, tokens, tracks, tmp_path):
    a = build(WaypointClient(server.base, tokens["agent-a"], timeout=10), "agent-a")

    async def call(tool, **args):
        async with Client(a) as c:
            r = await c.call_tool(tool, args)
            return "\n".join(getattr(b, "text", "") for b in r.content)

    text = asyncio.run(call("wp_add", title="Build", track="release", running_until="2026-03-10T15:00:00Z"))
    assert "[running]" in text and "running until" in text
    asyncio.run(call("wp_update", id="W-1", running_until="2026-03-10T18:00:00Z"))
    assert store.get_item("W-1")["running_until"] == "2026-03-10T18:00:00Z"
    tf = tmp_path / "t"
    tf.write_text(tokens["agent-a"])
    out = io.StringIO()
    assert wp.main(["--url", server.base, "--token-file", str(tf), "--json", "add", "Soak", "--track", "release",
                    "--running-until", "2026-03-10T20:00:00Z"], out, io.StringIO()) == 0
    got = json.loads(out.getvalue())
    assert got["state"] == "running" and got["running_until"] == "2026-03-10T20:00:00Z"
    store.conn.execute("UPDATE items SET running_check = 0 WHERE num = 2")
    out = io.StringIO()
    wp.main(["--url", server.base, "--token-file", str(tf), "show", "W-2"], out, io.StringIO())
    assert "[running RUNNING?]" in out.getvalue()
