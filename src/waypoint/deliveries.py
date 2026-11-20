"""Deliveries: handing messages to deliverers and taking their confirmations (docs/API.md, Deliveries).

A deliverer sees the messages of the recipients registered with it. A message is handed out when
it is not confirmed, not withdrawn and not under an unexpired lease; each hand-out takes a lease
and raises `attempt`. Confirming is idempotent. Messages that went stale before anyone confirmed
them are withdrawn, never handed out again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import datetime, timedelta

from . import content, schema
from .errors import ApiError
from .messages import Outbox
from .store import Client, Store, Transition, item_id
from .timeutil import fmt, parse_ts

WITHDRAW_ON_CHANGE = ("wake", "nudge", "at_risk", "running_check")

log = logging.getLogger("waypoint.deliveries")


def delivery_id(num: int) -> str:
    return f"D-{num}"


_DELIVERY_ID = re.compile(r"D-([1-9][0-9]{0,17})")


def delivery_num(did: str) -> int:
    m = _DELIVERY_ID.fullmatch(did) if isinstance(did, str) else None
    if m is None:
        raise ApiError("not_found", "no such delivery")
    return int(m.group(1))


class Signal:
    """Wakes long-polls when something may be ready. Safe to fire from any thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    def fire(self) -> None:
        with self._lock:
            waiters = list(self._waiters)
        for entry in waiters:
            loop, event = entry
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # its loop has closed (shutting down): nobody is waiting there
                with self._lock:
                    self._waiters.discard(entry)

    def register(self) -> tuple:
        """Start listening before looking, so a signal between the look and the wait isn't lost."""
        loop = asyncio.get_running_loop()
        entry = (loop, asyncio.Event())
        with self._lock:
            self._waiters.add(entry)
        return entry

    def unregister(self, entry) -> None:
        with self._lock:
            self._waiters.discard(entry)


