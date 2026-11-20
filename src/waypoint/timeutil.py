"""Timestamps and days as the contract writes them."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DELAY = re.compile(r"^([0-9]{1,4})([mhd])$")


def parse_ts(value: str) -> datetime:
    """An RFC 3339 timestamp with a zone, as an aware UTC datetime. ValueError otherwise."""
    if not isinstance(value, str) or not _TS.match(value):
        raise ValueError("not a timestamp with a zone")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def fmt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_ts(value: str | None) -> str | None:
    """Input timestamp to the stored form (UTC, seconds, Z)."""
    return None if value is None else fmt(parse_ts(value).replace(microsecond=0))


def parse_day(value: str) -> date:
    if not isinstance(value, str) or not _DAY.match(value):
        raise ValueError("not a day")
    return date.fromisoformat(value)


def parse_delay(value: str) -> timedelta:
    m = _DELAY.match(value)
    if not m:
        raise ValueError("not a delay")
    n, unit = int(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
