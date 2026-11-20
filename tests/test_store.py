"""Items, conditions, derived states and flags, loops, history: the store directly."""

from __future__ import annotations

import pytest

from waypoint.errors import ApiError


def test_create_defaults(store, mk):
    it = mk("agent-a", "Release notes for 2.4", label="release notes")
    assert it["id"] == "W-1"
    assert it["state"] == "next"
    assert it["owner"] == "agent-a" and it["created_by"] == "agent-a"
    assert it["waits_on"] == [] and it["waited_on_by"] == []
    assert it["late"] is False and it["at_risk"] is False and it["at_risk_from"] == []
    assert it["version"] == 1
    assert it["created_at"] == "2026-03-10T09:30:00Z"


def test_ids_are_never_reused(store, mk):
    ids = [mk("agent-a", f"Item {n}")["id"] for n in range(3)]
    assert ids == ["W-1", "W-2", "W-3"]
    store.conn.execute("PRAGMA foreign_keys = OFF")
    store.conn.execute("DELETE FROM history WHERE item = 3")
    store.conn.execute("DELETE FROM items WHERE num = 3")
    assert mk("agent-a", "Item after a delete")["id"] == "W-4"


def test_waiting_until_done_then_unblocked(store, mk, who):
    a = mk("agent-a", "Staging soak", kind="event")
    b = mk("agent-b", "Production release", waits_on=[{"item": a["id"]}])
    assert b["state"] == "waiting"
    assert b["waits_on"] == [{"item": a["id"], "met": False, "met_at": None}]
    assert store.get_item(a["id"])["waited_on_by"] == [b["id"]]
    store.clock.advance(hours=1)
    store.patch_item(who("agent-a"), a["id"], {"state": "done", "note": "soak clean"})
    b = store.get_item(b["id"], history=True)
    assert b["state"] == "next"
    assert b["state_since"] == "2026-03-10T10:30:00Z"
    assert b["waits_on"][0] == {"item": a["id"], "met": True, "met_at": "2026-03-10T10:30:00Z"}
    assert b["updated_by"] is None  # Waypoint's own change
    events = [h["event"] for h in b["history"]]
    assert events == ["created", "condition_met", "derived"]
    assert b["history"][-1]["changes"] == {"state": ["waiting", "next"]}


@pytest.fixture
def heard(store):
    """Transitions the store reports to its listeners, as (kind, item, owner, because)."""
    got = []
    store.listeners.append(lambda conn, trans: got.extend((t.kind, t.item, t.owner, t.because) for t in trans))
    return got


def test_done_reports_unblocked(store, mk, who, heard):
    a = mk("agent-a", "Soak")
    b = mk("agent-b", "Release", waits_on=[{"item": a["id"]}])
    c = mk("agent-c", "Other", waits_on=[{"item": a["id"]}, {"time": "2026-03-11T00:00:00Z"}])
    heard.clear()
    store.patch_item(who("agent-a"), a["id"], {"state": "done"})
    assert heard == [("unblocked", b["id"], "agent-b", [a["id"]])]
    assert store.get_item(c["id"])["state"] == "waiting"


def test_listener_failure_rolls_back_the_write(store, mk, who):
    a = mk("agent-a", "A")

    def boom(conn, trans):
        raise RuntimeError("queue full")

    store.listeners.append(boom)
    with pytest.raises(RuntimeError):
        store.patch_item(who("agent-a"), a["id"], {"state": "done"})
    store.listeners.clear()
    assert store.get_item(a["id"])["state"] == "next"


def test_running_stays_running(store, mk, who):
    a = mk("agent-a", "Dependency")
    b = mk("agent-b", "Work", waits_on=[{"item": a["id"]}], state="running")
    assert b["state"] == "running"
    store.patch_item(who("agent-a"), a["id"], {"state": "done"})
    assert store.get_item(b["id"])["state"] == "running"
    store.patch_item(who("agent-b"), b["id"], {"state": "next"})
    assert store.get_item(b["id"])["state"] == "next"


def test_not_running_any_more_derives(store, mk, who):
    a = mk("agent-a", "Dependency")
    b = mk("agent-b", "Work", waits_on=[{"item": a["id"]}], state="running")
    out = store.patch_item(who("agent-b"), b["id"], {"state": "next"})
    assert out["state"] == "waiting"  # 'next' means "not running"; the conditions decide


