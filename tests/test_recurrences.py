"""Recurrences in the store and the API: creating, reading, changing, pausing, resuming and stopping
them, who may do which, retirement, and migration 7."""

from __future__ import annotations

import sqlite3

import pytest

from waypoint import db
from waypoint.errors import ApiError
from waypoint.recurrences import Recurrences

DAILY = {"daily": "09:00"}


@pytest.fixture
def rec(store, tokens, tracks):
    return Recurrences.of(store)


def body(**kw):
    return {"track": "release", "title": "Morning review", "schedule": DAILY, **kw}


# ---------------------------------------------------------------- creating and reading
def test_create_and_read(rec, who):
    r = rec.create(who("agent-a"), body(label="review", note="the open issues", window="2h", checkin=True))
    assert r["id"] == "R-1" and r["owner"] == "agent-a" and r["state"] == "active" and r["kind"] == "task"
    assert r["next_at"] == "2026-03-11T08:00:00Z"  # 09:00 Paris; it is 10:30 there now, so tomorrow
    assert r["window"] == "2h" and r["checkin"] is True and r["checkin_by"] is None and r["open_occurrence"] is None
    got = rec.get("R-1", history=True)
    assert got["history"][0]["event"] == "created" and got["history"][0]["by"] == "agent-a"
    assert [x["id"] for x in rec.list()] == ["R-1"]


@pytest.mark.parametrize("extra, code, field", [
    ({"schedule": {"every": "1m"}}, "invalid", "schedule.every"),
    ({"schedule": {"daily": "09:00", "tz": ""}}, "invalid", "schedule.tz"),
    ({"title": "two\nlines"}, "invalid", "title"),
    ({"note": "token ghp_" + "a" * 36}, "content", "note"),
    ({"track": "nowhere"}, "invalid", "track"),
    ({"checkin_by": "pages"}, "invalid", "checkin_by"),
    ({"overdue": [{"after": "1h", "action": "notify"}]}, "invalid", "overdue.0.to"),
])
def test_bad_recurrences_are_refused(rec, who, extra, code, field):
    with pytest.raises(ApiError) as e:
        rec.create(who("agent-a"), body(**extra))
    assert e.value.code == code and e.value.field == field


# ---------------------------------------------------------------- who may do what
def test_a_persons_recurrence_only_by_that_person_or_a_coordinator(rec, who):
    with pytest.raises(ApiError) as e:
        rec.create(who("agent-a"), body(owner="alice"))
    assert e.value.code == "forbidden"
    with pytest.raises(ApiError):
        rec.create(who("bob"), body(owner="alice"))  # another human
    rec.create(who("alice"), body(owner="alice"))
    r = rec.create(who("coordinator"), body(owner="alice"))
    with pytest.raises(ApiError):
        rec.patch(who("agent-a"), r["id"], {"note": "x"})
    rec.patch(who("alice"), r["id"], {"note": "fine"})


def test_an_agent_may_hand_one_to_another_agent_and_keep_writing_it(rec, who):
    r = rec.create(who("agent-a"), body(owner="agent-b"))
    rec.patch(who("agent-a"), r["id"], {"note": "creator"})
    rec.patch(who("agent-b"), r["id"], {"note": "owner"})
    with pytest.raises(ApiError) as e:
        rec.patch(who("agent-c"), r["id"], {"note": "stranger"})
    assert e.value.code == "forbidden"
    with pytest.raises(ApiError):
        rec.create(who("pages"), body())  # readers write nothing


# ---------------------------------------------------------------- changing, pausing, stopping
def test_pause_resume_stop(rec, who, clock):
    r = rec.create(who("agent-a"), body())
    p = rec.patch(who("agent-a"), r["id"], {"state": "paused"})
    assert p["state"] == "paused" and p["next_at"] is None and p["version"] == 2
    clock.advance(days=3)  # 2026-03-13 09:30Z
    a = rec.patch(who("agent-a"), r["id"], {"state": "active"})
    assert a["next_at"] == "2026-03-14T08:00:00Z"  # from now, the paused days not caught up
    rec.patch(who("agent-a"), r["id"], {"state": "stopped"})
    with pytest.raises(ApiError) as e:
        rec.patch(who("agent-a"), r["id"], {"state": "active"})
    assert e.value.field == "state"
    assert rec.list() == [] and [x["state"] for x in rec.list(states=None)] == ["stopped"]


def test_a_new_schedule_takes_effect_from_now_and_versions_conflict(rec, who):
    r = rec.create(who("agent-a"), body())
    s = rec.patch(who("agent-a"), r["id"], {"schedule": {"every": "30m"}, "if_version": 1})
    assert s["schedule"] == {"every": "30m"} and s["next_at"] == "2026-03-10T10:00:00Z"  # anchored at creation (09:30Z), strictly after now
    with pytest.raises(ApiError) as e:
        rec.patch(who("agent-a"), r["id"], {"note": "late", "if_version": 1})
    assert e.value.code == "version_conflict"
    h = rec.get(r["id"], history=True)["history"]
    assert h[-1]["changes"]["schedule"][1] == '{"every": "30m"}'


