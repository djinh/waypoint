"""The scheduler turns recurrences into ordinary items: at the right time, by the overlap rule, and
caught up (or not) after Waypoint's own downtime."""

from __future__ import annotations

import pytest

from waypoint.recurrences import Recurrences
from waypoint.scheduler import Scheduler


@pytest.fixture
def sched(store, tokens, tracks):
    store.settings.outage_seconds = 10**9
    store.route("agent-a", "host-1")
    return Scheduler(store)


@pytest.fixture
def rec(store, sched):
    return Recurrences.of(store)


def make(rec, who, **kw):
    return rec.create(who("agent-a"), {"track": "release", "title": "Morning review", "label": "review",
                                       "schedule": {"daily": "09:00"}, **kw})


def items(store):
    return [store.get_item(f"W-{r[0]}") for r in store.conn.execute("SELECT num FROM items ORDER BY num")]


def to(store, iso):
    from datetime import datetime
    store.clock.set(datetime.fromisoformat(iso.replace("Z", "+00:00")))


def test_an_occurrence_is_an_ordinary_item_for_the_owner(store, sched, rec, who):
    make(rec, who, note="the open issues", window="2h")
    to(store, "2026-03-11T07:59:59Z")
    sched.run_pass()
    assert items(store) == []
    to(store, "2026-03-11T08:00:00Z")
    rep = sched.run_pass()
    assert rep.occurrences == 1
    [it] = items(store)
    assert it["recurrence"] == "R-1" and it["occurrence_at"] == "2026-03-11T08:00:00Z" and it["owner"] == "agent-a"
    assert it["title"] == "Morning review" and it["label"] == "review" and it["note"] == "the open issues"
    assert it["due_at"] == "2026-03-11T10:00:00Z" and it["state"] == "next"
    r = rec.get("R-1")
    assert r["last_at"] == "2026-03-11T08:00:00Z" and r["next_at"] == "2026-03-12T08:00:00Z"
    assert r["open_occurrence"] == it["id"]
    [m] = store.conn.execute("SELECT recipient, kind, text FROM messages WHERE kind = 'recurring'").fetchall()
    assert m["recipient"] == "agent-a" and "R-1" in m["text"]


def test_overlap_skip_keeps_one_item(store, sched, rec, who):
    make(rec, who)
    for day in ("11", "12"):
        to(store, f"2026-03-{day}T08:00:00Z")
        sched.run_pass()
    [it] = items(store)  # the second occurrence was skipped: the first is still open
    hist = store.get_item(it["id"], history=True)["history"]
    assert any("skipped: the previous one is still open" in (h["note"] or "") for h in hist)
    assert rec.get("R-1")["next_at"] == "2026-03-13T08:00:00Z"


def test_overlap_replace_and_allow(store, sched, rec, who):
    make(rec, who, overlap="replace")
    make(rec, who, overlap="allow", title="Allowed")
    for day in ("11", "12"):
        to(store, f"2026-03-{day}T08:00:00Z")
        sched.run_pass()
    got = sorted((it["title"], it["state"]) for it in items(store))
    assert got == [("Allowed", "next"), ("Allowed", "next"), ("Morning review", "dropped"), ("Morning review", "next")]
    allowed = [it for it in items(store) if it["title"] == "Allowed"]
    assert len(allowed) == 2
    dropped = [it for it in items(store) if it["state"] == "dropped"]
    assert dropped[0]["note"].startswith("superseded by the occurrence at")


def test_catch_up_latest_after_an_outage(store, sched, rec, who):
    make(rec, who)
    store.settings.outage_seconds = 60
    to(store, "2026-03-10T10:00:00Z")
    sched.run_pass()
    to(store, "2026-03-13T12:00:00Z")  # down for three days: 11, 12 and 13 March 09:00 missed
    sched.run_pass()
    [it] = items(store)
    assert it["occurrence_at"] == "2026-03-13T08:00:00Z" and it["note"].startswith("caught up: 3 occurrence(s)")
    assert it["late"]  # due 09:00Z, now 12:00Z
    assert rec.get("R-1")["next_at"] == "2026-03-14T08:00:00Z"


def test_catch_up_none_waits_for_the_next(store, sched, rec, who):
    make(rec, who, catch_up="none")
    store.settings.outage_seconds = 60
    to(store, "2026-03-10T10:00:00Z")
    sched.run_pass()
    to(store, "2026-03-13T12:00:00Z")
    sched.run_pass()
    assert items(store) == []
    h = rec.get("R-1", history=True)["history"]
    assert h[-1]["event"] == "skipped" and "not caught up" in h[-1]["note"]
    assert rec.get("R-1")["next_at"] == "2026-03-14T08:00:00Z"


