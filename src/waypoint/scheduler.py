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

from . import recur
from .messages import Outbox
from .recurrences import Recurrences, recurrence_id
from .store import OPEN, WAYPOINT, Store, item_id
from .timeutil import fmt, parse_delay, parse_ts

log = logging.getLogger("waypoint.scheduler")

PRUNE_DAYS = 30
# A recurrence that takes check-ins gets its occurrence up to this long before its time (at most half
# the gap to the following one), so a job that starts on the minute and finishes in seconds finds it
# open: the job's timer and Waypoint's pass don't wait for each other. Only while none of its
# occurrences is open; otherwise at its time, as for every recurrence.
CHECKIN_LEAD = timedelta(minutes=5)


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
    occurrences: int = 0
    occurrences_skipped: int = 0
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
                self._recurrences(c, now, report)
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
        # when the due time was set: the creation or a change of it, whoever made it (Waypoint creates
        # occurrences of recurrences itself); only attending to it needs the owner
        for e in c.execute("SELECT id, at, event, changes FROM history WHERE item = ? AND event IN ('created', 'changed') "
                           "ORDER BY id", (num,)):
            if e["event"] == "created" or "due_at" in json.loads(e["changes"] or "{}"):
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

    # ------------------------------------------------------------------ recurring items
    def _recurrences(self, c, now: datetime, report: PassReport) -> None:
        """Each active recurrence whose next occurrence has come gets an ordinary item for its owner,
        as its overlap rule allows; occurrences missed while Waypoint was down are caught up as its
        catch_up rule says (docs/design/RECURRING.md)."""
        st = self.store
        rec = Recurrences.of(st)
        tz = st.settings.tz
        for r in c.execute("SELECT * FROM recurrences WHERE state = 'active' AND next_at IS NOT NULL "
                           "AND (next_at <= ? OR (checkin = 1 AND next_at <= ?)) ORDER BY next_at, num",
                           (fmt(now), fmt(now + CHECKIN_LEAD))).fetchall():
            # each recurrence on its own: a failure rolls back only that one, and the pass goes on
            c.execute("SAVEPOINT recurrence")
            try:
                upto = now + self._lead(r, tz) if r["checkin"] and not self._open(c, r["num"]) else now
                if parse_ts(r["next_at"]) <= upto:
                    self._recurrence(c, rec, r, now, upto, report, tz)
                c.execute("RELEASE recurrence")
            except Exception as exc:
                c.execute("ROLLBACK TO recurrence")
                c.execute("RELEASE recurrence")
                self._recurrence_failed(c, rec, r, exc)

    @staticmethod
    def _open(c, num: int) -> bool:
        """True if an occurrence of recurrence `num` is open. Then the next one is not made early: the
        running job's check-in must close its own occurrence, and `skip` would swallow an early next."""
        return c.execute("SELECT 1 FROM items WHERE recurrence = ? AND state IN ('next','running','waiting') "
                         "LIMIT 1", (num,)).fetchone() is not None

    @staticmethod
    def _lead(r, tz: str) -> timedelta:
        """How early a check-in recurrence's next occurrence is made: CHECKIN_LEAD, but at most half
        the gap to the one after it, so an early occurrence is never mistaken for the previous run's."""
        sched, at = json.loads(r["schedule"]), parse_ts(r["next_at"])
        gap = recur.next_after(sched, at, parse_ts(r["created_at"]), tz) - at
        return min(CHECKIN_LEAD, gap / 2)

    def _recurrence(self, c, rec, r, now: datetime, upto: datetime, report: PassReport, tz: str) -> None:
        """The occurrences of `r` that fell due up to `upto` (now, or a little later for check-ins)."""
        sched, created = json.loads(r["schedule"]), parse_ts(r["created_at"])
        first = last = parse_ts(r["next_at"])
        count = 1
        while True:  # count what fell due up to `upto`, keeping only the last
            t = recur.next_after(sched, last, created, tz)
            if t > upto:
                break
            last, count = t, count + 1
        down = report.outage is not None and first > parse_ts(report.outage[0])
        missed = count > 1 or down  # more than one fell due, or the one did while Waypoint was down
        why = "while Waypoint was down" if down else "at once"
        if missed and r["catch_up"] == "none":
            rec.history(c, r["num"], None, "skipped", None,
                        f"{count} occurrence(s) up to {self.outbox.local(fmt(last))} fell due {why}; not caught up")
            report.occurrences_skipped += count
        else:
            note = f"caught up: {count} occurrence(s) fell due {why}" if missed else None
            self._occur(c, rec, r, last, note, report, now)
        nxt = recur.next_after(sched, max(last, now), created, tz)
        c.execute("UPDATE recurrences SET last_at = ?, next_at = ? WHERE num = ?", (fmt(last), fmt(nxt), r["num"]))
        rec._changed(c, r["num"])

    def _recurrence_failed(self, c, rec, r, exc: Exception) -> None:
        """An occurrence couldn't be made (a bug or bad data): the recurrence pauses, its history says
        why (by type only), and the coordinator is told, so the rest of the pass is unaffected."""
        rid = recurrence_id(r["num"])
        log.error("recurrence %s failed: %s; paused", rid, type(exc).__name__)
        c.execute("UPDATE recurrences SET state = 'paused', next_at = NULL, version = version + 1, updated_by = NULL "
                  "WHERE num = ?", (r["num"],))
        rec.history(c, r["num"], None, "changed", {"state": [r["state"], "paused"]},
                    f"paused: creating its occurrence failed ({type(exc).__name__})")
        self.outbox.add(c, self.store.coordinator(), "recurring", None,
                        f"{rid} ({r['title']}) was paused: creating its occurrence failed ({type(exc).__name__}). "
                        "Check it, then resume it or stop it.")
        rec._changed(c, r["num"])

    def _occur(self, c, rec, r, at: datetime, note: str | None, report: PassReport, now: datetime) -> None:
        st = self.store
        rid, ats = recurrence_id(r["num"]), fmt(at)
        open_ = c.execute("SELECT num FROM items WHERE recurrence = ? AND state IN ('next','running','waiting') "
                          "ORDER BY occurrence_at, num", (r["num"],)).fetchall()
        if open_ and r["overlap"] == "skip":
            line = f"{rid} occurrence at {self.outbox.local(ats)} skipped: the previous one is still open"
            st._history(c, open_[0]["num"], None, "derived", None, line)
            rec.history(c, r["num"], None, "skipped", None, f"{self.outbox.local(ats)}: {item_id(open_[0]['num'])} still open")
            report.occurrences_skipped += 1
            return
        if open_ and r["overlap"] == "replace":
            for o in open_:
                st.patch_item(WAYPOINT, item_id(o["num"]),
                              {"state": "dropped", "note": f"superseded by the occurrence at {self.outbox.local(ats)}"})
        body = {"track": r["track"], "title": r["title"], "kind": r["kind"], "owner": r["owner"],
                "due_at": fmt(at + parse_delay(r["window_len"])), "overdue": json.loads(r["overdue"]),
                "links": json.loads(r["links"])}
        if r["label"]:
            body["label"] = r["label"]
        if note or r["note"]:
            body["note"] = (note or r["note"])[:200]
        made = st.create_item(WAYPOINT, body)
        num = int(made["id"][2:])
        c.execute("UPDATE items SET recurrence = ?, occurrence_at = ? WHERE num = ?", (r["num"], ats, num))
        st._history(c, num, None, "derived", None, f"occurrence of {rid} at {self.outbox.local(ats)}")
        rec.history(c, r["num"], None, "occurred", None, f"{made['id']} for {self.outbox.local(ats)}")
        row = self.outbox.item_row(c, num)
        self.outbox.add(c, r["owner"], "recurring", row,
                        f"{self.outbox.ref(row)} is due: the {self.outbox.local(ats)} occurrence of {rid}.",
                        fell_due_at=fmt(min(at, now)), late=self.outbox.late_for(ats))
        report.occurrences += 1

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
