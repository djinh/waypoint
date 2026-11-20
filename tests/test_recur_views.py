"""Recurrences in the views and the event stream: upcoming occurrences on the timeline (computed,
never stored), a person's recurrences in their view, and `event: recurrence`."""

from __future__ import annotations

from waypoint import views
from waypoint.events import Broadcaster
from waypoint.recurrences import Recurrences


def test_the_timeline_shows_upcoming_occurrences(store, tokens, tracks, who):
    rec = Recurrences.of(store)
    rec.create(who("agent-a"), {"track": "release", "title": "Morning review", "label": "review",
                                "schedule": {"daily": "09:00"}})
    rec.create(who("agent-b"), {"track": "infra", "title": "Nightly", "schedule": {"daily": "02:00"}})
    paused = rec.create(who("agent-a"), {"track": "release", "title": "Paused", "schedule": {"daily": "12:00"}})
    rec.patch(who("agent-a"), paused["id"], {"state": "paused"})
    tl = views.timeline(store)
    morning = [u for u in tl["upcoming"] if u["recurrence"] == "R-1"]
    assert morning[0] == {"recurrence": "R-1", "at": "2026-03-11T08:00:00Z", "owner": "agent-a", "track": "release",
                          "title": "Morning review", "label": "review"}
    assert len(morning) == 9  # 11 to 19 March within the default 9-day window
    assert {u["recurrence"] for u in tl["upcoming"]} == {"R-1", "R-2"}  # not the paused one
    assert [u["at"] for u in tl["upcoming"]] == sorted(u["at"] for u in tl["upcoming"])
    only_infra = views.timeline(store, track_ids=["infra"])
    assert {u["recurrence"] for u in only_infra["upcoming"]} == {"R-2"}
    assert store.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0  # nothing stored


def test_a_persons_view_lists_their_recurrences(store, tokens, tracks, who):
    rec = Recurrences.of(store)
    rec.create(who("alice"), {"track": "release", "title": "Weekly sign-off", "owner": "alice",
                              "schedule": {"weekly": ["fri"], "at": "16:00"}})
    rec.create(who("agent-a"), {"track": "release", "title": "Not hers", "schedule": {"daily": "09:00"}})
    got = views.for_human(store, "alice")
    assert [r["id"] for r in got["recurrences"]] == ["R-1"]


def test_changes_to_a_recurrence_are_events(store, tokens, tracks, who):
    b = Broadcaster.of(store)
    rec = Recurrences.of(store)
    rec.create(who("agent-a"), {"track": "release", "title": "Morning review", "schedule": {"daily": "09:00"}})
    rec.patch(who("agent-a"), "R-1", {"state": "paused"})
    events = [(kind, data) for _, kind, data in b._buffer if kind == "recurrence"]
    assert [d["state"] for _, d in events] == ["active", "paused"]
    assert events[-1][1] == {"id": "R-1", "version": 2, "state": "paused", "next_at": None}