def test_time_condition_met_on_tick(store, mk, heard):
    it = mk("agent-a", "Rotate staging credentials", waits_on=[{"time": "2026-03-10T12:00:00+01:00"}])
    assert it["waits_on"] == [{"time": "2026-03-10T11:00:00Z", "met": False, "met_at": None}]
    assert it["state"] == "waiting"
    store.clock.advance(hours=1, minutes=29, seconds=59)
    assert store.tick() == []
    store.clock.advance(seconds=1)
    store.tick()
    assert heard == [("unblocked", it["id"], "agent-a", ["2026-03-10T11:00:00Z"])]
    it = store.get_item(it["id"])
    assert it["state"] == "next" and it["waits_on"][0]["met_at"] == "2026-03-10T11:00:00Z"


def test_time_condition_in_the_past_is_met_at_once(store, mk):
    it = mk("agent-a", "Already due", waits_on=[{"time": "2026-03-01T00:00:00Z"}])
    assert it["state"] == "next" and it["waits_on"][0]["met"]


def test_late_and_at_risk(store, mk, who, heard):
    notes = mk("agent-a", "Release notes", due_at="2026-03-10T10:00:00Z")
    release = mk("agent-a", "Production release", waits_on=[{"item": notes["id"]}])
    announce = mk("agent-c", "Announce", waits_on=[{"item": release["id"]}])
    assert not store.get_item(notes["id"])["late"]
    store.clock.advance(minutes=30)
    heard.clear()
    store.tick()
    assert heard == []  # exactly at the due time: not late yet
    store.clock.advance(seconds=1)
    store.tick()
    kinds = sorted((h[0], h[1]) for h in heard)
    assert kinds == [("at_risk", release["id"]), ("at_risk", announce["id"]), ("late", notes["id"])]
    for iid in (release["id"], announce["id"]):
        it = store.get_item(iid)
        assert it["at_risk"] and it["at_risk_from"] == [
            {"item": notes["id"], "why": "late", "due_at": "2026-03-10T10:00:00Z"}]
    # a new due date clears it
    store.patch_item(who("agent-a"), notes["id"], {"due_at": "2026-03-11T10:00:00Z"})
    assert not store.get_item(notes["id"])["late"]
    assert not store.get_item(announce["id"])["at_risk"]


def test_done_item_is_never_late(store, mk, who):
    it = mk("agent-a", "Quick one", due_at="2026-03-10T09:00:00Z")
    assert it["late"]
    assert not store.patch_item(who("agent-a"), it["id"], {"state": "done"})["late"]


def test_dropped_puts_dependents_at_risk_and_asks_to_replan(store, mk, who):
    hotfix = mk("agent-a", "Hotfix 2.3.5")
    dep = mk("agent-b", "Deploy the hotfix", waits_on=[{"item": hotfix["id"]}])
    with pytest.raises(ApiError) as e:
        store.patch_item(who("agent-a"), hotfix["id"], {"state": "dropped"})
    assert e.value.code == "invalid" and e.value.field == "note"
    out = store.patch_item(who("agent-a"), hotfix["id"], {"state": "dropped", "note": "folded into 2.4"})
    assert out["state"] == "dropped" and out["done_at"] == "2026-03-10T09:30:00Z"
    dep = store.get_item(dep["id"])
    assert dep["state"] == "waiting"
    assert dep["at_risk_from"] == [{"item": hotfix["id"], "why": "dropped", "due_at": None}]


def test_dropped_reports_replan(store, mk, who, heard):
    a = mk("agent-a", "A")
    b = mk("agent-b", "B", waits_on=[{"item": a["id"]}])
    heard.clear()
    store.patch_item(who("agent-a"), a["id"], {"state": "dropped", "note": "not needed"})
    assert heard == [("replan", b["id"], "agent-b", [a["id"]])]


def test_reopening_a_done_item_reopens_its_dependents(store, mk, who, heard):
    a = mk("agent-a", "A")
    b = mk("agent-b", "B", waits_on=[{"item": a["id"]}])
    c = mk("agent-c", "C", waits_on=[{"item": a["id"]}])
    store.patch_item(who("agent-a"), a["id"], {"state": "done"})
    store.patch_item(who("agent-c"), c["id"], {"state": "running"})
    assert store.get_item(b["id"])["state"] == "next"
    heard.clear()
    a2 = store.patch_item(who("agent-a"), a["id"], {"state": "next"})
    assert heard == [("replan", b["id"], "agent-b", [])]
    assert store.get_item(b["id"])["state"] == "waiting"
    assert store.get_item(c["id"])["state"] == "running"
    assert a2["state"] == "next" and a2["done_at"] is None


def test_reopen_via_patch(store, mk, who):
    a = mk("agent-a", "A")
    store.patch_item(who("agent-a"), a["id"], {"state": "done"})
    out = store.patch_item(who("agent-a"), a["id"], {"state": "waiting"})
    assert out["state"] == "next" and out["done_at"] is None


