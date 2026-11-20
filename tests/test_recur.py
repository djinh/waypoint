"""The schedule engine of recurring items: checking schedules, and the next occurrence in a zone,
across daylight saving changes and month ends."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from waypoint import recur
from waypoint.errors import ApiError

UTC = timezone.utc
CREATED = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)


def z(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def nxt(sched, after, tz="Europe/Amsterdam"):
    return recur.next_after(sched, z(after), CREATED, tz).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- checking
@pytest.mark.parametrize("sched, field", [
    ({"every": "4m"}, "schedule.every"),
    ({"every": "15s"}, "schedule"),
    ({"every": "1h", "tz": "Europe/Paris"}, "schedule"),       # an interval has no zone
    ({"daily": "25:00"}, "schedule"),
    ({"daily": "09:00", "tz": "Mars/Olympus"}, "schedule.tz"),
    ({"weekly": [], "at": "09:00"}, "schedule"),
    ({"weekly": ["mon", "funday"], "at": "09:00"}, "schedule"),
    ({"monthly": 31, "at": "09:00"}, "schedule"),
    ({"monthly": 0, "at": "09:00"}, "schedule"),
    ({"hourly": True}, "schedule"),
    ({"daily": "09:00\n"}, "schedule"),
    ({"daily": "09:00", "tz": ""}, "schedule.tz"),
])
def test_bad_schedules_are_refused(sched, field):
    with pytest.raises(ApiError) as e:
        recur.check(sched, "Europe/Amsterdam")
    assert e.value.code == "invalid" and e.value.field == field


@pytest.mark.parametrize("sched", [
    {"every": "5m"}, {"every": "2h", "from": "2026-03-01T08:00:00+01:00"}, {"daily": "09:00"},
    {"weekly": ["mon", "thu"], "at": "06:40", "tz": "Asia/Kathmandu"}, {"monthly": "last", "at": "23:59"},
    {"monthly": 28, "at": "00:00"},
])
def test_good_schedules_pass(sched):
    recur.check(sched, "Europe/Amsterdam")


# ---------------------------------------------------------------- intervals
def test_an_interval_is_anchored_and_strictly_after():
    s = {"every": "15m"}
    assert nxt(s, "2026-03-01T07:00:00Z") == "2026-03-01T08:00:00Z"  # before the anchor: the anchor
    assert nxt(s, "2026-03-01T08:00:00Z") == "2026-03-01T08:15:00Z"  # strictly after
    assert nxt(s, "2026-03-01T08:07:00Z") == "2026-03-01T08:15:00Z"
    assert nxt({"every": "2h", "from": "2026-03-10T09:30:00Z"}, "2026-03-11T00:00:00Z") == "2026-03-11T01:30:00Z"


def test_an_interval_ignores_daylight_saving():
    s = {"every": "1h", "from": "2026-03-29T00:00:00Z"}  # clocks go forward at 01:00Z in Amsterdam
    assert nxt(s, "2026-03-29T00:30:00Z") == "2026-03-29T01:00:00Z"
    assert nxt(s, "2026-03-29T01:00:00Z") == "2026-03-29T02:00:00Z"


# ---------------------------------------------------------------- wall-clock times
def test_daily_in_the_zone():
    assert nxt({"daily": "09:00"}, "2026-03-10T07:00:00Z") == "2026-03-10T08:00:00Z"  # 09:00 CET
    assert nxt({"daily": "09:00"}, "2026-03-10T08:00:00Z") == "2026-03-11T08:00:00Z"
    assert nxt({"daily": "09:00"}, "2026-06-10T08:00:00Z") == "2026-06-11T07:00:00Z"  # 09:00 CEST
    assert nxt({"daily": "09:00", "tz": "UTC"}, "2026-06-10T08:00:00Z") == "2026-06-10T09:00:00Z"


def test_a_time_that_does_not_exist_fires_at_the_first_valid_minute():
    # 2026-03-29, Amsterdam: 02:00 local jumps to 03:00
    assert nxt({"daily": "02:30"}, "2026-03-28T12:00:00Z") == "2026-03-29T01:00:00Z"  # 03:00 CEST
    assert nxt({"daily": "02:30"}, "2026-03-29T01:00:00Z") == "2026-03-30T00:30:00Z"  # back to 02:30 CEST


def test_a_time_that_occurs_twice_fires_once_the_first_time():
    # 2026-10-25, Amsterdam: 03:00 local goes back to 02:00, so 02:30 happens twice
    assert nxt({"daily": "02:30"}, "2026-10-24T12:00:00Z") == "2026-10-25T00:30:00Z"  # 02:30 CEST
    assert nxt({"daily": "02:30"}, "2026-10-25T00:30:00Z") == "2026-10-26T01:30:00Z"  # not 01:30Z the same day


def test_weekly():
    s = {"weekly": ["mon", "thu"], "at": "06:40"}
    assert nxt(s, "2026-03-10T12:00:00Z") == "2026-03-12T05:40:00Z"  # Tue -> Thu 06:40 CET
    assert nxt(s, "2026-03-12T05:40:00Z") == "2026-03-16T05:40:00Z"  # Thu -> Mon


def test_monthly_and_the_last_day():
    assert nxt({"monthly": 1, "at": "04:30"}, "2026-03-10T00:00:00Z") == "2026-04-01T02:30:00Z"
    assert nxt({"monthly": "last", "at": "12:00", "tz": "UTC"}, "2026-02-10T00:00:00Z") == "2026-02-28T12:00:00Z"
    assert nxt({"monthly": "last", "at": "12:00", "tz": "UTC"}, "2028-02-10T00:00:00Z") == "2028-02-29T12:00:00Z"
    assert nxt({"monthly": "last", "at": "12:00", "tz": "UTC"}, "2026-04-30T12:00:00Z") == "2026-05-31T12:00:00Z"


def test_a_zone_with_a_quarter_hour_offset():
    assert nxt({"daily": "09:00", "tz": "Asia/Kathmandu"}, "2026-03-10T00:00:00Z") == "2026-03-10T03:15:00Z"


def test_between_lists_occurrences_up_to_a_limit():
    got = recur.between({"every": "15m"}, z("2026-03-01T08:00:00Z"), z("2026-03-01T09:00:00Z"), CREATED, "UTC")
    assert [t.strftime("%H:%M") for t in got] == ["08:15", "08:30", "08:45", "09:00"]
    assert len(recur.between({"every": "5m"}, CREATED, z("2026-03-02T08:00:00Z"), CREATED, "UTC", limit=10)) == 10
