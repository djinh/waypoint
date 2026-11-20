"""Recurrences: a template and a schedule that turn into ordinary items (docs/design/RECURRING.md).

This module keeps the recurrences themselves: creating, reading, changing, pausing, resuming and
stopping them, who may do which, and their history. The scheduler creates the occurrences.
"""

from __future__ import annotations

import json
import re

from . import content, recur, schema
from .errors import ApiError
from .store import WAYPOINT, WRITERS, Client, Store, item_id
from .timeutil import fmt, parse_ts

_RID = re.compile(r"R-([1-9][0-9]{0,17})")
TEMPLATE = ("owner", "track", "title", "label", "kind", "note", "links", "overdue")
SETTINGS = ("schedule", "window", "overlap", "catch_up", "checkin", "checkin_by")
CHECKIN_ROLES = ("agent", "human", "coordinator")


def recurrence_id(num: int) -> str:
    return f"R-{num}"


def recurrence_num(rid: str) -> int:
    m = _RID.fullmatch(rid) if isinstance(rid, str) else None
    if m is None:
        raise ApiError("not_found", "no such recurrence")
    return int(m.group(1))


class Recurrences:
    def __init__(self, store: Store):
        self.store = store
        store.retire_listeners.append(self.on_retire)
        self.on_change: list = []  # fn(c, num), after a recurrence changed (for the event stream)

    @classmethod
    def of(cls, store: Store) -> "Recurrences":
        if not hasattr(store, "_recurrences"):
            store._recurrences = cls(store)
        return store._recurrences

    # ------------------------------------------------------------------ reading
    def serialize(self, c, r) -> dict:
        oldest = c.execute("SELECT num FROM items WHERE recurrence = ? AND state IN ('next','running','waiting') "
                           "ORDER BY occurrence_at, num LIMIT 1", (r["num"],)).fetchone()
        return {
            "id": recurrence_id(r["num"]), "owner": r["owner"], "track": r["track"], "title": r["title"],
            "label": r["label"], "kind": r["kind"], "note": r["note"], "links": json.loads(r["links"]),
            "overdue": json.loads(r["overdue"]), "schedule": json.loads(r["schedule"]), "window": r["window_len"],
            "overlap": r["overlap"], "catch_up": r["catch_up"], "checkin": bool(r["checkin"]),
            "checkin_by": r["checkin_by"], "state": r["state"], "next_at": r["next_at"], "last_at": r["last_at"],
            "open_occurrence": item_id(oldest[0]) if oldest else None, "version": r["version"],
            "created_at": r["created_at"], "created_by": r["created_by"], "updated_at": r["updated_at"],
            "updated_by": r["updated_by"],
        }

    def _row(self, c, num: int):
        r = c.execute("SELECT * FROM recurrences WHERE num = ?", (num,)).fetchone()
        if r is None:
            raise ApiError("not_found", "no such recurrence")
        return r

    def get(self, rid: str, history: bool = False) -> dict:
        num = recurrence_num(rid)
        with self.store.read() as c:
            out = self.serialize(c, self._row(c, num))
            if history:
                out["history"] = [
                    {"at": h["at"], "by": h["by"], "event": h["event"],
                     "changes": json.loads(h["changes"]) if h["changes"] else None, "note": h["note"]}
                    for h in c.execute("SELECT * FROM recurrence_history WHERE recurrence = ? ORDER BY id", (num,))]
        return out

    def list(self, owner: str | None = None, track: str | None = None,
             states: tuple = ("active", "paused")) -> list[dict]:
        sql, args = "SELECT * FROM recurrences WHERE 1", []
        if owner:
            sql += " AND owner = ?"; args.append(owner)
        if track:
            sql += " AND track = ?"; args.append(track)
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"; args.extend(states)
        sql += " ORDER BY next_at IS NULL, next_at, num"
        with self.store.read() as c:
            return [self.serialize(c, r) for r in c.execute(sql, args)]

    # ------------------------------------------------------------------ who may do what
    def _may_assign(self, caller: Client, owner: str) -> None:
        """Whether CALLER may make OWNER the owner (on create, or when handing it over): a live agent,
        coordinator or human; a person only by that person or a coordinator."""
        if caller.role not in WRITERS:
            raise ApiError("forbidden", "this client may not write recurrences")
        target = self.store.client(owner)
        if target is None or target.role not in WRITERS:
            raise ApiError("invalid", "owner: no such agent, coordinator or human", field="owner")
        if target.role == "human" and caller.name != owner and caller.role != "coordinator":
            raise ApiError("forbidden", "a person's recurrences are created and changed only by that person "
                                        "or a coordinator")

    def _may_change(self, caller: Client, row) -> None:
        """Whether CALLER may change this recurrence. A person's: that person or a coordinator. One whose
        owner was retired: a coordinator or a human (to re-own, resume or stop it). Otherwise as for
        items: the owner, a coordinator or a human, or the agent that created it for another agent."""
        if caller.role not in WRITERS:
            raise ApiError("forbidden", "this client may not write recurrences")
        owner = self.store.client(row["owner"])
        if owner is None:
            if caller.role in ("coordinator", "human"):
                return
            raise ApiError("forbidden", "its owner was retired: a coordinator or a human may re-own or stop it")
        if owner.role == "human":
            if caller.name != owner.name and caller.role != "coordinator":
                raise ApiError("forbidden", "a person's recurrences are created and changed only by that person "
                                            "or a coordinator")
            return
        if caller.role in ("coordinator", "human") or caller.name == owner.name:
            return
        if caller.name == row["created_by"] and owner.role == "agent":
            return  # the creator of one it handed to another agent
        raise ApiError("forbidden", "only its owner, its creator, a coordinator or a human may change it")

    def _checkin_by(self, name: str | None) -> None:
        if name is None:
            return
        cl = self.store.client(name)
        if cl is None or cl.role not in CHECKIN_ROLES:
            raise ApiError("invalid", "checkin_by: no such agent, coordinator or human", field="checkin_by")

    # ------------------------------------------------------------------ writing
    def _template_columns(self, body: dict, kind: str) -> dict:
        """Content check, lengths (the schema did those), links and ladder, as for items."""
        fields = {k: body[k] for k in ("title", "label", "note", "links", "overdue") if k in body}
        out = self.store._validated_fields(fields, kind=kind)
        for k in ("title", "label", "note"):
            if k in fields:
                out[k] = fields[k]
        return out

    def create(self, caller: Client, body: dict) -> dict:
        st = self.store
        schema.check("RecurrenceCreate", body)
        owner = body.get("owner", caller.name)
        self._may_assign(caller, owner)
        recur.check(body["schedule"], st.settings.tz)
        self._checkin_by(body.get("checkin_by"))
        kind = body.get("kind", "task")
        cols = self._template_columns(body, kind)
        now = st.now()
        nows = fmt(now)
        nxt = recur.next_after(body["schedule"], now, now, st.settings.tz)
        with st.tx() as c:
            st._check_track(c, body["track"])
            cur = c.execute(
                "INSERT INTO recurrences (owner, track, title, label, kind, note, links, overdue, schedule, window_len, "
                "overlap, catch_up, checkin, checkin_by, state, next_at, created_at, created_by, updated_at, "
                "updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active', ?,?,?,?,?)",
                (owner, body["track"], body["title"], cols.get("label"), kind, cols.get("note"),
                 cols.get("links", "[]"), cols.get("overdue", "[]"), json.dumps(body["schedule"]),
                 body.get("window", "1h"), body.get("overlap", "skip"), body.get("catch_up", "latest"),
                 int(body.get("checkin", False)), body.get("checkin_by"), fmt(nxt), nows, caller.name, nows,
                 caller.name))
            num = cur.lastrowid
            self.history(c, num, caller.name, "created", None, None)
            self._changed(c, num)
            return self.serialize(c, self._row(c, num))

    def patch(self, caller: Client, rid: str, body: dict) -> dict:
        st = self.store
        schema.check("RecurrencePatch", body)
        num = recurrence_num(rid)
        with st.tx() as c:
            row = self._row(c, num)
            self._may_change(caller, row)
            if "owner" in body and body["owner"] != row["owner"]:
                self._may_assign(caller, body["owner"])
            if "if_version" in body and body["if_version"] != row["version"]:
                raise ApiError("version_conflict", "if_version: the recurrence changed meanwhile")
            if row["state"] == "stopped":
                raise ApiError("invalid", "state: a stopped recurrence is final", field="state")
            if "schedule" in body:
                recur.check(body["schedule"], st.settings.tz)
            if "checkin_by" in body:
                self._checkin_by(body["checkin_by"])
            if "track" in body:
                st._check_track(c, body["track"])
            cols = self._template_columns(body, row["kind"])
            sets: dict[str, object] = {}
            for k in ("owner", "track"):
                if k in body:
                    sets[k] = body[k]
            for k in ("title", "label", "note", "links", "overdue"):
                if k in cols:
                    sets[k] = cols[k]
            if "schedule" in body:
                sets["schedule"] = json.dumps(body["schedule"])
            if "window" in body:
                sets["window_len"] = body["window"]
            for k in ("overlap", "catch_up", "checkin_by"):
                if k in body:
                    sets[k] = body[k]
            if "checkin" in body:
                sets["checkin"] = int(body["checkin"])
            state = body.get("state", row["state"])
            if state != row["state"]:
                sets["state"] = state
            now = st.now()
            if state == "active" and (state != row["state"] or "schedule" in body):
                sched = body.get("schedule") or json.loads(row["schedule"])
                # from the next scheduled time after now: a resumed recurrence doesn't catch up
                sets["next_at"] = fmt(recur.next_after(sched, now, parse_ts(row["created_at"]), st.settings.tz))
            elif state != "active":
                sets["next_at"] = None
            changes = {k: [row[_col(k)], v] for k, v in sets.items()
                       if k != "next_at" and row[_col(k)] != v}
            if sets:
                new = {k: row[k] for k in _MUTABLE}
                new.update({_col(k): v for k, v in sets.items()})
                c.execute("UPDATE recurrences SET owner = ?, track = ?, title = ?, label = ?, note = ?, links = ?, "
                          "overdue = ?, schedule = ?, window_len = ?, overlap = ?, catch_up = ?, checkin = ?, "
                          "checkin_by = ?, state = ?, next_at = ?, version = ?, updated_at = ?, updated_by = ? "
                          "WHERE num = ?", (*(new[k] for k in _MUTABLE), row["version"] + 1, fmt(now),
                                            caller.name, num))
                self.history(c, num, caller.name, "changed", changes or None, None)
                self._changed(c, num)
            return self.serialize(c, self._row(c, num))

    # ------------------------------------------------------------------ check-ins
    def checkin(self, caller: Client, rid: str, body: dict) -> dict:
        """A job reports a run: `ok` closes the oldest open occurrence as done, `failed` drops it and
        tells the owner. Allowed for the check-in client, the owner, coordinators and humans."""
        st = self.store
        schema.check("CheckinRequest", body)
        num = recurrence_num(rid)
        note = body.get("note")
        if note and content.problem(note):
            raise ApiError("content", "note: looks like it holds data that must not be stored", field="note")
        with st.tx() as c:
            row = self._row(c, num)
            if not (caller.name in (row["checkin_by"], row["owner"]) or caller.role in ("coordinator", "human")):
                raise ApiError("forbidden", "only its check-in client, its owner, a coordinator or a human may check in")
            if not row["checkin"]:
                raise ApiError("invalid", "checkin: this recurrence takes no check-ins", field="checkin")
            oldest = c.execute("SELECT num FROM items WHERE recurrence = ? AND state IN ('next','running','waiting') "
                               "ORDER BY occurrence_at, num LIMIT 1", (num,)).fetchone()
            result = body["result"]
            if oldest is None:
                self.history(c, num, caller.name, "checkin", None, f"{result} with no open occurrence"
                             + (f": {note}" if note else ""))
                return {"recurrence": self.serialize(c, row), "occurrence": None}
            iid = item_id(oldest[0])
            text = note or ("run reported ok" if result == "ok" else "run reported failed")
            st._history(c, oldest[0], caller.name, "derived", None, f"check-in by {caller.name}: {result}")
            got = st.patch_item(WAYPOINT, iid, {"state": "done" if result == "ok" else "dropped", "note": text})
            if result == "failed":
                from .messages import Outbox  # the outbox imports the store
                ob = Outbox.of(st)
                item_row = ob.item_row(c, oldest[0])
                ob.add(c, row["owner"], "recurring", item_row,
                       f"{ob.ref(item_row)}: the run of {recurrence_id(num)} failed: {text}")
            self.history(c, num, caller.name, "checkin", None, f"{result}: {iid}" + (f": {note}" if note else ""))
            self._changed(c, num)
            return {"recurrence": self.serialize(c, self._row(c, num)), "occurrence": got}

    # ------------------------------------------------------------------ retirement
    def on_retire(self, c, name: str) -> None:
        """A retired owner's recurrences pause (and the coordinator is told); a retired check-in
        client is cleared."""
        st = self.store
        nows = fmt(st.now())
        for r in c.execute("SELECT * FROM recurrences WHERE owner = ? AND state = 'active'", (name,)).fetchall():
            c.execute("UPDATE recurrences SET state = 'paused', next_at = NULL, version = version + 1, "
                      "updated_at = ?, updated_by = NULL WHERE num = ?", (nows, r["num"]))
            note = f"paused: its owner {name} was retired"
            self.history(c, r["num"], None, "changed", {"state": [r["state"], "paused"]}, note)
            self._tell_coordinator(c, f"{recurrence_id(r['num'])} ({r['title']}) was {note}; give it a new "
                                      f"owner and resume it, or stop it.")
            self._changed(c, r["num"])
        for r in c.execute("SELECT * FROM recurrences WHERE checkin_by = ? AND state != 'stopped'", (name,)).fetchall():
            c.execute("UPDATE recurrences SET checkin_by = NULL, version = version + 1, updated_at = ?, "
                      "updated_by = NULL WHERE num = ?", (nows, r["num"]))
            self.history(c, r["num"], None, "changed", {"checkin_by": [name, None]},
                         f"check-in client {name} was retired")
            self._changed(c, r["num"])

    def _tell_coordinator(self, c, text: str) -> None:
        from .messages import Outbox  # the outbox imports the store; imported here to avoid a cycle
        Outbox.of(self.store).add(c, self.store.coordinator(), "recurring", None, text)

    # ------------------------------------------------------------------ history
    def history(self, c, num: int, by: str | None, event: str, changes: dict | None, note: str | None) -> None:
        c.execute("INSERT INTO recurrence_history (recurrence, at, by, event, changes, note) VALUES (?,?,?,?,?,?)",
                  (num, fmt(self.store.now()), by, event, json.dumps(changes) if changes else None, note))

    def _changed(self, c, num: int) -> None:
        self.store.touched_recurrences.add(num)
        for fn in self.on_change:
            fn(c, num)


_MUTABLE = ("owner", "track", "title", "label", "note", "links", "overdue", "schedule", "window_len", "overlap",
            "catch_up", "checkin", "checkin_by", "state", "next_at")


def _col(field: str) -> str:
    return "window_len" if field == "window" else field