@pytest.mark.parametrize("length", [1, 2, 3, 5])
def test_loops_are_refused(store, mk, who, length):
    items = [mk("agent-a", f"Step {n}") for n in range(length)]
    for prev, cur in zip(items, items[1:]):
        store.patch_item(who("agent-a"), cur["id"], {"add_waits_on": [{"item": prev["id"]}]})
    first, last = items[0], items[-1]
    with pytest.raises(ApiError) as e:
        store.patch_item(who("agent-a"), first["id"], {"waits_on": [{"item": last["id"]}]})
    assert e.value.code == "loop"
    cyc = e.value.cycle
    assert cyc[0] == cyc[-1] == first["id"] and len(cyc) == length + 1
    # nothing was written
    assert store.get_item(first["id"])["waits_on"] == []
    assert store.get_item(first["id"])["version"] == 1


def test_loop_across_owners_refused_on_add(store, mk, who):
    a = mk("agent-a", "A")
    b = mk("agent-b", "B", waits_on=[{"item": a["id"]}])
    with pytest.raises(ApiError) as e:
        store.patch_item(who("agent-a"), a["id"], {"add_waits_on": [{"item": b["id"]}]})
    assert e.value.code == "loop" and e.value.cycle == [a["id"], b["id"], a["id"]]


def test_unknown_condition_item(store, mk):
    with pytest.raises(ApiError) as e:
        mk("agent-a", "Waits on nothing real", waits_on=[{"item": "W-999"}])
    assert e.value.code == "invalid" and e.value.field == "waits_on.0"
    assert store.items() == []  # the transaction was rolled back


def test_duplicate_conditions_collapse(store, mk, who):
    a = mk("agent-a", "A")
    b = mk("agent-b", "B", waits_on=[{"item": a["id"]}, {"item": a["id"]}])
    assert len(b["waits_on"]) == 1
    b = store.patch_item(who("agent-b"), b["id"], {"add_waits_on": [{"item": a["id"]}]})
    assert len(b["waits_on"]) == 1


def test_version_conflict(store, mk, who):
    it = mk("agent-a", "A")
    store.patch_item(who("agent-a"), it["id"], {"note": "first", "if_version": 1})
    with pytest.raises(ApiError) as e:
        store.patch_item(who("agent-a"), it["id"], {"note": "second", "if_version": 1})
    assert e.value.code == "version_conflict"
    assert store.get_item(it["id"])["note"] == "first"


def test_history_records_changes(store, mk, who):
    it = mk("agent-a", "A", note="started")
    store.clock.advance(minutes=5)
    store.patch_item(who("agent-a"), it["id"], {"note": "half done", "due_at": "2026-03-11T12:00:00Z"})
    store.patch_item(who("agent-a"), it["id"], {"state": "done", "note": "released 14:02"})
    h = store.get_item(it["id"], history=True)["history"]
    assert [x["event"] for x in h] == ["created", "changed", "changed"]
    assert h[1]["by"] == "agent-a" and h[1]["at"] == "2026-03-10T09:35:00Z"
    assert h[1]["changes"] == {"note": ["started", "half done"], "due_at": [None, "2026-03-11T12:00:00Z"]}
    assert h[2]["changes"]["state"] == ["next", "done"] and h[2]["note"] == "released 14:02"


def test_when_precision(store, mk):
    it = mk("agent-a", "Release day", when={"start": "2026-03-12", "end": None, "precision": "day"})
    assert it["when"] == {"start": "2026-03-12", "end": None, "precision": "day"}
    it = mk("agent-a", "Soak", when={"start": "2026-03-09T17:00:42+01:00", "end": "2026-03-11T17:59:59+01:00",
                                     "precision": "hour"})
    assert it["when"] == {"start": "2026-03-09T16:00:00Z", "end": "2026-03-11T16:00:00Z", "precision": "hour"}
    for bad in (
        {"start": "2026-03-12T10:00:00Z", "precision": "day"},
        {"start": "2026-03-12", "precision": "minute"},
        {"start": "2026-03-12", "end": "2026-03-11", "precision": "day"},
    ):
        with pytest.raises(ApiError) as e:
            mk("agent-a", "Bad when", when=bad)
        assert e.value.code == "invalid" and e.value.field.startswith("when")


def test_question_rules(store, mk):
    with pytest.raises(ApiError) as e:
        mk("alice", "Choose", caller="agent-a", kind="decision")
    assert e.value.field == "question"
    with pytest.raises(ApiError):
        mk("agent-a", "Task with a question", question={"text": "Why?"})
    d = mk("alice", "Choose the strategy", caller="agent-b", kind="decision",
           question={"text": "Online or offline?", "options": ["online", "offline"], "recommendation": "online"})
    assert d["question"]["options"] == ["online", "offline"] and d["created_by"] == "agent-b"
    a = mk("alice", "Go for production", caller="agent-a", kind="approval")
    assert a["question"] is None


