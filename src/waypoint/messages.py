"""Messages: what Waypoint tells whom, created the moment it falls due.

A message row is written in the same transaction as the change or scheduler step that causes it,
so it can neither be lost nor created twice. Handing messages to deliverers is deliveries.py's job.
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .store import Store, Transition, item_id, item_num
from .timeutil import fmt, parse_ts

TEXT_MAX = 1000
SUMMARY_MAX = 140


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


class Outbox:
    """Composes messages and writes them; also turns state transitions into messages.

    One per store (``Outbox.of(store)``), so transitions never become messages twice."""

    @classmethod
    def of(cls, store: Store) -> "Outbox":
        if getattr(store, "outbox", None) is None:
            store.outbox = cls(store)
        return store.outbox

    def __init__(self, store: Store):
        self.store = store
        # Set by the scheduler for the length of a pass that follows an outage: (down_from, down_until).
        self.outage: tuple[datetime, datetime] | None = None
        # Called (without arguments) whenever a message is written; deliveries wake long-polls with it.
        self.on_new_message: list = []
        store.listeners.append(self.on_transitions)

    # ------------------------------------------------------------------ formatting
    def local(self, ts: str) -> str:
        """A time as people read it in the deployment's zone: '14:00' today, 'Tue 12 Mar 14:00' otherwise."""
        tz = ZoneInfo(self.store.settings.tz)
        t = parse_ts(ts).astimezone(tz)
        now = self.store.now().astimezone(tz)
        return t.strftime("%H:%M") if t.date() == now.date() else t.strftime("%a %d %b %H:%M")

    def span(self, a: datetime, b: datetime) -> str:
        return f"{self.local(fmt(a))}–{self.local(fmt(b))}"

    @staticmethod
    def ref(row) -> str:
        return f"{item_id(row['num'])} ({row['label'] or row['title']})"

    def late_note(self, due: str, late: dict | None) -> str:
        if not late:
            return ""
        down = self.span(parse_ts(late["down_from"]), parse_ts(late["down_until"]))
        return f" (late: due {self.local(due)}, Waypoint was down {down})"

    def late_for(self, due: str) -> dict | None:
        """The late note for something that fell due at `due`, if that was during the outage."""
        if self.outage is None:
            return None
        down_from, down_until = self.outage
        if not (down_from < parse_ts(due) <= down_until):
            return None
        return {"due_at": due, "reason": "waypoint_down", "down_from": fmt(down_from), "down_until": fmt(down_until)}

    # ------------------------------------------------------------------ writing
    def add(self, c, recipient: str | None, kind: str, row, text: str, *, items=(), fell_due_at: str | None = None,
            late: dict | None = None, summary: str | None = None) -> int | None:
        """Write one message; returns its number. No recipient (no coordinator configured): nothing."""
        if not recipient:
            return None
        if self.store.deliverer_of(recipient) is None:
            # nobody can reach this recipient: the coordinator gets it, if someone can reach them
            coordinator = self.store.coordinator()
            if coordinator and coordinator != recipient and self.store.deliverer_of(coordinator):
                text = f"For {recipient} (no deliverer registered): {text}"
                recipient = coordinator
        now = fmt(self.store.now())
        if summary is None:
            summary = f"{row['title']} ({row['owner']})" if row is not None else text
        summary = _cut(summary, SUMMARY_MAX)
        links = row["links"] if row is not None else "[]"
        cur = c.execute(
            "INSERT INTO messages (recipient, kind, item, items, fell_due_at, late, text, summary, links, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (recipient, kind, row["num"] if row is not None else None, json.dumps(list(items)),
             fell_due_at or now, json.dumps(late) if late else None, _cut(text, TEXT_MAX), summary, links, now),
        )
        if row is not None:
            self.store._history(c, row["num"], None, "message", None, f"{kind} to {recipient}")
        for fn in self.on_new_message:
            fn()
        return cur.lastrowid

    def item_row(self, c, num: int):
        return c.execute("SELECT * FROM items WHERE num = ?", (num,)).fetchone()

    # ------------------------------------------------------------------ transitions
    def on_transitions(self, c, transitions: list[Transition]) -> None:
        for t in transitions:
            row = self.item_row(c, item_num(t.item))
            if t.kind in ("unblocked", "replan") and self.store.actor == row["owner"]:
                continue  # the owner's own write caused it: it knows already
            if t.kind == "unblocked":
                self._unblocked(c, row, t.because)
            elif t.kind == "replan":
                self._replan(c, row, t.because)
            # 'late' and 'at_risk' are told by the overdue ladder (nudge, flag), not at once

    def _unblocked(self, c, row, because: list[str]) -> None:
        parts, late, fell = [], None, None
        for b in because:
            if b.startswith("W-"):
                parts.append(f"{self.ref(self.item_row(c, item_num(b)))} is done")
            else:
                parts.append(f"{self.local(b)} has passed")
                late = late or self.late_for(b)
                fell = fell or (b if late else None)
        lead = "; ".join(parts) + "; " if parts else ""
        text = f"{lead}{self.ref(row)} can start{self.late_note(fell, late) if late else ''}"
        self.add(c, row["owner"], "unblocked", row, text, items=[b for b in because if b.startswith("W-")],
                 fell_due_at=fell, late=late)

    def _replan(self, c, row, because: list[str]) -> None:
        if because:
            reasons = []
            for b in because:
                up = self.item_row(c, item_num(b))
                reasons.append(f"{self.ref(up)} was dropped" + (f": {up['note']}" if up["note"] else ""))
            text = f"{'; '.join(reasons)}. {self.ref(row)} waits on it: please re-plan."
        else:
            text = f"Something {self.ref(row)} waits on was reopened; it is waiting again. Please re-plan."
        self.add(c, row["owner"], "replan", row, text, items=because)