class Deliveries:
    """One per store (``Deliveries.of(store)``), like the outbox it uses."""

    @classmethod
    def of(cls, store: Store) -> "Deliveries":
        if getattr(store, "deliveries", None) is None:
            store.deliveries = cls(store, Outbox.of(store))
        return store.deliveries

    def __init__(self, store: Store, outbox: Outbox):
        self.store = store
        self.outbox = outbox
        self.signal = Signal()
        store.change_listeners.append(self.on_client_change)
        store.listeners.append(self.on_transitions)
        outbox.on_new_message.append(lambda: self.signal.fire())

    # ------------------------------------------------------------------ handing out
    def _pending_sql(self) -> str:
        return ("FROM messages m JOIN clients r ON r.name = m.recipient "
                "WHERE r.deliverer = ? AND r.retired_at IS NULL AND m.acked_at IS NULL AND m.withdrawn_at IS NULL")

    def fetch(self, caller: Client, limit: int = 50) -> list[dict]:
        """Hand out what is ready for this deliverer now (possibly nothing), taking leases."""
        if caller.role != "deliverer":
            raise ApiError("forbidden", "only deliverers fetch deliveries")
        st = self.store
        with st.tx() as c:
            now = st.now()
            nows = fmt(now)
            seen = c.execute("SELECT last_seen FROM deliverers WHERE name = ?", (caller.name,)).fetchone()
            last_seen = seen["last_seen"] if seen else None
            c.execute("INSERT INTO deliverers (name, last_seen) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET "
                      "last_seen = excluded.last_seen, silent_reported_at = NULL", (caller.name, nows))
            rows = c.execute(
                f"SELECT m.* {self._pending_sql()} AND (m.lease_until IS NULL OR m.lease_until <= ?) "
                "ORDER BY m.fell_due_at, m.num LIMIT ?", (caller.name, nows, limit),
            ).fetchall()
            out = []
            lease = fmt(now + timedelta(seconds=st.settings.lease_seconds))
            for r in rows:
                late, text = r["late"], r["text"]
                if r["handed_at"] is None and late is None:
                    away = self._away(r, now, last_seen)
                    if away:
                        late = json.dumps(away)
                        text = text + f" (delivered late, due {self.outbox.local(r['fell_due_at'])})"
                c.execute("UPDATE messages SET handed_at = COALESCE(handed_at, ?), lease_until = ?, "
                          "attempt = attempt + 1, late = ?, text = ? WHERE num = ?",
                          (nows, lease, late, text, r["num"]))
                out.append(self._delivery(c, r["num"]))
            return out

    def _away(self, r, now: datetime, last_seen: str | None) -> dict | None:
        """Handed out first well after it fell due, because the deliverer wasn't fetching."""
        silent = timedelta(seconds=self.store.settings.silent_seconds)
        due = parse_ts(r["fell_due_at"])
        if now - due <= silent:
            return None
        return {"due_at": r["fell_due_at"], "reason": "recipient_away",
                "down_from": last_seen if last_seen and last_seen < r["fell_due_at"] else None,
                "down_until": fmt(now)}

    @staticmethod
    def _delivery(c, num: int) -> dict:
        r = c.execute("SELECT * FROM messages WHERE num = ?", (num,)).fetchone()
        return {
            "id": delivery_id(r["num"]),
            "recipient": r["recipient"],
            "kind": r["kind"],
            "item": item_id(r["item"]) if r["item"] is not None else None,
            "items": json.loads(r["items"]),
            "fell_due_at": r["fell_due_at"],
            "attempt": r["attempt"],
            "late": json.loads(r["late"]) if r["late"] else None,
            "text": r["text"],
            "summary": r["summary"],
            "links": json.loads(r["links"]),
        }

    def next_lease_end(self, caller: Client) -> datetime | None:
        """When the earliest lease of this deliverer's pending messages runs out."""
        with self.store.read() as c:
            row = c.execute(f"SELECT MIN(m.lease_until) {self._pending_sql()} AND m.lease_until IS NOT NULL",
                            (caller.name,)).fetchone()
        return parse_ts(row[0]) if row and row[0] else None

    async def long_poll(self, caller: Client, wait: float, limit: int, run) -> list[dict]:
        """Answer at once if anything is ready; else hold up to `wait` seconds for a signal or the
        end of a lease. `run` runs a blocking call off the event loop."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            entry = self.signal.register()
            try:
                got = await run(self.fetch, caller, limit)
                remaining = deadline - loop.time()
                if got or remaining <= 0:
                    return got
                lease_end = await run(self.next_lease_end, caller)
                if lease_end is not None:
                    until_lease = (lease_end - self.store.now()).total_seconds()
                    remaining = min(remaining, max(until_lease, 0.05))
                try:
                    await asyncio.wait_for(entry[1].wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass
            finally:
                self.signal.unregister(entry)

    # ------------------------------------------------------------------ confirmations
    def ack(self, caller: Client, did: str, body: dict) -> dict:
        if caller.role != "deliverer":
            raise ApiError("forbidden", "only deliverers confirm deliveries")
        schema.check("AckRequest", body)
        num = delivery_num(did)
        if body.get("detail") and content.problem(body["detail"]):
            raise ApiError("content", "detail: looks like it holds data that must not be stored", field="detail")
        st = self.store
        with st.tx() as c:
            r = c.execute("SELECT m.* FROM messages m JOIN clients r ON r.name = m.recipient "
                          "WHERE m.num = ? AND r.deliverer = ?", (num, caller.name)).fetchone()
            if r is None:
                raise ApiError("not_found", "no such delivery")
            if r["acked_at"] is None:
                now = fmt(st.now())
                c.execute("UPDATE messages SET acked_at = ?, ack_result = ?, ack_detail = ?, lease_until = NULL "
                          "WHERE num = ?", (now, body["result"], body.get("detail"), num))
                if body["result"] == "undeliverable" and r["withdrawn_at"] is None:
                    self._undeliverable(c, r, body.get("detail"))
                r = c.execute("SELECT * FROM messages WHERE num = ?", (num,)).fetchone()
            return {"id": delivery_id(num), "result": r["ack_result"], "acked_at": r["acked_at"]}

    def _undeliverable(self, c, r, detail: str | None) -> None:
        coordinator = self.store.coordinator()
        if not coordinator or r["recipient"] == coordinator:
            # telling the coordinator that the coordinator can't be reached goes nowhere
            log.warning("%s to %s undeliverable; not escalated", delivery_id(r["num"]), r["recipient"])
            return
        about = f" about {item_id(r['item'])}" if r["item"] is not None else ""
        why = f": {detail}" if detail else ""
        text = f"Could not deliver {delivery_id(r['num'])} ({r['kind']}{about}) to {r['recipient']}{why}."
        row = self.outbox.item_row(c, r["item"]) if r["item"] is not None else None
        self.outbox.add(c, coordinator, "undeliverable", row, text,
                        items=[item_id(r["item"])] if r["item"] is not None else [])

    # ------------------------------------------------------------------ withdrawal
    def _withdraw(self, c, where: str, args: tuple) -> None:
        c.execute(f"UPDATE messages SET withdrawn_at = ?, lease_until = NULL WHERE acked_at IS NULL "
                  f"AND withdrawn_at IS NULL AND {where}", (fmt(self.store.now()), *args))

    def on_client_change(self, c, num: int, owner_update: bool, changed: set[str] = frozenset()) -> None:
        """The owner changed the item, or someone closed it: its reminders and warnings are stale.
        A note from a coordinator or a human leaves the owner's reminders in place, but a new due
        time, whoever sets it, makes the late warnings about the old one stale."""
        if not owner_update:
            if "due_at" in changed:
                self._withdraw(c, "kind IN ('nudge', 'at_risk') AND item = ?", (num,))
                self._withdraw(c, "kind = 'at_risk' AND items = ?", (json.dumps([item_id(num)]),))
            return
        kinds = ",".join(f"'{k}'" for k in WITHDRAW_ON_CHANGE)
        self._withdraw(c, f"kind IN ({kinds}) AND item = ?", (num,))
        # warnings to downstream owners that this item is late
        self._withdraw(c, "kind = 'at_risk' AND items = ?", (json.dumps([item_id(num)]),))

    def on_transitions(self, c, transitions: list[Transition]) -> None:
        for t in transitions:
            if t.kind == "replan":
                num = int(t.item[2:])
                state = c.execute("SELECT state FROM items WHERE num = ?", (num,)).fetchone()[0]
                if state == "waiting":
                    self._withdraw(c, "kind = 'unblocked' AND item = ?", (num,))

    # ------------------------------------------------------------------ silent deliverers
    def check_silent(self, c, now: datetime) -> None:
        """Called by every scheduler pass: report deliverers silent for `silent_seconds`, once."""
        coordinator = self.store.coordinator()
        cutoff = fmt(now - timedelta(seconds=self.store.settings.silent_seconds))
        rows = c.execute(
            "SELECT cl.name, cl.created_at, d.last_seen, d.silent_reported_at FROM clients cl "
            "LEFT JOIN deliverers d ON d.name = cl.name WHERE cl.role = 'deliverer' AND cl.retired_at IS NULL "
            "AND EXISTS (SELECT 1 FROM clients x WHERE x.deliverer = cl.name AND x.retired_at IS NULL)"
        ).fetchall()
        for r in rows:
            last = r["last_seen"] or r["created_at"]
            if last > cutoff or r["silent_reported_at"]:
                continue
            c.execute("INSERT INTO deliverers (name, last_seen, silent_reported_at) VALUES (?, ?, ?) "
                      "ON CONFLICT(name) DO UPDATE SET silent_reported_at = excluded.silent_reported_at",
                      (r["name"], r["last_seen"], fmt(now)))
            if coordinator:
                since = f"since {self.outbox.local(r['last_seen'])}" if r["last_seen"] else "since it was registered"
                text = f"Deliverer {r['name']} has not fetched its messages {since}."
                self.outbox.add(c, coordinator, "deliverer_silent", None, text)
