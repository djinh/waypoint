"""The scheduler on a fake clock: wake-ups, transitions, the overdue ladder and catch-up after downtime."""

from __future__ import annotations

import json

import pytest

from waypoint.scheduler import Scheduler


@pytest.fixture
def sched(store, tokens, tracks) -> Scheduler:
    # Most tests jump the clock between passes for brevity; that's not an outage unless a test says so.
    store.settings.outage_seconds = 10**9
    return Scheduler(store)


@pytest.fixture
def outages(store):
    store.settings.outage_seconds = 60


def messages(store, since: int = 0) -> list[dict]:
    rows = store.conn.execute("SELECT * FROM messages WHERE num > ? ORDER BY num", (since,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["late"] = json.loads(d["late"]) if d["late"] else None
        d["items"] = json.loads(d["items"])
        out.append(d)
    return out


def kinds(store, since: int = 0) -> list[tuple]:
    return [(m["kind"], m["recipient"], m["item"]) for m in messages(store, since)]


def last_num(store) -> int:
    return store.conn.execute("SELECT COALESCE(MAX(num), 0) FROM messages").fetchone()[0]


# ---------------------------------------------------------------------- wake-ups


def test_wake_up_once_at_its_time(store, sched, mk):
    it = mk("agent-a", "Check the soak result", label="soak result", note="soak ends 16:00",
            wake_at="2026-03-10T10:30:00Z")
    sched.run_pass()
    assert messages(store) == []
    store.clock.advance(minutes=59, seconds=59)
    sched.run_pass()
    assert messages(store) == []
    store.clock.advance(seconds=1)
    sched.run_pass()
    (m,) = messages(store)
    assert (m["kind"], m["recipient"], m["item"]) == ("wake", "agent-a", 1)
    assert m["text"] == "Reminder: W-1 (soak result): soak ends 16:00"
    assert m["summary"] == "Check the soak result (agent-a)"
    assert m["fell_due_at"] == "2026-03-10T10:30:00Z" and m["late"] is None
    store.clock.advance(seconds=15)
    sched.run_pass()
    assert len(messages(store)) == 1
    hist = [h["event"] for h in store.get_item(it["id"], history=True)["history"]]
    assert hist[-1] == "message"


def test_a_done_items_wake_up_is_cancelled(store, sched, mk, who):
    it = mk("agent-a", "Remind me", wake_at="2026-03-10T10:00:00Z")
    store.patch_item(who("agent-a"), it["id"], {"state": "done"})
    store.clock.advance(hours=1)
    sched.run_pass()
    assert messages(store) == []


def test_a_new_wake_time_reminds_again(store, sched, mk, who):
    it = mk("agent-a", "Remind me", wake_at="2026-03-10T10:00:00Z")
    store.clock.advance(hours=1)
    sched.run_pass()
    store.patch_item(who("agent-a"), it["id"], {"wake_at": "2026-03-10T11:00:00Z"})
    sched.run_pass()
    assert len(messages(store)) == 1
    store.clock.advance(hours=1)
    sched.run_pass()
    assert [m["kind"] for m in messages(store)] == ["wake", "wake"]


# ---------------------------------------------------------------------- transitions


def test_done_wakes_exactly_the_sessions_it_unblocks(store, sched, mk, who):
    soak = mk("agent-a", "Staging soak", label="gate soak")
    mk("agent-b", "Production release", label="production release", waits_on=[{"item": soak["id"]}])
    other = mk("agent-c", "Unrelated")
    store.patch_item(who("agent-a"), soak["id"], {"state": "done"})
    (m,) = messages(store)
    assert (m["kind"], m["recipient"], m["item"], m["items"]) == ("unblocked", "agent-b", 2, ["W-1"])
    assert m["text"] == "W-1 (gate soak) is done; W-2 (production release) can start"
    assert other["id"] not in m["text"]


def test_answer_to_a_decision_wakes_the_asker(store, sched, mk, who):
    q = mk("alice", "Choose the strategy", caller="agent-b", kind="decision", question={"text": "Which?"})
    mk("agent-b", "Migration tool", track="infra", waits_on=[{"item": q["id"]}])
    store.patch_item(who("alice"), q["id"], {"state": "done", "note": "online"})
    assert kinds(store) == [("unblocked", "agent-b", 2)]


def test_time_condition_unblocks_on_a_pass(store, sched, mk):
    mk("agent-a", "Rotate credentials", waits_on=[{"time": "2026-03-10T11:00:00Z"}])
    store.clock.advance(hours=2)
    sched.run_pass()
    (m,) = messages(store)
    assert m["kind"] == "unblocked" and m["text"] == "12:00 has passed; W-1 (Rotate credentials) can start"


def test_dropped_asks_dependents_to_replan(store, sched, mk, who):
    hot = mk("agent-a", "Hotfix 2.3.5")
    mk("agent-b", "Deploy hotfix", waits_on=[{"item": hot["id"]}])
    store.patch_item(who("agent-a"), hot["id"], {"state": "dropped", "note": "folded into 2.4"})
    (m,) = messages(store)
    assert (m["kind"], m["recipient"]) == ("replan", "agent-b")
    assert m["text"] == ("W-1 (Hotfix 2.3.5) was dropped: folded into 2.4. W-2 (Deploy hotfix) waits on it: "
                         "please re-plan.")


def test_reopening_asks_dependents_to_replan(store, sched, mk, who):
    a = mk("agent-a", "A")
    mk("agent-b", "B", waits_on=[{"item": a["id"]}])
    store.patch_item(who("agent-a"), a["id"], {"state": "done"})
    store.patch_item(who("agent-a"), a["id"], {"state": "next"})
    assert [k[0] for k in kinds(store)] == ["unblocked", "replan"]


# ---------------------------------------------------------------------- the ladder


def test_default_ladder(store, sched, mk):
    notes = mk("agent-a", "Release notes", due_at="2026-03-10T10:00:00Z")
    mk("agent-c", "Announce", waits_on=[{"item": notes["id"]}])
    store.clock.advance(minutes=29, seconds=59)
    sched.run_pass()
    assert messages(store) == []
    store.clock.advance(seconds=1)  # exactly the due time: step 1, "0m after"
    sched.run_pass()
    assert kinds(store) == [("nudge", "agent-a", 1)]
    assert messages(store)[0]["text"] == "W-1 (Release notes) is late: due 11:00."
    n = last_num(store)
    store.clock.advance(minutes=59, seconds=45)
    sched.run_pass()
    assert messages(store, n) == []
    store.clock.advance(seconds=15)
    sched.run_pass()
    assert kinds(store, n) == [("escalate", "coordinator", 1), ("at_risk", "agent-c", 2)]
    esc, risk = messages(store, n)
    assert esc["text"] == "W-1 (Release notes), owned by agent-a, is late: due 11:00. Please chase or re-plan."
    assert risk["items"] == ["W-1"] and "W-2 (Announce), which waits on it, is at risk" in risk["text"]
    n = last_num(store)
    store.clock.advance(days=2)
    sched.run_pass()
    assert messages(store, n) == []
    steps = store.get_item(notes["id"])["overdue_steps"]
    assert [(s["action"], s["caught_up"]) for s in steps] == [("nudge", False), ("escalate", False), ("flag", False)]
    hist = store.get_item(notes["id"], history=True)["history"]
    assert [h["note"] for h in hist if h["event"] == "ladder_step"] == [
        "nudge (step 1, 0m after due)", "escalate (step 2, 1h after due)", "flag (step 3, 1h after due)"]


def test_owner_update_stops_the_ladder(store, sched, mk, who):
    it = mk("agent-a", "Notes", due_at="2026-03-10T10:00:00Z")
    store.clock.advance(minutes=31)
    sched.run_pass()
    assert kinds(store) == [("nudge", "agent-a", 1)]
    store.patch_item(who("agent-a"), it["id"], {"note": "on it, 20 more minutes"})
    store.clock.advance(hours=2)
    sched.run_pass()
    assert kinds(store) == [("nudge", "agent-a", 1)]
    assert store.get_item(it["id"])["late"]  # still late, just attended


def test_update_before_the_due_time_does_not_stop_it(store, sched, mk, who):
    it = mk("agent-a", "Notes", due_at="2026-03-10T10:00:00Z")
    store.clock.advance(minutes=10)
    store.patch_item(who("agent-a"), it["id"], {"note": "progress"})
    store.clock.advance(minutes=21)
    sched.run_pass()
    assert kinds(store) == [("nudge", "agent-a", 1)]


def test_a_new_due_time_rearms_the_ladder(store, sched, mk, who):
    it = mk("agent-a", "Notes", due_at="2026-03-10T10:00:00Z")
    store.clock.advance(minutes=31)
    sched.run_pass()
    store.patch_item(who("agent-a"), it["id"], {"due_at": "2026-03-10T11:00:00Z"})
    assert store.get_item(it["id"])["overdue_steps"] == []
    store.clock.advance(minutes=30)
    sched.run_pass()
    assert [k[0] for k in kinds(store)] == ["nudge"]  # 10:31: the new due time hasn't come
    store.clock.advance(minutes=30)
    sched.run_pass()
    assert [k[0] for k in kinds(store)] == ["nudge", "nudge"]


def test_done_stops_the_ladder(store, sched, mk, who):
    it = mk("agent-a", "Notes", due_at="2026-03-10T10:00:00Z")
    store.patch_item(who("agent-a"), it["id"], {"state": "done"})
    store.clock.advance(hours=3)
    sched.run_pass()
    assert messages(store) == []


def test_own_ladder_with_notify(store, sched, mk):
    mk("agent-b", "Production sudo cut", due_at="2026-03-10T10:00:00Z",
       overdue=[{"after": "0m", "action": "nudge"}, {"after": "14h", "action": "notify", "to": "alice"}])
    store.clock.advance(minutes=30)
    sched.run_pass()
    store.clock.advance(hours=14)
    sched.run_pass()
    assert kinds(store) == [("nudge", "agent-b", 1), ("notify", "alice", 1)]
    note = messages(store)[1]
    assert note["text"] == "Production sudo cut (agent-b) is late"
    assert note["summary"] == "Production sudo cut (agent-b)"


def test_steps_run_once_across_restarts(store, mk, tokens, tracks):
    mk("agent-a", "Notes", due_at="2026-03-10T10:00:00Z")
    store.clock.advance(minutes=31)
    Scheduler(store).run_pass()
    store.clock.advance(seconds=15)
    Scheduler(store).run_pass()
    assert kinds(store) == [("nudge", "agent-a", 1)]


def test_created_past_due_starts_with_a_nudge(store, sched, mk):
    """The ladder clock starts when the due time was set, if that is later."""
    it = mk("agent-a", "Should have been done", due_at="2026-03-10T06:00:00Z")
    mk("agent-c", "Waits", waits_on=[{"item": it["id"]}])
    assert store.get_item(it["id"])["late"]  # late at once: derived from due_at alone
    store.clock.advance(seconds=15)
    sched.run_pass()
    assert kinds(store) == [("nudge", "agent-a", 1)]
    n = last_num(store)
    store.clock.advance(minutes=59, seconds=30)
    sched.run_pass()
    assert messages(store, n) == []
    store.clock.advance(seconds=30)
    sched.run_pass()
    assert kinds(store, n) == [("escalate", "coordinator", 1), ("at_risk", "agent-c", 2)]
    steps = store.get_item(it["id"])["overdue_steps"]
    assert [(s["action"], s["caught_up"]) for s in steps] == [("nudge", False), ("escalate", False), ("flag", False)]


def test_redated_into_the_past_starts_with_a_nudge(store, sched, mk, who):
    it = mk("agent-a", "Notes", due_at="2026-03-11T10:00:00Z")
    store.clock.advance(minutes=5)
    store.patch_item(who("agent-a"), it["id"], {"due_at": "2026-03-10T08:00:00Z"})
    store.clock.advance(seconds=15)
    sched.run_pass()
    assert kinds(store) == [("nudge", "agent-a", 1)]
    store.clock.advance(hours=1)
    sched.run_pass()
    assert [k[0] for k in kinds(store)] == ["nudge", "escalate"]


def test_one_outbox_per_store(store, mk, tokens, tracks, who):
    Scheduler(store)
    Scheduler(store)
    a = mk("agent-a", "A")
    mk("agent-b", "B", waits_on=[{"item": a["id"]}])
    store.patch_item(who("agent-a"), a["id"], {"state": "done"})
    assert kinds(store) == [("unblocked", "agent-b", 2)]


def test_a_failing_pass_logs_its_type_only(store, sched, caplog):
    import asyncio

    def broken():
        raise ValueError("item text: hunter2")

    sched.run_pass = broken
    store.settings.pass_seconds = 0

    async def run():
        task = asyncio.create_task(sched.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(run())
    assert "scheduler pass failed: ValueError" in caplog.text
    assert "hunter2" not in caplog.text and "Traceback" not in caplog.text


def test_no_coordinator_no_escalation_message(store, mk, tokens, tracks):
    store.retire_client("coordinator")
    sched = Scheduler(store)
    mk("agent-a", "Notes", due_at="2026-03-10T09:00:00Z", overdue=[{"after": "0m", "action": "escalate"}])
    sched.run_pass()
    assert messages(store) == []
    assert [s["action"] for s in store.get_item("W-1")["overdue_steps"]] == ["escalate"]


# ---------------------------------------------------------------------- catch-up after downtime


def test_outage_catch_up(store, sched, mk, who, outages):
    """Waypoint is down 09:40–12:40 UTC. Everything that fell due meanwhile is caught up once."""
    late_item = mk("agent-a", "Release notes", due_at="2026-03-10T10:00:00Z")
    mk("agent-c", "Announce", waits_on=[{"item": late_item["id"]}])
    mk("agent-b", "Check the soak", wake_at="2026-03-10T11:00:00Z")
    changed = mk("agent-b", "Look at metrics", wake_at="2026-03-10T11:30:00Z")
    done = mk("agent-b", "Done meanwhile", wake_at="2026-03-10T11:45:00Z")
    store.clock.advance(minutes=10)
    sched.run_pass()  # 09:40, the last pass before the outage
    assert messages(store) == []
    store.clock.set(store.clock.now().replace(hour=12, minute=0))
    # the scheduler is down, but the API still takes writes
    store.patch_item(who("agent-b"), changed["id"], {"note": "looked already"})
    store.patch_item(who("agent-b"), done["id"], {"state": "done"})
    store.clock.set(store.clock.now().replace(hour=12, minute=40))
    report = sched.run_pass()
    assert report.outage == ("2026-03-10T09:40:00Z", "2026-03-10T12:40:00Z")
    assert (report.wakes_sent, report.wakes_late, report.wakes_dropped, report.steps_caught_up) == (1, 1, 1, 2)
    got = messages(store)
    assert [(m["kind"], m["recipient"], m["item"]) for m in got] == [
        ("wake", "agent-b", 3),
        ("escalate", "coordinator", 1),
        ("at_risk", "agent-c", 2),
        ("outage", "coordinator", None),
    ]
    wake, esc, risk, summary = got
    down = {"reason": "waypoint_down", "down_from": "2026-03-10T09:40:00Z", "down_until": "2026-03-10T12:40:00Z"}
    assert wake["late"] == {"due_at": "2026-03-10T11:00:00Z", **down}
    assert wake["text"] == "Reminder: W-3 (Check the soak) (late: due 12:00, Waypoint was down 10:40–13:40)"
    assert esc["late"] == {"due_at": "2026-03-10T11:00:00Z", **down}
    assert esc["text"].endswith("(late: due 12:00, Waypoint was down 10:40–13:40)")
    assert summary["text"] == ("Waypoint was down 10:40–13:40 (3 h 0 min). Caught up: 1 wake-up(s) sent late, "
                               "1 dropped because the item changed, 2 overdue step(s) taken late.")
    assert summary["summary"] == "Waypoint was down (3 h 0 min)"
    # the nudge was skipped, not delivered later
    steps = store.get_item(late_item["id"])["overdue_steps"]
    assert [(s["action"], s["caught_up"]) for s in steps] == [("escalate", True), ("flag", True)]
    n = last_num(store)
    store.clock.advance(seconds=15)
    sched.run_pass()
    assert messages(store, n) == []  # one summary, no repeats


def test_outage_threshold(store, sched, outages):
    sched.run_pass()
    store.clock.advance(seconds=60)
    assert sched.run_pass().outage is None
    store.clock.advance(seconds=61)
    assert sched.run_pass().outage is not None
    assert [m["kind"] for m in messages(store)] == ["outage"]


def test_time_condition_met_during_outage_is_late(store, sched, mk, outages):
    mk("agent-a", "Rotate credentials", waits_on=[{"time": "2026-03-10T10:00:00Z"}])
    sched.run_pass()
    store.clock.advance(hours=2)
    sched.run_pass()
    unblocked = [m for m in messages(store) if m["kind"] == "unblocked"][0]
    assert unblocked["late"]["reason"] == "waypoint_down" and unblocked["fell_due_at"] == "2026-03-10T10:00:00Z"
    assert "(late: due 11:00, Waypoint was down 10:30–12:30)" in unblocked["text"]


def test_listeners_hear_about_new_messages(store, sched, mk):
    heard = []
    sched.listeners.append(lambda: heard.append(1))
    sched.run_pass()
    assert heard == []
    mk("agent-a", "Remind me", wake_at="2026-03-10T09:30:00Z")
    sched.run_pass()
    assert heard == [1]


def test_the_app_runs_a_pass_before_serving(store, tokens, tracks):
    """The lifespan runs the first pass before the app takes requests, then keeps running passes."""
    import asyncio

    from waypoint.api import create_app

    store.settings.pass_seconds = 1
    app = create_app(store, scheduler=Scheduler(store))

    def passes():
        return store.conn.execute("SELECT COUNT(*) FROM passes").fetchone()[0]

    async def run():
        async with app.router.lifespan_context(app):
            first = passes()
            for _ in range(60):
                if passes() > first:
                    break
                await asyncio.sleep(0.05)
            return first, passes()

    first, later = asyncio.run(run())
    assert first == 1 and later == 2


def test_ladder_across_daylight_saving(store, sched, mk):
    """Delays are absolute durations; times in messages are local, on either side of the change.
    Paris goes from UTC+1 to UTC+2 at 01:00 UTC on 29 March 2026."""
    store.clock.set(store.clock.now().replace(month=3, day=28, hour=23, minute=0))
    mk("agent-a", "Overnight job", due_at="2026-03-29T00:30:00Z",
       overdue=[{"after": "0m", "action": "nudge"}, {"after": "2h", "action": "escalate"}])
    store.clock.set(store.clock.now().replace(day=29, hour=0, minute=30))
    sched.run_pass()
    store.clock.set(store.clock.now().replace(hour=2, minute=29, second=59))
    sched.run_pass()
    assert [k[0] for k in kinds(store)] == ["nudge"]
    store.clock.advance(seconds=1)
    sched.run_pass()
    nudge, esc = messages(store)
    assert nudge["text"] == "W-1 (Overnight job) is late: due 01:30."
    assert esc["fell_due_at"] == "2026-03-29T02:30:00Z"
    assert "due 01:30" in esc["text"]


@pytest.mark.parametrize("by", ["coordinator", "alice"])
def test_others_notes_dont_stop_the_ladder(store, sched, mk, who, by):
    """Only the owner attends to an item; a coordinator's or a human's note leaves the ladder running."""
    it = mk("agent-a", "Notes", due_at="2026-03-10T10:00:00Z")
    mk("agent-c", "Waits", waits_on=[{"item": it["id"]}])
    store.clock.advance(minutes=31)
    sched.run_pass()
    store.clock.advance(minutes=15)
    store.patch_item(who(by), it["id"], {"note": "chasing agent-a"})
    store.clock.advance(minutes=45)
    sched.run_pass()
    assert [k[0] for k in kinds(store)] == ["nudge", "escalate", "at_risk"]


def test_a_hand_over_by_the_owner_attends(store, sched, mk, who):
    it = mk("agent-a", "Notes", due_at="2026-03-10T10:00:00Z")
    store.clock.advance(minutes=31)
    sched.run_pass()
    store.patch_item(who("agent-a"), it["id"], {"owner": "agent-b"})
    store.clock.advance(hours=1)
    sched.run_pass()
    assert [k[0] for k in kinds(store)] == ["nudge"]


def test_a_wake_missed_in_an_outage_survives_a_coordinators_note(store, sched, mk, who, outages):
    it = mk("agent-b", "Check the soak", wake_at="2026-03-10T11:00:00Z")
    sched.run_pass()
    store.clock.set(store.clock.now().replace(hour=12))
    store.patch_item(who("coordinator"), it["id"], {"note": "still soaking"})
    store.clock.set(store.clock.now().replace(hour=12, minute=30))
    report = sched.run_pass()
    assert (report.wakes_sent, report.wakes_dropped) == (1, 0)


def test_no_notice_to_the_owner_about_its_own_change(store, sched, mk, who):
    """The client whose write caused an unblock or a re-plan knows already."""
    a = mk("agent-a", "A")
    mine = mk("agent-a", "Mine")
    store.patch_item(who("agent-a"), mine["id"], {"add_waits_on": [{"item": a["id"]}]})  # next → waiting
    store.patch_item(who("agent-a"), a["id"], {"state": "done"})                         # unblocks my own
    assert messages(store) == []
    theirs = mk("agent-b", "Theirs")
    store.patch_item(who("agent-b"), theirs["id"], {"add_waits_on": [{"item": mine["id"]}]})
    assert messages(store) == []


def test_a_notice_when_someone_else_causes_it(store, sched, mk, who):
    a = mk("agent-a", "A")
    b = mk("agent-b", "B")
    store.patch_item(who("coordinator"), b["id"], {"add_waits_on": [{"item": a["id"]}]})  # re-plan for agent-b
    store.patch_item(who("agent-a"), a["id"], {"state": "done"})                          # unblocked for agent-b
    assert [(m["kind"], m["recipient"]) for m in messages(store)] == [("replan", "agent-b"), ("unblocked", "agent-b")]
