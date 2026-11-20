"""Time source. Everything that asks "what time is it" asks a Clock, so tests can fake it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(microsecond=0)


class FakeClock(Clock):
    """A clock that only moves when told to."""

    def __init__(self, start: datetime):
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when.astimezone(timezone.utc)

    def advance(self, **kw) -> datetime:
        self._now += timedelta(**kw)
        return self._now
