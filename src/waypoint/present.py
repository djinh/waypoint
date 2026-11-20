"""Shared by the MCP server and `wp`: reading times and conditions people type, and showing items
on one line. Conveniences only; the daemon checks everything."""

from __future__ import annotations

import re
from datetime import datetime, timezone


class InputError(ValueError):
    """Something typed that isn't a time, an item id or a day."""


_ITEM = re.compile(r"W-[1-9][0-9]*\Z")
_RECURRENCE = re.compile(r"R-[1-9][0-9]*\Z")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
# Control characters and line and paragraph separators. Text other clients wrote (titles, notes,
# questions, links) is shown with these as spaces, so it can never start a line of its own, e.g. one
# that looks like a "[waypoint]" wake-up, in an agent's tool output or a terminal.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def flat(value):
    """VALUE (an API answer) with every string on one line: control characters become spaces."""
    if isinstance(value, str):
        return _CONTROL.sub(" ", value)
    if isinstance(value, list):
        return [flat(v) for v in value]
    if isinstance(value, dict):
        return {k: flat(v) for k, v in value.items()}
    return value


def item_id(value: str) -> str:
    """An item id, checked before it goes into a URL."""
    v = (value or "").strip()
    if not _ITEM.match(v):
        raise InputError(f"not an item id: {value!r} (like W-12)")
    return v


def recurrence_id(value: str) -> str:
    """A recurrence id, checked before it goes into a URL (ASCII digits only, nothing after)."""
    v = (value or "").strip().upper()
    if not _RECURRENCE.match(v):
        raise InputError(f"not a recurrence id: {value!r} (like R-3)")
    return v


def client_name(value: str) -> str:
    v = (value or "").strip()
    if not _NAME.match(v):
        raise InputError(f"not a client name: {value!r}")
    return v


def to_ts(value: str) -> str:
    """A time as the API wants it. Without a zone it is read in this machine's local zone."""
    v = value.strip().replace(" ", "T", 1)
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", v):
        v += ":00"
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise InputError(f"not a time: {value!r} (use e.g. 2026-03-10T14:00 or 2026-03-10T14:00:00Z)") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # local zone
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_condition(value: str) -> dict:
    v = value.strip()
    return {"item": v} if _ITEM.match(v) else {"time": to_ts(v)}


def to_when(start: str, end: str | None) -> dict:
    if _DAY.match(start.strip()):
        return {"start": start.strip(), "end": end.strip() if end else None, "precision": "day"}
    return {"start": to_ts(start), "end": to_ts(end) if end else None, "precision": "minute"}


def local(ts: str | None) -> str:
    if not ts:
        return ""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    now = datetime.now().astimezone()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if abs((dt.date() - now.date()).days) < 6:
        return dt.strftime("%a %H:%M")
    return dt.strftime("%d %b %H:%M")


def line(it: dict) -> str:
    """One item on one line: id, state and flags, title, owner, times, note."""
    flags = "".join([" LATE" if it["late"] else "", " AT-RISK" if it["at_risk"] else "",
                     " RUNNING?" if it.get("running_overdue") else ""])
    rec = f" ({it['recurrence']})" if it.get("recurrence") else ""
    bits = [f"{it['id']} [{it['state']}{flags}] {it['title']}{rec} ({it['owner']}, {it['track']})"]
    if it.get("running_until"):
        bits.append(f"running until {local(it['running_until'])}")
    if it.get("due_at"):
        bits.append(f"due {local(it['due_at'])}")
    if it.get("when"):
        w = it["when"]
        start = w["start"] if w["precision"] == "day" else local(w["start"])
        end = (w["end"] if w["precision"] == "day" else local(w["end"])) if w.get("end") else None
        bits.append(f"when {start}" + (f"–{end}" if end else ""))
    if it.get("wake_at"):
        bits.append(f"wake {local(it['wake_at'])}")
    open_conds = [c.get("item") or local(c.get("time")) for c in it["waits_on"] if not c["met"]]
    if open_conds:
        bits.append("waits on " + ", ".join(open_conds))
    if it.get("note"):
        bits.append(f"— {it['note']}")
    return " · ".join(bits)


def to_answer_at(value: str) -> dict | None:
    """Where a person answers: an issue or pull request URL, or a session's client name; '' for none."""
    v = (value or "").strip()
    if not v:
        return None
    if v.startswith(("https://", "http://")):
        return {"issue": v}
    return {"session": client_name(v)}


def answer_line(it: dict) -> str | None:
    a = it.get("answer_at")
    if not a:
        return None
    where = f"comment on {a['issue']}" if "issue" in a else f"in session {a['session']}"
    return f"answer: {where}" + (" (default: the asking session)" if a.get("default") else "")


def message_line(m: dict) -> str:
    """One of my messages on one line: id, when it fell due, kind, what became of it, and its text."""
    about = f" {m['item']}" if m.get("item") else ""
    return f"{m['id']} {local(m['fell_due_at'])} {m['kind']}{about} ({m['state']}) · {m['text']}"


_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def to_schedule(text: str) -> dict:
    """A schedule as people type it: "every 15m", "daily 09:00", "weekly mon,thu 06:40",
    "monthly 1 04:30" or "monthly last 23:00", each optionally ending in "tz Europe/Paris"."""
    words = (text or "").split()
    tz = None
    if len(words) >= 2 and words[-2] == "tz":
        tz, words = words[-1], words[:-2]
    try:
        kind = words[0]
        if kind == "every" and len(words) == 2:
            return {"every": words[1]}
        if kind == "daily" and len(words) == 2:
            out = {"daily": words[1]}
        elif kind == "weekly" and len(words) == 3:
            out = {"weekly": words[1].split(","), "at": words[2]}
        elif kind == "monthly" and len(words) == 3:
            out = {"monthly": words[1] if words[1] == "last" else int(words[1]), "at": words[2]}
        else:
            raise ValueError
    except (IndexError, ValueError):
        raise InputError(f"not a schedule: {text!r} (like 'every 15m', 'daily 09:00', 'weekly mon,thu 06:40', "
                         "'monthly 1 04:30', with an optional 'tz Europe/Paris')") from None
    if tz:
        out["tz"] = tz
    return out


def schedule_text(s: dict) -> str:
    if "every" in s:
        return f"every {s['every']}"
    tz = f" tz {s['tz']}" if s.get("tz") else ""
    if "daily" in s:
        return f"daily {s['daily']}{tz}"
    if "weekly" in s:
        return f"weekly {','.join(s['weekly'])} {s['at']}{tz}"
    return f"monthly {s['monthly']} {s['at']}{tz}"


def recurrence_line(r: dict) -> str:
    """One recurrence on one line: id, state, title, owner, schedule, next time, open occurrence."""
    bits = [f"{r['id']} [{r['state']}] {r['title']} ({r['owner']}, {r['track']})", schedule_text(r["schedule"])]
    if r.get("next_at"):
        bits.append(f"next {local(r['next_at'])}")
    if r.get("open_occurrence"):
        bits.append(f"open {r['open_occurrence']}")
    if r.get("checkin"):
        bits.append("check-ins" + (f" by {r['checkin_by']}" if r.get("checkin_by") else ""))
    return " · ".join(bits)