# ---------------------------------------------------------------- retirement
def test_a_retired_owner_pauses_its_recurrences_and_the_coordinator_hears(rec, store, who):
    store.route("coordinator", "host-1")
    r = rec.create(who("agent-a"), body(owner="agent-c"))
    store.change_client(who("alice"), "agent-c", {"retired": True})
    got = rec.get(r["id"], history=True)
    assert got["state"] == "paused" and got["next_at"] is None
    assert got["history"][-1]["note"] == "paused: its owner agent-c was retired"
    with store.read() as c:
        [m] = c.execute("SELECT recipient, kind, text FROM messages").fetchall()
    assert m["recipient"] == "coordinator" and m["kind"] == "recurring" and "R-1" in m["text"]


def test_a_retired_checkin_client_is_cleared(rec, store, who):
    r = rec.create(who("agent-a"), body(checkin=True, checkin_by="agent-c"))
    assert r["checkin_by"] == "agent-c"
    store.change_client(who("alice"), "agent-c", {"retired": True})
    got = rec.get(r["id"], history=True)
    assert got["checkin_by"] is None and got["history"][-1]["changes"] == {"checkin_by": ["agent-c", None]}


# ---------------------------------------------------------------- the API
def test_the_api(server, tokens, tracks):
    a = tokens["agent-a"]
    st, r, _ = server("POST", "/v1/recurrences", a, body())
    assert st == 201 and r["id"] == "R-1"
    assert server("GET", "/v1/recurrences/R-1", tokens["pages"])[1]["title"] == "Morning review"
    assert server("GET", "/v1/recurrences/R-9", a)[0] == 404
    assert server("GET", "/v1/recurrences/R-01", a)[0] == 404
    st, r, _ = server("PATCH", "/v1/recurrences/R-1", a, {"state": "paused"})
    assert st == 200 and r["state"] == "paused"
    assert [x["id"] for x in server("GET", "/v1/recurrences?state=paused", a)[1]["recurrences"]] == ["R-1"]
    assert server("GET", "/v1/recurrences?state=active", a)[1]["recurrences"] == []
    assert server("GET", "/v1/recurrences?state=sleeping", a)[0] == 400
    assert server("POST", "/v1/recurrences", a, body(owner="alice"))[0] == 403
    st, item, _ = server("POST", "/v1/items", a, {"track": "release", "title": "plain", "kind": "task"})
    assert item["recurrence"] is None and item["occurrence_at"] is None


# ---------------------------------------------------------------- migration 7
def test_migration_7_from_schema_6(tmp_path):
    path = tmp_path / "w.db"
    old = sqlite3.connect(path)
    db.migrate(old, db.MIGRATIONS[:6])
    old.close()
    conn = db.connect(path)
    assert db.schema_version(conn) == len(db.MIGRATIONS) == 7
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    assert {"recurrence", "occurrence_at"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM recurrences").fetchone()[0] == 0
    conn.close()


def test_after_the_owner_is_retired_a_coordinator_or_human_can_re_own_resume_or_stop(rec, store, who):
    for owner in ("agent-c", "bob"):
        caller = who("coordinator") if owner == "bob" else who("agent-a")
        r = rec.create(caller, body(owner=owner))
        store.change_client(who("alice"), owner, {"retired": True})
        with pytest.raises(ApiError) as e:
            rec.patch(who("agent-a"), r["id"], {"state": "active"})
        assert e.value.code == "forbidden"
        moved = rec.patch(who("coordinator"), r["id"], {"owner": "agent-b", "state": "active"})
        assert moved["owner"] == "agent-b" and moved["state"] == "active" and moved["next_at"]
        assert rec.patch(who("alice"), r["id"], {"state": "stopped"})["state"] == "stopped"


def test_the_owner_hands_it_to_another_agent_but_not_to_a_person(rec, who):
    r = rec.create(who("agent-a"), body(owner="agent-b"))
    handed = rec.patch(who("agent-b"), r["id"], {"owner": "agent-c"})
    assert handed["owner"] == "agent-c"
    with pytest.raises(ApiError) as e:
        rec.patch(who("agent-c"), r["id"], {"owner": "alice"})
    assert e.value.code == "forbidden"
    assert rec.patch(who("coordinator"), r["id"], {"owner": "alice"})["owner"] == "alice"


def test_the_coordinators_recurring_message_points_to_wp_messages():
    from waypoint.adapters import ref_line
    assert ref_line({"kind": "recurring", "item": None}).endswith("read it with wp_messages")
