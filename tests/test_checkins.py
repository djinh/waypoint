"""Check-ins: a job on a timer reports each run; a run that isn't reported is late, climbs the ladder,
and shows in /v1/status."""

from __future__ import annotations

from datetime import datetime

import pytest

from waypoint.errors import ApiError
from waypoint.monitor import status
from waypoint.recurrences import Recurrences
from waypoint.scheduler import Scheduler


@pytest.fixture
def sched(store, tokens, tracks):
    store.settings.outage_seconds = 10**9
    for name in ("agent-a", "coordinator"):
        store.route(name, "host-1")
    return Scheduler(store)


@pytest.fixture
def rec(store, sched, who):
    r = Recurrences.of(store)
    # a nightly backup owned by the coordinator; the script checks in as agent-c
    r.create(who("coordinator"), {"track": "release", "title": "Nightly backup", "owner": "coordinator",
                                  "schedule": {"every": "1d", "from": "2026-03-10T03:00:00Z"}, "window": "1h",
                                  "checkin": True, "checkin_by": "agent-c", "overlap": "allow"})
    return r


def at(store, iso):
    store.clock.set(datetime.fromisoformat(iso.replace("Z", "+00:00")))


def test_ok_closes_the_oldest_open_occurrence(store, sched, rec, who):
    for day in ("11", "12"):  # two runs fall due; the first wasn't reported in time
        at(store, f"2026-03-{day}T03:00:00Z")
        sched.run_pass()
    got = rec.checkin(who("agent-c"), "R-1", {"result": "ok", "note": "backup ok, 41 MB"})
    assert got["occurrence"]["occurrence_at"] == "2026-03-11T03:00:00Z"  # the oldest, not the newest
    assert got["occurrence"]["state"] == "done" and got["occurrence"]["note"] == "backup ok, 41 MB"
    assert got["recurrence"]["open_occurrence"] == "W-2"
    h = rec.get("R-1", history=True)["history"][-1]
    assert h["by"] == "agent-c" and h["event"] == "checkin" and h["note"].startswith("ok: W-1")


def test_failed_drops_it_and_tells_the_owner(store, sched, rec, who):
    at(store, "2026-03-11T03:00:00Z")
    sched.run_pass()
    got = rec.checkin(who("agent-c"), "R-1", {"result": "failed", "note": "disk full"})
    assert got["occurrence"]["state"] == "dropped" and got["occurrence"]["note"] == "disk full"
    [m] = store.conn.execute("SELECT recipient, text FROM messages WHERE kind = 'recurring' AND text LIKE '%failed%'")
    assert m["recipient"] == "coordinator" and "disk full" in m["text"]


def test_a_checkin_with_nothing_open_is_recorded(store, sched, rec, who):
    got = rec.checkin(who("agent-c"), "R-1", {"result": "ok"})
    assert got["occurrence"] is None
    assert rec.get("R-1", history=True)["history"][-1]["note"] == "ok with no open occurrence"


def test_who_may_check_in(store, sched, rec, who):
    at(store, "2026-03-11T03:00:00Z")
    sched.run_pass()
    with pytest.raises(ApiError) as e:
        rec.checkin(who("agent-b"), "R-1", {"result": "ok"})
    assert e.value.code == "forbidden"
    with pytest.raises(ApiError) as e:
        rec.patch(who("agent-c"), "R-1", {"note": "the check-in client can't change it"})
    assert e.value.code == "forbidden"
    rec.checkin(who("coordinator"), "R-1", {"result": "ok"})
    with pytest.raises(ApiError) as e:
        rec.checkin(who("agent-c"), "R-1", {"result": "ok", "note": "key " + "deadbeef" * 5})
    assert e.value.code == "content"


def test_no_checkins_for_a_recurrence_without_them(store, sched, rec, who):
    rec.create(who("agent-a"), {"track": "release", "title": "Review", "schedule": {"daily": "09:00"}})
    with pytest.raises(ApiError) as e:
        rec.checkin(who("agent-a"), "R-2", {"result": "ok"})
    assert e.value.field == "checkin"


def test_a_missed_run_is_late_and_listed_in_status(store, sched, rec, who):
    at(store, "2026-03-11T03:00:00Z")
    sched.run_pass()
    assert status(store)["missed_checkins"] == []
    at(store, "2026-03-11T04:01:00Z")  # the window (1h) passed without a check-in
    sched.run_pass()
    s = status(store)
    assert s["missed_checkins"] == [{"recurrence": "R-1", "occurrence": "W-1", "occurrence_at": "2026-03-11T03:00:00Z"}]
    assert s["ok"] is False
    kinds = [r[0] for r in store.conn.execute("SELECT kind FROM messages WHERE item = 1 ORDER BY num")]
    assert "nudge" in kinds  # the ladder's first step, to the owner


def test_the_api(server, tokens, tracks, store, sched, rec):
    at(store, "2026-03-11T03:00:00Z")
    sched.run_pass()
    st, out, _ = server("POST", "/v1/recurrences/R-1/checkin", tokens["agent-c"], {"result": "ok"})
    assert st == 200 and out["occurrence"]["state"] == "done"
    assert server("POST", "/v1/recurrences/R-1/checkin", tokens["agent-b"], {"result": "ok"})[0] == 403
    assert server("POST", "/v1/recurrences/R-1/checkin", tokens["agent-c"], {"result": "maybe"})[0] == 400


def test_readers_and_deliverers_may_not_check_in(server, tokens, tracks, store, sched, rec):
    for who in ("pages", "host-1"):
        assert server("POST", "/v1/recurrences/R-1/checkin", tokens[who], {"result": "ok"})[0] == 403, who