def test_paused_and_stopped_create_nothing(store, sched, rec, who):
    make(rec, who)
    rec.patch(who("agent-a"), "R-1", {"state": "paused"})
    to(store, "2026-03-12T08:00:00Z")
    sched.run_pass()
    assert items(store) == []


def test_an_interval_creates_one_per_pass_and_skips_while_open(store, sched, rec, who):
    rec.create(who("agent-a"), {"track": "release", "title": "Poll", "schedule": {"every": "15m"}, "overlap": "allow"})
    for minute in ("09:45", "10:00", "10:15"):
        to(store, f"2026-03-10T{minute}:00Z")
        sched.run_pass()
    assert [it["occurrence_at"][11:16] for it in items(store)] == ["09:45", "10:00", "10:15"]


def test_occurrences_are_waypoints_own_writes_even_after_a_hand_over(store, sched, rec, who):
    make(rec, who, overlap="replace")
    to(store, "2026-03-11T08:00:00Z")
    sched.run_pass()
    rec.patch(who("agent-a"), "R-1", {"owner": "agent-b"})
    to(store, "2026-03-12T08:00:00Z")
    sched.run_pass()  # replacing agent-a's open W-1 is Waypoint's write, not agent-b's
    first, second = items(store)
    assert first["state"] == "dropped" and second["owner"] == "agent-b" and second["state"] == "next"
    h1 = store.get_item(first["id"], history=True)["history"]
    [drop] = [h for h in h1 if h["event"] == "changed" and "state" in (h["changes"] or {})]
    assert drop["by"] is None and drop["changes"]["state"] == ["next", "dropped"]
    assert store.get_item(second["id"], history=True)["history"][0]["by"] is None
    assert second["updated_by"] is None


def test_a_failing_recurrence_is_paused_and_the_pass_goes_on(store, sched, rec, who, mk):
    store.route("coordinator", "host-1")
    make(rec, who)
    make(rec, who, title="Other")
    mk("agent-a", "Remind me", wake_at="2026-03-11T08:00:00Z")
    with store.tx() as c:  # bad data in one recurrence
        c.execute("UPDATE recurrences SET schedule = '{\"daily\": \"9 o''clock\"}' WHERE num = 1")
    to(store, "2026-03-11T08:00:00Z")
    sched.run_pass()
    assert [it["title"] for it in items(store) if it["recurrence"]] == ["Other"]
    bad = rec.get("R-1", history=True)
    assert bad["state"] == "paused" and "creating its occurrence failed" in bad["history"][-1]["note"]
    kinds = {(r[0], r[1]) for r in store.conn.execute("SELECT recipient, kind FROM messages")}
    assert ("agent-a", "wake") in kinds and ("coordinator", "recurring") in kinds
    to(store, "2026-03-12T08:00:00Z")
    sched.run_pass()  # the next passes run normally
    assert len([it for it in items(store) if it["recurrence"]]) == 1  # "Other" skipped: still open


def test_a_caught_up_occurrence_is_late_because_waypoint_was_down(store, sched, rec, who):
    from waypoint.deliveries import Deliveries
    from waypoint.store import Client
    make(rec, who)
    store.settings.outage_seconds = 60
    to(store, "2026-03-10T10:00:00Z")
    sched.run_pass()
    to(store, "2026-03-13T12:00:00Z")
    sched.run_pass()
    [d] = [x for x in Deliveries.of(store).fetch(Client("host-1", "deliverer", "H"), 50) if x["kind"] == "recurring"]
    assert d["late"]["reason"] == "waypoint_down"


def test_a_caught_up_occurrence_climbs_its_ladder_from_its_creation(store, sched, rec, who):
    store.route("coordinator", "host-1")
    make(rec, who)  # the default ladder: nudge at 0m, escalate and flag 1 h later
    store.settings.outage_seconds = 60
    to(store, "2026-03-10T10:00:00Z")
    sched.run_pass()
    to(store, "2026-03-13T12:00:00Z")  # caught up; its due time (09:00Z) is already past
    sched.run_pass()
    store.clock.advance(seconds=15)
    sched.run_pass()

    def kinds():
        return [r[0] for r in store.conn.execute("SELECT kind FROM messages WHERE item = 1 AND kind IN "
                                                 "('nudge', 'escalate', 'at_risk') ORDER BY num")]
    assert kinds() == ["nudge"]  # only the first step, not the whole ladder at once
    store.clock.advance(minutes=59, seconds=45)
    sched.run_pass()
    assert kinds() == ["nudge", "escalate"]  # an hour after the occurrence was created
