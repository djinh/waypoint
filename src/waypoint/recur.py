"""Schedules of recurring items (docs/design/RECURRING.md): checking them, and when they occur next.

Pure functions over the contract's `Schedule` shapes; no database, no clock of its own:

    {"every": "15m", "from": "2026-03-10T08:00:00Z"}     an interval on absolute time
    {"daily": "09:00", "tz": "Europe/Paris"}             wall-clock times in a zone
    {"weekly": ["mon", "thu"], "at": "06:40"}
    {"monthly": 1, "at": "04:30"}                        1 to 28, or "last"

Wall-clock times use the schedule's `tz`, or the deployment's zone. A time that doesn't exist on a
day (clocks go forward) occurs at the first valid minute after it; a time that occurs twice (clocks
go back) occurs once, the first time.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import schema
from .errors import ApiError
from .timeutil import parse_delay, parse_ts

MIN_INTERVAL = timedelta(minutes=5)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SEARCH_DAYS = 400  # a weekly or monthly schedule always occurs within this many days


def check(sched: dict, default_tz: str) -> None:
    """Refuse a schedule the contract or these rules don't allow (400 invalid, naming the field)."""
    try:
        schema.check("Schedule", sched)
    except ApiError as e:
        raise ApiError("invalid", f"schedule: {e.message.split(': ', 1)[-1]}", field="schedule") from None
    if "every" in sched:
        if parse_delay(sched["every"]) < MIN_INTERVAL:
            raise ApiError("invalid", "schedule.every: at least 5m", field="schedule.every")
        if "from" in sched:
            try:
                parse_ts(sched["from"])
            except ValueError:
                raise ApiError("invalid", "schedule.from: a timestamp with a zone", field="schedule.from") from None
        return
    if "tz" in sched and not sched["tz"].strip():
        raise ApiError("invalid", "schedule.tz: not an IANA time zone", field="schedule.tz")
    try:
        ZoneInfo(sched.get("tz") or default_tz)
    except Exception:
        raise ApiError("invalid", "schedule.tz: not an IANA time zone", field="schedule.tz") from None


def next_after(sched: dict, after: datetime, created_at: datetime, default_tz: str) -> datetime:
    """The first occurrence strictly after AFTER (UTC). CREATED_AT anchors an interval without `from`."""
    after = after.astimezone(timezone.utc)
    if "every" in sched:
        step = parse_delay(sched["every"])
        anchor = parse_ts(sched["from"]) if "from" in sched else created_at.astimezone(timezone.utc)
        if after < anchor:
            return anchor
        n = (after - anchor) // step + 1
        return anchor + n * step
    zone = ZoneInfo(sched.get("tz") or default_tz)
    hh, mm = (int(x) for x in _clock(sched).split(":"))
    day = after.astimezone(zone).date()
    for _ in range(SEARCH_DAYS):
        if _occurs_on(sched, day):
            t = _resolve(datetime.combine(day, time(hh, mm)), zone)
            if t > after:
                return t
        day += timedelta(days=1)
    raise ValueError("no occurrence found")  # can't happen for a checked schedule


def between(sched: dict, after: datetime, until: datetime, created_at: datetime, default_tz: str,
            limit: int = 50) -> list[datetime]:
    """The occurrences after AFTER up to and including UNTIL, at most LIMIT of them."""
    out: list[datetime] = []
    t = after
    while len(out) < limit:
        t = next_after(sched, t, created_at, default_tz)
        if t > until:
            break
        out.append(t)
    return out


def _clock(sched: dict) -> str:
    return sched["daily"] if "daily" in sched else sched["at"]


def _occurs_on(sched: dict, day: date) -> bool:
    if "daily" in sched:
        return True
    if "weekly" in sched:
        return WEEKDAYS[day.weekday()] in sched["weekly"]
    want = sched["monthly"]
    if want == "last":
        return day.day == calendar.monthrange(day.year, day.month)[1]
    return day.day == want


def _resolve(local: datetime, zone: ZoneInfo) -> datetime:
    """The UTC time of a naive local time: the first of two (clocks go back), or the first valid
    minute after a time that doesn't exist (clocks go forward)."""
    for minute in range(0, 24 * 60):
        cand = local + timedelta(minutes=minute)
        aware = cand.replace(tzinfo=zone, fold=0)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == cand:
            return utc
    raise ValueError("no valid local time")  # can't happen: a gap is at most a few hours
