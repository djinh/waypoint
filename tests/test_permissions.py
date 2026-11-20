"""Who may write what (docs/API.md, Permissions; Tracks)."""

from __future__ import annotations

import pytest

from waypoint.errors import ApiError


def refused(fn, *args, code="forbidden"):
    with pytest.raises(ApiError) as e:
        fn(*args)
    assert e.value.code == code
    return e.value


def test_agent_changes_only_its_own_items(store, mk, who):
    it = mk("agent-a", "Agent A's item")
    refused(store.patch_item, who("agent-b"), it["id"], {"note": "mine now"})
    refused(store.patch_item, who("agent-b"), it["id"], {"state": "done"})
    refused(store.patch_item, who("agent-b"), it["id"], {"add_waits_on": [{"item": it["id"]}]})
    assert store.get_item(it["id"])["version"] == 1


def test_agent_hands_an_item_over(store, mk, who):
    it = mk("agent-a", "Hand me over")
    out = store.patch_item(who("agent-a"), it["id"], {"owner": "agent-b"})
    assert out["owner"] == "agent-b"
    store.patch_item(who("agent-b"), it["id"], {"note": "taken over"})
    # Its creator still writes it while it is open (the next tests); a third agent never does.
    refused(store.patch_item, who("agent-c"), it["id"], {"note": "not mine"})


def test_creator_keeps_writing_an_open_task_it_handed_to_another_agent(store, mk, who):
    """A service files an item for an agent, then keeps it current and closes it (design: a client
    writes its own items and those it hands over)."""
    it = mk("agent-b", "CRITICAL pkg CVE-2026-1 on the web server: no fix yet", caller="agent-a")
    assert it["owner"] == "agent-b" and it["created_by"] == "agent-a"
    out = store.patch_item(who("agent-a"), it["id"], {"title": "CRITICAL pkg CVE-2026-1 on the web server: fix available",
                                                      "wake_at": "2030-01-01T00:00:00Z"})
    assert out["updated_by"] == "agent-a" and out["owner"] == "agent-b"
    out = store.patch_item(who("agent-a"), it["id"], {"state": "done", "note": "fixed in 1.2-3"})
    assert out["state"] == "done"
    history = store.get_item(it["id"], history=True)["history"]
    assert [h["by"] for h in history if h["event"] == "changed"][-2:] == ["agent-a", "agent-a"]


def test_creator_cannot_touch_it_once_the_owner_closed_or_dropped_it(store, mk, who):
    it = mk("agent-b", "Filed for agent-b", caller="agent-a")
    store.patch_item(who("agent-b"), it["id"], {"state": "dropped", "note": "mitigated by hand"})
    refused(store.patch_item, who("agent-a"), it["id"], {"state": "next"})
    refused(store.patch_item, who("agent-a"), it["id"], {"note": "reopen please"})
    it2 = mk("agent-b", "Filed and done", caller="agent-a")
    store.patch_item(who("agent-b"), it2["id"], {"state": "done", "note": "done by hand"})
    refused(store.patch_item, who("agent-a"), it2["id"], {"title": "changed"})


@pytest.mark.parametrize("owner", ["alice", "coordinator"])
def test_creator_rule_is_only_for_items_owned_by_agents(store, mk, who, owner):
    it = mk(owner, "Filed for a person or the coordinator", caller="agent-a")
    refused(store.patch_item, who("agent-a"), it["id"], {"note": "still mine?"})


def test_creator_rule_needs_the_creator_and_a_task_or_event(store, mk, who):
    # agent-c creates it for agent-a, who hands it to agent-b: agent-a didn't create it.
    it = mk("agent-a", "Passed along", caller="agent-c")
    store.patch_item(who("agent-a"), it["id"], {"owner": "agent-b"})
    refused(store.patch_item, who("agent-a"), it["id"], {"note": "I only passed it on"})
    store.patch_item(who("agent-c"), it["id"], {"note": "the creator may"})
    ev = mk("agent-b", "Soak ends", caller="agent-a", kind="event", when={"start": "2030-01-02", "precision": "day"})
    store.patch_item(who("agent-a"), ev["id"], {"note": "an event the creator handed over"})


def test_agent_creates_items_for_others(store, mk):
    # wp_ask: the agent creates the human's item and its own item waits on it
    q = mk("alice", "Choose the strategy", caller="agent-b", kind="decision", question={"text": "Which?"})
    mine = mk("agent-b", "Migration tool", waits_on=[{"item": q["id"]}])
    assert q["owner"] == "alice" and q["created_by"] == "agent-b"
    assert mine["state"] == "waiting"


@pytest.mark.parametrize("role_name", ["alice", "coordinator"])
def test_humans_and_coordinator_change_anything(store, mk, who, role_name):
    it = mk("agent-a", "Anyone's item")
    out = store.patch_item(who(role_name), it["id"], {"state": "done", "note": "closed for the owner"})
    assert out["state"] == "done" and out["updated_by"] == role_name


@pytest.mark.parametrize("role_name", ["pages", "host-1"])
def test_readers_and_deliverers_write_nothing(store, mk, who, role_name):
    it = mk("agent-a", "Read only")
    refused(store.create_item, who(role_name), {"track": "release", "title": "x", "kind": "task"})
    refused(store.patch_item, who(role_name), it["id"], {"note": "no"})
    refused(store.put_track, who(role_name), "new-track", {"title": "No"})


def test_track_writers(store, who, tracks):
    # any agent creates a new track
    store.put_track(who("agent-c"), "docs", {"title": "Docs site", "lead": "agent-c"})
    # only the lead, a coordinator or a human replaces an existing one
    refused(store.put_track, who("agent-b"), "release", {"title": "Taken over", "lead": "agent-b"})
    assert store.tracks(ids={"release"})[0]["title"] == "Release 2.4"
    store.put_track(who("agent-a"), "release", {"title": "Release 2.4.1", "lead": "agent-a", "order": 10})
    store.put_track(who("coordinator"), "release", {"title": "Release 2.4", "lead": "agent-b", "order": 10})
    store.put_track(who("alice"), "infra", {"title": "Infra", "lead": None, "archived": True})
    assert [t["id"] for t in store.tracks()] == ["docs", "release"]
    assert [t["id"] for t in store.tracks(archived=True)] == ["docs", "infra", "release"]  # by order, then id
    e = refused(store.put_track, who("agent-a"), "x", {"title": "Bad lead", "lead": "nobody"}, code="invalid")
    assert e.field == "lead"


def test_creator_may_not_set_the_owners_item_running(store, mk, who):
    """Only the owner knows it is working on it (#57); closing and next/waiting stay allowed."""
    it = mk("agent-b", "Filed for agent-b", caller="agent-a")
    refused(store.patch_item, who("agent-a"), it["id"], {"state": "running"})
    refused(store.patch_item, who("agent-a"), it["id"], {"running_until": "2030-01-01T00:00:00Z"})
    assert store.get_item(it["id"])["state"] == "next"
    store.patch_item(who("agent-b"), it["id"], {"state": "running"})  # the owner may
    assert store.patch_item(who("agent-a"), it["id"], {"state": "next"})["state"] == "next"
    assert store.patch_item(who("agent-a"), it["id"], {"state": "waiting"})["state"] in ("next", "waiting")
    assert store.patch_item(who("agent-a"), it["id"], {"state": "dropped", "note": "gone"})["state"] == "dropped"
    it2 = mk("agent-b", "Filed again", caller="agent-a")
    assert store.patch_item(who("agent-a"), it2["id"], {"state": "done"})["state"] == "done"