def test_ladder_rules(store, mk):
    it = mk("agent-a", "With a ladder", overdue=[{"after": "0m", "action": "nudge"},
                                                  {"after": "16h", "action": "notify", "to": "alice"}])
    assert it["overdue"][1] == {"after": "16h", "action": "notify", "to": "alice"}
    for steps in ([{"after": "1h", "action": "notify"}],
                  [{"after": "1h", "action": "nudge", "to": "alice"}],
                  [{"after": "1h", "action": "notify", "to": "agent-b"}]):
        with pytest.raises(ApiError) as e:
            mk("agent-a", "Bad ladder", overdue=steps)
        assert e.value.code == "invalid"


def test_unknown_track_and_owner(store, mk):
    with pytest.raises(ApiError) as e:
        mk("agent-a", "Nowhere", track="no-such-track")
    assert e.value.field == "track"
    with pytest.raises(ApiError) as e:
        mk("agent-z", "Nobody", caller="agent-a")
    assert e.value.field == "owner"
    with pytest.raises(ApiError) as e:
        mk("pages", "A reader can't own items", caller="agent-a")
    assert e.value.field == "owner"


def test_list_filters(store, mk, who):
    q = mk("alice", "Choose", caller="agent-b", kind="decision", question={"text": "Which?"})
    w = mk("agent-b", "Migration tool", track="infra", waits_on=[{"item": q["id"]}])
    late = mk("agent-a", "Notes", due_at="2026-03-10T09:00:00Z")
    risky = mk("agent-a", "Release", waits_on=[{"item": late["id"]}])
    done = mk("agent-a", "Done one")
    store.patch_item(who("agent-a"), done["id"], {"state": "done"})

    def ids(**kw):
        return [i["id"] for i in store.list_items(**kw)[0]]

    assert done["id"] not in ids()
    assert done["id"] in ids(states=None)
    assert ids(owner="alice") == [q["id"]]
    assert ids(track="infra") == [w["id"]]
    assert ids(waiting_on="alice") == [w["id"]]
    assert ids(waits_on_human=True) == [w["id"]]
    assert w["id"] not in ids(waits_on_human=False)
    assert ids(late=True) == [late["id"]]
    assert ids(at_risk=True) == [risky["id"]]
    items, truncated = store.list_items(limit=2)
    assert len(items) == 2 and truncated
    store.clock.advance(minutes=1)
    store.patch_item(who("agent-a"), risky["id"], {"note": "moving"})
    assert ids(changed_since="2026-03-10T09:30:00Z") == [risky["id"]]


def test_concurrent_writes_cannot_both_close_a_loop(tmp_path, clock, who, tracks, mk, store):
    """Two writers, each on its own connection, race to make A wait on B and B wait on A."""
    import threading

    from waypoint.db import connect
    from waypoint.store import Store

    a = mk("agent-a", "A")
    b = mk("agent-b", "B")
    other = Store(connect(tmp_path / "waypoint.db"), clock, store.settings)
    barrier = threading.Barrier(2)
    results = {}

    def write(name, st, iid, dep):
        barrier.wait()
        try:
            st.patch_item(who(name), iid, {"add_waits_on": [{"item": dep}]})
            results[name] = "ok"
        except ApiError as e:
            results[name] = e.code

    threads = [threading.Thread(target=write, args=("agent-a", store, a["id"], b["id"])),
               threading.Thread(target=write, args=("agent-b", other, b["id"], a["id"]))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert sorted(results.values()) == ["loop", "ok"]
    waits = [store.get_item(i)["waits_on"] for i in (a["id"], b["id"])]
    assert sorted(len(w) for w in waits) == [0, 1]
    other.conn.close()


@pytest.mark.parametrize("field,body", [
    ("due_at", {"due_at": "2026-02-30T10:00:00Z"}),
    ("wake_at", {"wake_at": "2026-02-30T10:00:00Z"}),
    ("waits_on.0.time", {"waits_on": [{"time": "2026-02-30T10:00:00Z"}]}),
    ("waits_on.1.time", {"waits_on": [{"time": "2026-03-01T10:00:00Z"}, {"time": "2026-13-01T10:00:00Z"}]}),
])
def test_impossible_dates_are_the_requests_fault(store, mk, field, body):
    with pytest.raises(ApiError) as e:
        mk("agent-a", "Bad date", **body)
    assert e.value.code == "invalid" and e.value.field == field
