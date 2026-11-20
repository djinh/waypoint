"""The scheduler: every pass re-derives what time changes, sends wake-ups and climbs overdue ladders.

One pass is one transaction. It records its own time, so the next pass knows whether Waypoint was
down in between (a gap longer than ``Settings.outage_seconds``). The ladder runs on the clock:
after an outage an item goes straight to the furthest step it would have reached by now, and only
that step runs, once.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from starlette.concurrency import run_in_threadpool

from .messages import Outbox
from .store import OPEN, Store, item_id
from .timeutil import fmt, parse_delay, parse_ts

log = logging.getLogger("waypoint.scheduler")

PRUNE_DAYS = 30


@dataclass
class PassReport:
    at: str
    outage: tuple[str, str] | None = None
    wakes_sent: int = 0
    wakes_late: int = 0
    wakes_dropped: int = 0
    steps: int = 0
    steps_caught_up: int = 0
    running_checks: int = 0
    running_lapsed: int = 0
    messages: list[int] = field(default_factory=list)


class Scheduler:
    def __init__(self, store: Store):
        self.store = store
        self.outbox = Outbox.of(store)
        # Called after every pass that created messages.
        self.listeners: list = []
        # Called as fn(conn, now) inside every pass's transaction (deliveries checks for silent deliverers).
        self.pass_hooks: list = []

    # ------------------------------------------------------------------ one pass
    def run_pass(self) -> PassReport:
        st = self.store
        with st.tx() as c:
            now = st.now()
            nows = fmt(now)
            first_msg = c.execute("SELECT COALESCE(MAX(num), 0) FROM messages").fetchone()[0]
            last = c.execute("SELECT MAX(at) FROM passes").fetchone()[0]
            report = PassReport(at=nows)
            if last is not None and now - parse_ts(last) > timedelta(seconds=st.settings.outage_seconds):
                self.outbox.outage = (parse_ts(last), now)
                report.outage = (last, nows)
            c.execute("INSERT INTO passes (at) VALUES (?)", (nows,))
            c.execute("DELETE FROM passes WHERE at < ?", (fmt(now - timedelta(days=7)),))
            # settled messages are kept for a month, then pruned (the item's history keeps a line)
            c.execute("DELETE FROM messages WHERE created_at < ? AND (acked_at IS NOT NULL OR withdrawn_at IS NOT NULL)",
                      (fmt(now - timedelta(days=PRUNE_DAYS)),))
            try:
                st.recompute(c)
                self._wakes(c, now, report)
                self._ladders(c, now, report)
                self._running(c, now, report)
                for fn in self.pass_hooks:
                    fn(c, now)
                if report.outage:
                    self._outage_summary(c, report)
            finally:
                self.outbox.outage = None
            report.messages = [r[0] for r in c.execute("SELECT num FROM messages WHERE num > ?", (first_msg,))]
        if report.messages:
            for fn in self.listeners:
                fn()
        return report

    # ------------------------------------------------------------------ wake-ups
    def _wakes(self, c, now: datetime, report: PassReport) -> None:
        rows = c.execute(
            "SELECT * FROM items WHERE wake_at IS NOT NULL AND wake_at <= ? AND NOT EXISTS "
            "(SELECT 1 FROM wakes w WHERE w.item = items.num AND w.wake_at = items.wake_at) ORDER BY wake_at, num",
            (fmt(now),),
        ).fetchall()
        for row in rows:
            outcome = "sent"
            late = self.outbox.late_for(row["wake_at"])
            if row["state"] not in OPEN:
                outcome = "cancelled"  # a done item's wake-ups are cancelled
            elif late and self._owner_changed_since(c, row["num"], row["wake_at"]):
                outcome = "dropped"  # missed during the outage, and the item changed meanwhile
                report.wakes_dropped += 1
            else:
                note = f": {row['note']}" if row["note"] else ""
                text = f"Reminder: {self.outbox.ref(row)}{note}{self.outbox.late_note(row['wake_at'], late)}"
                self.outbox.add(c, row["owner"], "wake", row, text, fell_due_at=row["wake_at"], late=late)
                report.wakes_sent += 1
                report.wakes_late += bool(late)
            c.execute("INSERT INTO wakes (item, wake_at, outcome, at) VALUES (?,?,?,?)",
                      (row["num"], row["wake_at"], outcome, fmt(now)))

    @staticmethod
    def _owner_changed_since(c, num: int, since: str) -> bool:
        return any(e["by"] == e["owner"] and e["at"] >= since for e in Store.client_events(c, num))

    # ------------------------------------------------------------------ the overdue ladder
    @staticmethod
    def _ladder_clock(c, num: int, due_at: str) -> tuple[datetime, bool, datetime | None]:
        """When this item's ladder starts, whether its owner has attended to it since, and when a
        lapsed running lease re-armed it (None if none did).

        The ladder starts at max(due time, when that due time was set, by anyone), so an item
        created or re-dated with a due time already past starts at its first step. It is attended
        when the item's owner (who owned it then) changed it at or after the due time, after the
        change that set the due time. Updates by a coordinator or a human don't stop the ladder.
        A running lease that lapsed (the owner went silent) re-arms it: only owner changes after
        the last lapse count as attending, and the steps not taken yet are counted from the lapse
        (no catch-up of those that fell due while the item ran)."""
        events = Store.client_events(c, num)
        set_id, set_at = 0, due_at
        for e in events:
            if e["event"] == "created" or "due_at" in e["changes"]:
                set_id, set_at = e["id"], e["at"]
        lapse = c.execute("SELECT id, at FROM history WHERE item = ? AND by IS NULL AND event = 'derived' "
                          "AND note LIKE 'running lapsed%' AND id > ? ORDER BY id DESC LIMIT 1",
                          (num, set_id)).fetchone()
        since_id = max(set_id, lapse["id"]) if lapse else set_id
        rearmed = parse_ts(lapse["at"]) if lapse and lapse["at"] >= due_at else None
        start = max(parse_ts(due_at), parse_ts(set_at))
        if rearmed is not None:
            start = max(start, rearmed)
        attended = any(e["id"] > since_id and e["at"] >= due_at and e["by"] == e["owner"] for e in events)
        return start, attended, rearmed

    def _ladders(self, c, now: datetime, report: PassReport) -> None:
        rows = c.execute(
            "SELECT * FROM items WHERE due_at IS NOT NULL AND due_at <= ? AND state IN ('next','running','waiting') "
            "ORDER BY due_at, num", (fmt(now),),
        ).fetchall()
        for row in rows:
            start, attended, rearmed = self._ladder_clock(c, row["num"], row["due_at"])
            if attended:
                continue
            ladder = json.loads(row["overdue"]) or self.store.settings.default_ladder
            taken = {r[0] for r in c.execute("SELECT step FROM ladder_steps WHERE item = ? AND due_at = ?",
                                             (row["num"], row["due_at"]))}
            reached = [(i, s, start + parse_delay(s["after"])) for i, s in enumerate(ladder)
                       if i not in taken and start + parse_delay(s["after"]) <= now]
            if not reached:
                continue
            if report.outage:
                # catching up after Waypoint's own downtime: only the furthest step reached, once
                furthest = max(t for _, _, t in reached)
                skipped = [x for x in reached if x[2] < furthest]
                run = [x for x in reached if x[2] == furthest]
                late = self.outbox.late_for(fmt(furthest))
            else:
                skipped, run, late = [], reached, None
            caught_up = bool(skipped) or late is not None
            for i, step, at in skipped:
                c.execute("INSERT INTO ladder_steps (item, due_at, step, action, target, at, caught_up, skipped) "
                          "VALUES (?,?,?,?,?,?,1,1)", (row["num"], row["due_at"], i, step["action"], step.get("to"),
                                                       fmt(now)))
            for i, step, at in run:
                c.execute("INSERT INTO ladder_steps (item, due_at, step, action, target, at, caught_up, skipped) "
                          "VALUES (?,?,?,?,?,?,?,0)", (row["num"], row["due_at"], i, step["action"], step.get("to"),
                                                       fmt(now), int(caught_up)))
                since = "after the lapse" if rearmed is not None and start == rearmed else "after due"
                note = f"{step['action']} (step {i + 1}, {step['after']} {since})"
                if caught_up:
                    note += ", caught up"
                self.store._history(c, row["num"], None, "ladder_step", None, note)
                self._act(c, row, step, fmt(at), late)
                report.steps += 1
                report.steps_caught_up += caught_up

    def _act(self, c, row, step: dict, at: str, late: dict | None) -> None:
        ob = self.outbox
        due = f"due {ob.local(row['due_at'])}"
        tail = ob.late_note(at, late)
        action = step["action"]
        if action == "nudge":
            ob.add(c, row["owner"], "nudge", row, f"{ob.ref(row)} is late: {due}.{tail}", fell_due_at=at, late=late)
        elif action == "escalate":
            text = f"{ob.ref(row)}, owned by {row['owner']}, is late: {due}. Please chase or re-plan.{tail}"
            coordinator = self.store.coordinator()
            if coordinator is None:
                log.warning("escalation for %s not sent: no coordinator", item_id(row["num"]))
            ob.add(c, coordinator, "escalate", row, text, fell_due_at=at, late=late)
        elif action == "notify":
            # push channels may be public: the title and the owner only
            ob.add(c, step["to"], "notify", row, f"{row['title']} ({row['owner']}) is late", fell_due_at=at, late=late)
        elif action == "flag":
            for dep in self._downstream(c, row["num"]):
                text = (f"{ob.ref(row)} is late ({due}) and flagged; {ob.ref(dep)}, which waits on it, "
                        f"is at risk.{tail}")
                ob.add(c, dep["owner"], "at_risk", dep, text, items=[item_id(row["num"])], fell_due_at=at, late=late)

    @staticmethod
    def _downstream(c, num: int) -> list:
        """Open items that wait on this one, directly or further on."""
        out, seen, todo = [], {num}, [num]
        while todo:
            cur = todo.pop(0)
            for r in c.execute("SELECT DISTINCT i.* FROM conditions k JOIN items i ON i.num = k.item "
                               "WHERE k.dep = ? AND i.state IN ('next','running','waiting') ORDER BY i.num", (cur,)):
                if r["num"] not in seen:
                    seen.add(r["num"])
                    out.append(r)
                    todo.append(r["num"])
        return out

    # ------------------------------------------------------------------ the running lease
    def _running(self, c, now: datetime, report: PassReport) -> None:
        """A running item whose lease passed gets one running_check to its owner; once that check is
        confirmed, the owner has the grace period to update the item, or it lapses back to next."""
        st = self.store
        nows = fmt(now)
        # items running from before leases existed (an upgrade) get one now
        c.execute("UPDATE items SET running_until = ? WHERE state = 'running' AND running_until IS NULL",
                  (fmt(now + timedelta(seconds=st.settings.running_lease_seconds)),))
        if report.outage:
            # Waypoint being down pauses the grace clock
            down_from, down_until = (parse_ts(x) for x in report.outage)
            for r in c.execute("SELECT num, running_lapse_at FROM items WHERE state = 'running' "
                               "AND running_lapse_at IS NOT NULL AND running_lapse_at > ?", (fmt(down_from),)).fetchall():
                c.execute("UPDATE items SET running_lapse_at = ? WHERE num = ?",
                          (fmt(parse_ts(r["running_lapse_at"]) + (down_until - down_from)), r["num"]))
        # 1. the check
        for row in c.execute("SELECT * FROM items WHERE state = 'running' AND running_check IS NULL "
                             "AND running_until <= ? ORDER BY running_until, num", (nows,)).fetchall():
            late = self.outbox.late_for(row["running_until"])
            text = (f"{self.outbox.ref(row)} was expected to finish running at "
                    f"{self.outbox.local(row['running_until'])}. Still running? Extend it (any update), "
                    f"mark it done, or set next or waiting.{self.outbox.late_note(row['running_until'], late)}")
            msg = self.outbox.add(c, row["owner"], "running_check", row, text, fell_due_at=row["running_until"],
                                  late=late)
            # no message (nobody to tell): the grace runs from now
            c.execute("UPDATE items SET running_check = ?, running_lapse_at = ? WHERE num = ?",
                      (msg or 0, None if msg else fmt(now + timedelta(seconds=st.settings.running_grace_seconds)),
                       row["num"]))
            st._history(c, row["num"], None, "derived", {"running_overdue": [False, True]}, None)
            report.running_checks += 1
        # 2. the grace starts when the check was confirmed (delivered, or given up on), not when sent
        for r in c.execute("SELECT i.num, m.acked_at FROM items i JOIN messages m ON m.num = i.running_check "
                           "WHERE i.state = 'running' AND i.running_lapse_at IS NULL AND m.acked_at IS NOT NULL"
                           ).fetchall():
            c.execute("UPDATE items SET running_lapse_at = ? WHERE num = ?",
                      (fmt(parse_ts(r["acked_at"]) + timedelta(seconds=st.settings.running_grace_seconds)), r["num"]))
        # 3. the lapse
        for row in c.execute("SELECT * FROM items WHERE state = 'running' AND running_lapse_at IS NOT NULL "
                             "AND running_lapse_at <= ? ORDER BY num", (nows,)).fetchall():
            target = "next" if st._all_met(c, row["num"], now) else "waiting"
            note = f"running lapsed at {self.outbox.local(nows)}, no update"
            c.execute("UPDATE items SET state = ?, state_since = ?, note = ?, running_until = NULL, "
                      "running_check = NULL, running_lapse_at = NULL, version = version + 1, updated_at = ?, "
                      "updated_by = NULL WHERE num = ?", (target, nows, note, nows, row["num"]))
            st._history(c, row["num"], None, "derived", {"state": ["running", target], "note": [row["note"], note]},
                        note)
            report.running_lapsed += 1

    # ------------------------------------------------------------------ outage summary
    def _outage_summary(self, c, report: PassReport) -> None:
        down_from, down_until = (parse_ts(x) for x in report.outage)
        minutes = int((down_until - down_from).total_seconds() // 60)
        length = f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"
        text = (f"Waypoint was down {self.outbox.span(down_from, down_until)} ({length}). Caught up: "
                f"{report.wakes_late} wake-up(s) sent late, {report.wakes_dropped} dropped because the item "
                f"changed, {report.steps_caught_up} overdue step(s) taken late.")
        if self.outbox.add(c, self.store.coordinator(), "outage", None, text,
                           summary=f"Waypoint was down ({length})") is None:
            log.warning("outage of %s, but no coordinator to tell", length)

    # ------------------------------------------------------------------ the loop
    async def run_forever(self, first_delay: float = 0) -> None:
        interval = self.store.settings.pass_seconds
        await asyncio.sleep(first_delay)
        while True:
            try:
                await run_in_threadpool(self.run_pass)
            except Exception as exc:
                # the type only: an exception's message may quote item text
                log.error("scheduler pass failed: %s", type(exc).__name__)
            await asyncio.sleep(interval)
