"""The store: clients, tracks and items in SQLite, and everything Waypoint derives from them.

All writes go through Store methods, each in one transaction (BEGIN IMMEDIATE) that also
re-derives states and flags, so the picture can't contradict itself. The scheduler calls
``tick()`` for what changes with time alone (time conditions, lateness).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import content, schema
from .clock import Clock
from .errors import ApiError
from .timeutil import fmt, norm_ts, parse_day, parse_delay, parse_ts

OPEN = ("next", "running", "waiting")
CLOSED = ("done", "dropped")
WRITERS = ("agent", "human", "coordinator")
ALL_ROLES = ("agent", "human", "coordinator", "reader", "deliverer")
ANSWERED = ("decision", "approval")  # kinds a person answers; they have answer_at
# A closing note by the asker must quote the answer and say where it was given.
_QUOTED = re.compile(r'"[^"]*\S[^"]*"|“[^”]*\S[^”]*”')
_WHERE = re.compile(r"\b(?:session|issue|pull request|PR)\b|https?://|\b[\w.-]+/[\w.-]+#\d+", re.I)
# An issue or pull request at a forge, as a link: a question that links one must say where it is answered.
_ISSUE_LINK = re.compile(r"^https://[^\s/]+/[^\s]+/(issues|pull|pulls|merge_requests)/[0-9]+(?:[#?]\S*)?$")
# Fields Waypoint keeps itself; a client's "changed" history event never lists them.
DERIVED_FIELDS = ("version", "updated_at", "updated_by", "state_since", "waited_on_by", "late", "at_risk",
                  "at_risk_from", "overdue_steps", "done_at", "running_until", "running_overdue")


@dataclass(frozen=True)
class Client:
    name: str
    role: str
    label: str | None = None


@dataclass
class Settings:
    tz: str = "UTC"
    done_days: int = 3
    # The client told about escalations and outages; None: the first client with role coordinator.
    coordinator: str | None = None
    pass_seconds: int = 15
    # A gap between scheduler passes longer than this is an outage (Waypoint was down).
    outage_seconds: int = 60
    # An unconfirmed message is handed out again after this long.
    lease_seconds: int = 120
    # A deliverer that hasn't fetched for this long is reported to the coordinator.
    silent_seconds: int = 300
    # "running" is a lease: it lasts this long unless the owner renews it (any update) ...
    running_lease_seconds: int = 7200
    # ... and lapses back to next this long after the owner got the check, without an update.
    running_grace_seconds: int = 3600
    default_ladder: list[dict] = field(
        default_factory=lambda: [
            {"after": "0m", "action": "nudge", "to": None},
            {"after": "1h", "action": "escalate", "to": None},
            {"after": "1h", "action": "flag", "to": None},
        ]
    )


@dataclass
class Transition:
    """A derived change that someone may need to hear about (the deliveries part acts on these)."""

    kind: str  # "unblocked" | "replan" | "late" | "at_risk"
    item: str
    owner: str
    because: list[str] = field(default_factory=list)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ts_field(value: str | None, name: str) -> str | None:
    """A request timestamp in stored form; an impossible date is the request's fault, not ours."""
    try:
        return norm_ts(value)
    except ValueError:
        raise ApiError("invalid", f"{name}: not a valid time", field=name) from None


def item_id(num: int) -> str:
    return f"W-{num}"


def item_num(iid: str) -> int:
    if not isinstance(iid, str) or not iid.startswith("W-") or not iid[2:].isdigit():
        raise ApiError("not_found", "no such item")
    return int(iid[2:])


class Store:
    def __init__(self, conn: sqlite3.Connection, clock: Clock | None = None, settings: Settings | None = None):
        self.conn = conn
        self.clock = clock or Clock()
        self.settings = settings or Settings()
        self._lock = threading.RLock()
        self.outbox = None  # set by messages.Outbox.of()
        self.actor: str | None = None
        self.deliveries = None  # set by deliveries.Deliveries.of()
        self.broadcaster = None  # set by events.Broadcaster.of()
        # Called as fn(conn, transitions) inside the write transaction after every recompute,
        # so whatever a transition causes (a wake-up) commits or rolls back with it.
        self.listeners: list = []
        # Called as fn(conn, num, owner_update, changed) inside the transaction of every client write
        # to an item; owner_update is True when the item's owner made it, or it made the item done or
        # dropped; changed is the set of fields the write changed.
        self.change_listeners: list = []
        # Called as fn(item_nums, track_ids) after a transaction that changed them has committed.
        self.commit_listeners: list = []
        self._touched_items: set[int] = set()
        self._touched_tracks: set[str] = set()

    # ------------------------------------------------------------------ transactions
    @contextmanager
    def tx(self):
        """One write transaction; nested calls join the outer one."""
        with self._lock:
            if self.conn.in_transaction:
                yield self.conn
                return
            self.conn.execute("BEGIN IMMEDIATE")
            self._touched_items, self._touched_tracks = set(), set()
            # the client whose write this transaction is (None for Waypoint's own, e.g. a scheduler pass)
            self.actor = None
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            items, tracks = self._touched_items, self._touched_tracks
            if items or tracks:
                for fn in self.commit_listeners:
                    try:
                        fn(items, tracks)
                    except Exception:  # a listener must never undo or fail a committed write
                        pass

    @contextmanager
    def read(self):
        with self._lock:
            yield self.conn

    def now(self) -> datetime:
        return self.clock.now()

    # ------------------------------------------------------------------ clients
    def add_client(self, name: str, role: str, label: str | None = None, deliverer: str | None = None) -> str:
        """Register a client; returns its token (shown once, stored only as a hash)."""
        if role not in ALL_ROLES:
            raise ApiError("invalid", "role: not valid", field="role")
        schema.check("ClientName", name)
        token = secrets.token_urlsafe(32)
        with self.tx() as c:
            if c.execute("SELECT 1 FROM clients WHERE name = ?", (name,)).fetchone():
                raise ApiError("invalid", "name: already registered", field="name")
            c.execute(
                "INSERT INTO clients (name, role, label, token_hash, deliverer, created_at) VALUES (?,?,?,?,?,?)",
                (name, role, label, hash_token(token), deliverer, fmt(self.now())),
            )
        return token

    def rotate_token(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.tx() as c:
            cur = c.execute("UPDATE clients SET token_hash = ? WHERE name = ? AND retired_at IS NULL",
                            (hash_token(token), name))
            if cur.rowcount != 1:
                raise ApiError("not_found", "no such client")
        return token

    def route(self, name: str, deliverer: str | None) -> None:
        """Register a recipient with the deliverer that reaches it (None: no deliverer)."""
        with self.tx() as c:
            if deliverer is not None:
                row = c.execute("SELECT role FROM clients WHERE name = ? AND retired_at IS NULL",
                                (deliverer,)).fetchone()
                if row is None or row["role"] != "deliverer":
                    raise ApiError("invalid", "deliverer: no such deliverer", field="deliverer")
            cur = c.execute("UPDATE clients SET deliverer = ? WHERE name = ? AND retired_at IS NULL",
                            (deliverer, name))
            if cur.rowcount != 1:
                raise ApiError("not_found", "no such client")
            if deliverer is None:
                self._reroute_to_coordinator(c, name)

    def _reroute_to_coordinator(self, c, name: str) -> None:
        """Messages waiting for a recipient nobody can reach any more go to the coordinator."""
        coordinator = self.coordinator()
        if not coordinator or coordinator == name or self.deliverer_of(coordinator) is None:
            return
        c.execute("UPDATE messages SET recipient = ?, text = substr(? || text, 1, 1000), lease_until = NULL "
                  "WHERE recipient = ? "
                  "AND acked_at IS NULL AND withdrawn_at IS NULL",
                  (coordinator, f"For {name} (no deliverer registered): ", name))

    def deliverer_of(self, name: str | None) -> str | None:
        if not name:
            return None
        with self.read() as c:
            row = c.execute("SELECT deliverer FROM clients WHERE name = ? AND retired_at IS NULL", (name,)).fetchone()
        return row["deliverer"] if row else None

    # -- /v1/clients (coordinators and humans)
    ADMINS = ("coordinator", "human")

    def client_record(self, name: str) -> dict:
        with self.read() as c:
            r = c.execute("SELECT cl.*, d.last_seen FROM clients cl LEFT JOIN deliverers d ON d.name = cl.name "
                          "WHERE cl.name = ?", (name,)).fetchone()
        if r is None:
            raise ApiError("not_found", "no such client")
        return {"name": r["name"], "role": r["role"], "label": r["label"], "deliverer": r["deliverer"],
                "created_at": r["created_at"], "retired_at": r["retired_at"],
                "last_seen": r["last_seen"] if r["role"] == "deliverer" else None}

    def list_clients(self, caller: Client) -> list[dict]:
        self._admin(caller)
        with self.read() as c:
            names = [r[0] for r in c.execute("SELECT name FROM clients ORDER BY name")]
        return [self.client_record(n) for n in names]

    def _admin(self, caller: Client, target_role: str | None = None) -> None:
        """Coordinators and humans manage clients; human and coordinator clients only a human
        (design owner, 2026-11-14): no create, rotate, retire, re-route or label change by a coordinator."""
        if caller.role not in self.ADMINS:
            raise ApiError("forbidden", "only coordinators and humans manage clients")
        if target_role in ("human", "coordinator") and caller.role != "human":
            raise ApiError("forbidden", "only a human manages human and coordinator clients")

    def register(self, caller: Client, body: dict) -> dict:
        self._admin(caller)
        schema.check("ClientCreate", body)
        self._admin(caller, body["role"])
        if body.get("label") and content.problem(body["label"]):
            raise ApiError("content", "label: looks like it holds data that must not be stored", field="label")
        with self.tx():
            if body.get("deliverer"):
                d = self.client(body["deliverer"])
                if d is None or d.role != "deliverer":
                    raise ApiError("invalid", "deliverer: no such deliverer", field="deliverer")
            token = self.add_client(body["name"], body["role"], label=body.get("label"),
                                    deliverer=body.get("deliverer"))
            return {"client": self.client_record(body["name"]), "token": token}

    def rotate(self, caller: Client, name: str) -> dict:
        self._admin(caller)
        self._admin(caller, self.client_record(name)["role"])
        token = self.rotate_token(name)
        return {"client": self.client_record(name), "token": token}

    def change_client(self, caller: Client, name: str, body: dict) -> dict:
        self._admin(caller)
        schema.check("ClientPatch", body)
        if body.get("label") and content.problem(body["label"]):
            raise ApiError("content", "label: looks like it holds data that must not be stored", field="label")
        with self.tx() as c:
            rec = self.client_record(name)
            if rec["retired_at"]:
                raise ApiError("not_found", "no such client")
            self._admin(caller, rec["role"])
            if "label" in body:
                c.execute("UPDATE clients SET label = ? WHERE name = ?", (body["label"], name))
            if "deliverer" in body:
                self.route(name, body["deliverer"])
            if body.get("retired"):
                if c.execute("SELECT 1 FROM items WHERE owner = ? AND state IN ('next','running','waiting')",
                             (name,)).fetchone():
                    raise ApiError("invalid", "retired: the client still owns open items; hand them over or "
                                              "close them first", field="retired")
                if c.execute("SELECT 1 FROM clients WHERE deliverer = ? AND retired_at IS NULL",
                             (name,)).fetchone():
                    raise ApiError("invalid", "retired: recipients are still registered with this deliverer",
                                   field="retired")
                self._reroute_to_coordinator(c, name)
                self.retire_client(name)
            return self.client_record(name)

    def retire_client(self, name: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE clients SET retired_at = ?, token_hash = NULL WHERE name = ?", (fmt(self.now()), name))

    def authenticate(self, token: str | None) -> Client | None:
        if not token:
            return None
        wanted = hash_token(token)
        with self.read() as c:
            row = c.execute(
                "SELECT name, role, label, token_hash FROM clients WHERE token_hash = ? AND retired_at IS NULL",
                (wanted,),
            ).fetchone()
        if row is None or not hmac.compare_digest(row["token_hash"], wanted):
            return None
        return Client(row["name"], row["role"], row["label"])

    def client(self, name: str) -> Client | None:
        with self.read() as c:
            row = c.execute("SELECT name, role, label FROM clients WHERE name = ? AND retired_at IS NULL",
                            (name,)).fetchone()
        return Client(row["name"], row["role"], row["label"]) if row else None

    def owners(self, names) -> dict:
        """Name -> {role, label} for the views; names without a client are role 'unknown'."""
        names = sorted(set(n for n in names if n))
        out = {}
        with self.read() as c:
            for n in names:
                row = c.execute("SELECT role, label FROM clients WHERE name = ? AND retired_at IS NULL",
                                (n,)).fetchone()
                out[n] = {"role": row["role"], "label": row["label"] or n} if row else {"role": "unknown", "label": n}
        return out

    # ------------------------------------------------------------------ tracks
    def tracks(self, archived: bool = False, ids=None) -> list[dict]:
        with self.read() as c:
            rows = c.execute("SELECT * FROM tracks ORDER BY ord, id").fetchall()
        out = [self._track(r) for r in rows]
        if not archived:
            out = [t for t in out if not t["archived"]]
        if ids is not None:
            out = [t for t in out if t["id"] in ids]
        return out

    @staticmethod
    def _track(r) -> dict:
        return {"id": r["id"], "title": r["title"], "lead": r["lead"], "note": r["note"],
                "order": r["ord"], "archived": bool(r["archived"])}

    def put_track(self, caller: Client, tid: str, body: dict) -> dict:
        schema.check("TrackId", tid)
        schema.check("TrackPut", body)
        if caller.role not in WRITERS:
            raise ApiError("forbidden", "this client may not write tracks")
        bad = content.check_fields({"title": body.get("title"), "note": body.get("note")})
        if bad:
            raise ApiError("content", f"{bad}: looks like it holds data that must not be stored", field=bad)
        lead = body.get("lead")
        if lead is not None and self.client(lead) is None:
            raise ApiError("invalid", "lead: no such client", field="lead")
        with self.tx() as c:
            row = c.execute("SELECT * FROM tracks WHERE id = ?", (tid,)).fetchone()
            if row is not None and caller.role == "agent" and row["lead"] != caller.name:
                raise ApiError("forbidden", "only the track's lead, a coordinator or a human may change it")
            c.execute(
                "INSERT INTO tracks (id, title, lead, note, ord, archived) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, lead=excluded.lead, note=excluded.note, "
                "ord=excluded.ord, archived=excluded.archived",
                (tid, body["title"], lead, body.get("note"), body.get("order", 0), int(body.get("archived", False))),
            )
            row = c.execute("SELECT * FROM tracks WHERE id = ?", (tid,)).fetchone()
            self._touched_tracks.add(tid)
        return self._track(row)

    # ------------------------------------------------------------------ reading items
    def _row(self, c, num: int):
        row = c.execute("SELECT * FROM items WHERE num = ?", (num,)).fetchone()
        if row is None:
            raise ApiError("not_found", "no such item")
        return row

    def _serialize(self, c, row) -> dict:
        num = row["num"]
        conds = []
        for cr in c.execute("SELECT * FROM conditions WHERE item = ? ORDER BY pos", (num,)):
            if cr["dep"] is not None:
                conds.append({"item": item_id(cr["dep"]), "met": cr["met_at"] is not None, "met_at": cr["met_at"]})
            else:
                conds.append({"time": cr["at"], "met": cr["met_at"] is not None, "met_at": cr["met_at"]})
        waited = [item_id(r[0]) for r in c.execute(
            "SELECT DISTINCT item FROM conditions WHERE dep = ? ORDER BY item", (num,))]
        steps = []
        if row["due_at"]:
            for s in c.execute("SELECT * FROM ladder_steps WHERE item = ? AND due_at = ? AND skipped = 0 "
                               "ORDER BY step",
                               (num, row["due_at"])):
                steps.append({"action": s["action"], "at": s["at"], "to": s["target"],
                              "caught_up": bool(s["caught_up"])})
        when = None
        if row["when_start"]:
            when = {"start": row["when_start"], "end": row["when_end"], "precision": row["when_precision"]}
        at_risk_from = json.loads(row["at_risk_from"])
        return {
            "id": item_id(num),
            "owner": row["owner"],
            "track": row["track"],
            "title": row["title"],
            "label": row["label"],
            "kind": row["kind"],
            "state": row["state"],
            "state_since": row["state_since"],
            "waits_on": conds,
            "waited_on_by": waited,
            "question": json.loads(row["question"]) if row["question"] else None,
            "answer_at": self._answer_at(c, row),
            "running_until": row["running_until"] if row["state"] == "running" else None,
            "running_overdue": row["state"] == "running" and row["running_check"] is not None,
            "when": when,
            "due_at": row["due_at"],
            "wake_at": row["wake_at"],
            "overdue": json.loads(row["overdue"]),
            "note": row["note"],
            "links": json.loads(row["links"]),
            "late": bool(row["late"]),
            "at_risk": bool(at_risk_from),
            "at_risk_from": at_risk_from,
            "overdue_steps": steps,
            "version": row["version"],
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
            "done_at": row["done_at"],
        }

    @staticmethod
    def _answer_at(c, row) -> dict | None:
        """Where a decision or approval is answered; by default the creating agent's session."""
        if row["kind"] not in ANSWERED:
            return None
        if row["answer_at"]:
            return {**json.loads(row["answer_at"]), "default": False}
        creator = c.execute("SELECT role FROM clients WHERE name = ?", (row["created_by"],)).fetchone()
        if creator is not None and creator["role"] in ("agent", "coordinator"):
            return {"session": row["created_by"], "default": True}
        return None

    def get_item(self, iid: str, history: bool = False) -> dict:
        num = item_num(iid)
        with self.read() as c:
            item = self._serialize(c, self._row(c, num))
            if history:
                item["history"] = [
                    {"at": h["at"], "by": h["by"], "event": h["event"],
                     "changes": json.loads(h["changes"]) if h["changes"] else None, "note": h["note"]}
                    for h in c.execute("SELECT * FROM history WHERE item = ? ORDER BY id", (num,))
                ]
        return item

    def items(self, where: str = "1", args: tuple = ()) -> list[dict]:
        with self.read() as c:
            rows = c.execute(f"SELECT * FROM items WHERE {where} ORDER BY created_at, num", args).fetchall()
            return [self._serialize(c, r) for r in rows]

    def list_items(self, owner=None, track=None, states=OPEN, waiting_on=None, waits_on_human=None,
                   late=None, at_risk=None, changed_since=None, limit=500) -> tuple[list[dict], bool]:
        clauses, args = [], []
        if owner:
            clauses.append("owner = ?"); args.append(owner)
        if track:
            clauses.append("track = ?"); args.append(track)
        if states is not None:
            clauses.append(f"state IN ({','.join('?' * len(states))})"); args.extend(states)
        if changed_since:
            clauses.append("updated_at > ?"); args.append(changed_since)
        if late is not None:
            clauses.append("late = ?"); args.append(int(late))
        if at_risk is not None:
            clauses.append("at_risk_from " + ("!=" if at_risk else "=") + " '[]'")
        items = self.items(" AND ".join(clauses) or "1", tuple(args))
        if waiting_on or waits_on_human is not None:
            humans: set[str] = set()
            if waits_on_human is not None:
                with self.read() as c:
                    humans = {r[0] for r in c.execute("SELECT name FROM clients WHERE role = 'human'")}
            owner_of = {}
            with self.read() as c:
                for r in c.execute("SELECT num, owner FROM items"):
                    owner_of[item_id(r[0])] = r[1]

            def keep(it):
                open_owners = {owner_of.get(cd["item"]) for cd in it["waits_on"] if "item" in cd and not cd["met"]}
                if waiting_on and waiting_on not in open_owners:
                    return False
                if waits_on_human is True and not (open_owners & humans):
                    return False
                if waits_on_human is False and (open_owners & humans):
                    return False
                return True

            items = [it for it in items if keep(it)]
        return items[:limit], len(items) > limit

    # ------------------------------------------------------------------ writing items
    def create_item(self, caller: Client, body: dict) -> dict:
        schema.check("ItemCreate", body)
        if caller.role not in WRITERS:
            raise ApiError("forbidden", "this client may not write items")
        now = fmt(self.now())
        owner = body.get("owner", caller.name)
        with self.tx() as c:
            self.actor = caller.name
            self._check_owner(owner)
            self._check_track(c, body["track"])
            vals = self._validated_fields(c, body, kind=body["kind"])
            state = body.get("state") or "waiting"  # derived below unless running
            cur = c.execute(
                "INSERT INTO items (owner, track, title, label, kind, state, state_since, question, when_start, "
                "when_end, when_precision, due_at, wake_at, overdue, note, links, answer_at, version, created_at, "
                "created_by, updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
                (owner, body["track"], body["title"], body.get("label"), body["kind"], state, now,
                 vals.get("question"), *vals.get("when", (None, None, None)), vals.get("due_at"),
                 vals.get("wake_at"), vals.get("overdue", "[]"), body.get("note"), vals.get("links", "[]"),
                 vals.get("answer_at"), now, caller.name, now, caller.name),
            )
            num = cur.lastrowid
            if "waits_on" in body:
                self._set_conditions(c, num, body["waits_on"])
            self._history(c, num, caller.name, "created", None, body.get("note"))
            self._initial_state(c, num, state)
            self._running_lease(c, num, became_running=state == "running", renew=False,
                                given=self._running_until_arg(body, state == "running"))
            self.recompute(c)
            return self._serialize(c, self._row(c, num))

    def patch_item(self, caller: Client, iid: str, body: dict) -> dict:
        num = item_num(iid)
        schema.check("ItemPatch", body)
        if caller.role not in WRITERS:
            raise ApiError("forbidden", "this client may not write items")
        now = fmt(self.now())
        with self.tx() as c:
            self.actor = caller.name
            row = self._row(c, num)
            if caller.role == "agent" and row["owner"] != caller.name:
                if not (self._creator_changes(c, caller, row, body) or self._asker_closes(c, caller, row, body)):
                    raise ApiError("forbidden", "an agent changes only its own items")
            if "if_version" in body and body["if_version"] != row["version"]:
                raise ApiError("version_conflict", "the item changed since that version")
            before = self._serialize(c, row)
            sets: dict[str, object] = {}
            if "owner" in body:
                self._check_owner(body["owner"])
                sets["owner"] = body["owner"]
            if "track" in body:
                self._check_track(c, body["track"])
                sets["track"] = body["track"]
            for f in ("title", "label", "note"):
                if f in body:
                    sets[f] = body[f]
            kind = body.get("kind", row["kind"])
            if "kind" in body:
                sets["kind"] = kind
            vals = self._validated_fields(c, body, kind=kind, existing=row)
            for f in ("question", "due_at", "wake_at", "overdue", "links", "answer_at"):
                if f in vals:
                    sets[f] = vals[f]
            if "when" in vals:
                sets["when_start"], sets["when_end"], sets["when_precision"] = vals["when"]
            new_state = body.get("state")
            will_run = new_state == "running" or (new_state is None and row["state"] == "running")
            given_until = self._running_until_arg(body, will_run)
            if new_state == "dropped" and not body.get("note"):
                raise ApiError("invalid", "note: a dropped item needs the reason", field="note")
            if sets:
                cols = ", ".join(f"{k} = ?" for k in sets)
                c.execute(f"UPDATE items SET {cols} WHERE num = ?", (*sets.values(), num))
            if "waits_on" in body:
                self._set_conditions(c, num, body["waits_on"])
            if "add_waits_on" in body:
                self._add_conditions(c, num, body["add_waits_on"])
            if new_state:
                self._owner_state(c, num, row["state"], new_state, now)
            self._running_lease(c, num, became_running=row["state"] != "running",
                                renew=caller.name == row["owner"], given=given_until)
            c.execute("UPDATE items SET version = version + 1, updated_at = ?, updated_by = ? WHERE num = ?",
                      (now, caller.name, num))
            after = self._serialize(c, self._row(c, num))
            changes = {k: [before[k], after[k]] for k in after
                       if k not in DERIVED_FIELDS and before[k] != after[k]}
            note = body.get("note") if new_state in CLOSED else None
            self._history(c, num, caller.name, "changed", changes or None, note)
            owner_update = caller.name == row["owner"] or (new_state in CLOSED and row["state"] not in CLOSED)
            for fn in self.change_listeners:
                fn(c, num, owner_update, set(changes))
            self.recompute(c)
            return self._serialize(c, self._row(c, num))

    def ask(self, caller: Client, iid: str, body: dict) -> tuple[dict, dict, bool]:
        """Create a decision for a human and make item `iid` wait on it, in one transaction.
        Returns (asked, item, created); an open identical question is returned, not repeated."""
        num = item_num(iid)
        schema.check("AskRequest", body)
        if caller.role not in WRITERS:
            raise ApiError("forbidden", "this client may not write items")
        human = self.client(body["human"])
        if human is None or human.role != "human":
            raise ApiError("invalid", "human: no such human", field="human")
        with self.tx() as c:
            row = self._row(c, num)
            if caller.role == "agent" and row["owner"] != caller.name:
                raise ApiError("forbidden", "an agent changes only its own items")
            q = body["question"]
            for cand in c.execute(
                    "SELECT i.num, i.question FROM conditions k JOIN items i ON i.num = k.dep WHERE k.item = ? "
                    "AND i.owner = ? AND i.kind = 'decision' AND i.title = ? AND i.created_by = ? "
                    "AND i.state IN ('next','running','waiting')", (num, body["human"], body["title"], caller.name)):
                if json.loads(cand["question"] or "{}").get("text") == q["text"]:
                    return (self._serialize(c, self._row(c, cand["num"])), self._serialize(c, row), False)
            create = {k: body[k] for k in ("title", "label", "question", "answer_at", "due_at", "note", "links")
                      if k in body}
            create.update(owner=body["human"], track=body.get("track", row["track"]), kind="decision")
            asked = self.create_item(caller, create)
            item = self.patch_item(caller, iid, {"add_waits_on": [{"item": asked["id"]}]})
            return self.get_item(asked["id"]), item, True

    def _creator_changes(self, c, caller: Client, row, body: dict) -> bool:
        """An agent keeps writing a task or event it created and handed to another agent, while the
        item is open (design: "a client writes its own items and those it hands over"). This is how a
        service that files items for others keeps them up to date and closes them. Not for items now
        owned by a human or a coordinator (questions keep their own rule, _asker_closes), and not
        after the owner closed or dropped it: theirs stands. Every such change is in the history
        under the creator's name, like any other. The creator may close it or set it back to
        next/waiting, but never `running`: only the owner knows it is working on it (#57)."""
        if row["created_by"] != caller.name or row["state"] not in OPEN or row["kind"] not in ("task", "event"):
            return False
        if body.get("state") == "running" or "running_until" in body:
            return False
        owner = c.execute("SELECT role FROM clients WHERE name = ?", (row["owner"],)).fetchone()
        return owner is not None and owner["role"] == "agent"

    def _asker_closes(self, c, caller: Client, row, body: dict) -> bool:
        """The agent that asked a person a question may close it once answered (design owner, #48):
        only while it is open, only `state: done`, with a note that quotes the answer ("…") and says
        where it was given (a session or an issue). Any other change of someone else's item, and any
        change after the person closed or dropped it, stays refused."""
        owner = c.execute("SELECT role FROM clients WHERE name = ?", (row["owner"],)).fetchone()
        if (row["created_by"] != caller.name or not row["question"] or owner is None or owner["role"] != "human"
                or row["state"] not in OPEN  # the person closed or dropped it: theirs stands
                or body.get("state") != "done" or set(body) - {"state", "note", "if_version"}):
            return False
        note = body.get("note") or ""
        if not (_QUOTED.search(note) and _WHERE.search(note)):
            raise ApiError("invalid", 'note: to close a question you asked, quote the answer ("…") and say where '
                                      "it was given (session or issue)", field="note")
        return True

    def _running_until_arg(self, body: dict, will_run: bool) -> str | None:
        if "running_until" not in body:
            return None
        if not will_run:
            raise ApiError("invalid", "running_until: only for a running item", field="running_until")
        value = _ts_field(body["running_until"], "running_until")
        if value <= fmt(self.now()):
            raise ApiError("invalid", "running_until: must be in the future", field="running_until")
        return value

    def _running_lease(self, c, num: int, became_running: bool, renew: bool, given: str | None) -> None:
        """Keep the running lease: set when an item starts running, renewed by the owner's updates (to the
        given time or now + the lease), cleared when it stops. A new lease forgets an earlier check."""
        state = c.execute("SELECT state FROM items WHERE num = ?", (num,)).fetchone()[0]
        if state != "running":
            c.execute("UPDATE items SET running_until = NULL, running_check = NULL, running_lapse_at = NULL "
                      "WHERE num = ?", (num,))
            return
        if not (became_running or renew or given):
            return  # someone else's update leaves the owner's lease as it is
        until = given or fmt(self.now() + timedelta(seconds=self.settings.running_lease_seconds))
        c.execute("UPDATE items SET running_until = ?, running_check = NULL, running_lapse_at = NULL WHERE num = ?",
                  (until, num))

    def _check_owner(self, owner: str) -> None:
        cl = self.client(owner)
        if cl is None or cl.role not in WRITERS:
            raise ApiError("invalid", "owner: no such agent or human", field="owner")

    @staticmethod
    def _check_track(c, track: str) -> None:
        if not c.execute("SELECT 1 FROM tracks WHERE id = ?", (track,)).fetchone():
            raise ApiError("invalid", "track: no such track", field="track")

    def _validated_fields(self, c, body: dict, kind: str, existing=None) -> dict:
        """Semantic checks beyond the schema; returns column values for the fields given."""
        out: dict[str, object] = {}
        texts = {"title": body.get("title"), "label": body.get("label"), "note": body.get("note")}
        q = body.get("question")
        if q:
            texts["question.text"] = q.get("text")
            texts["question.recommendation"] = q.get("recommendation")
            for i, o in enumerate(q.get("options", [])):
                texts[f"question.options.{i}"] = o
        for i, l in enumerate(body.get("links") or []):
            texts[f"links.{i}.url"] = l["url"]
            texts[f"links.{i}.label"] = l.get("label")
        bad = content.check_fields(texts)
        if bad:
            raise ApiError("content", f"{bad}: looks like it holds data that must not be stored", field=bad)

        # questions belong to decisions (required) and approvals (optional)
        has_q = q if "question" in body else (json.loads(existing["question"]) if existing and existing["question"] else None)
        if kind == "decision" and not has_q:
            raise ApiError("invalid", "question: a decision needs one", field="question")
        if kind in ("task", "event") and has_q:
            raise ApiError("invalid", "question: only decisions and approvals have one", field="question")
        if "question" in body:
            out["question"] = None if q is None else json.dumps(
                {"text": q["text"], "options": q.get("options", []), "recommendation": q.get("recommendation")})

        if "when" in body:
            out["when"] = self._when(body["when"])
        for f in ("due_at", "wake_at"):
            if f in body:
                out[f] = _ts_field(body[f], f)
        if "overdue" in body:
            steps = []
            for i, s in enumerate(body["overdue"]):
                to = s.get("to")
                if (s["action"] == "notify") != (to is not None):
                    raise ApiError("invalid", f"overdue.{i}.to: needed for notify only", field=f"overdue.{i}.to")
                if to is not None:
                    cl = self.client(to)
                    if cl is None or cl.role != "human":
                        raise ApiError("invalid", f"overdue.{i}.to: not a human", field=f"overdue.{i}.to")
                parse_delay(s["after"])
                steps.append({"after": s["after"], "action": s["action"], "to": to})
            out["overdue"] = json.dumps(steps)
        if "links" in body:
            out["links"] = json.dumps([{"url": l["url"], "label": l.get("label")} for l in body["links"]])
        if "answer_at" in body:
            out["answer_at"] = self._answer_at_value(body["answer_at"], kind)
        elif existing is None and kind in ANSWERED and any(_ISSUE_LINK.match(l["url"]) for l in body.get("links") or []):
            # a new question that links an issue must say whether it is answered there or in a session
            raise ApiError("invalid", "answer_at: say where the answer goes: answer_at issue or session",
                           field="answer_at")
        return out

    def _answer_at_value(self, value: dict | None, kind: str) -> str | None:
        if value is None:
            return None
        if kind not in ANSWERED:
            raise ApiError("invalid", "answer_at: only decisions and approvals are answered", field="answer_at")
        if "issue" in value:
            if content.check_fields({"links.0.url": value["issue"]}):
                raise ApiError("content", "answer_at.issue: looks like it holds data that must not be stored",
                               field="answer_at.issue")
            return json.dumps({"issue": value["issue"]})
        cl = self.client(value["session"])
        if cl is None or cl.role not in ("agent", "coordinator"):
            raise ApiError("invalid", "answer_at.session: not a session's client name", field="answer_at.session")
        return json.dumps({"session": value["session"]})

    @staticmethod
    def _when(w):
        if w is None:
            return (None, None, None)
        prec = w["precision"]
        try:
            if prec == "day":
                start = parse_day(w["start"])
                end = parse_day(w["end"]) if w.get("end") else None
                s, e = start.isoformat(), end.isoformat() if end else None
            else:
                start = parse_ts(w["start"])
                end = parse_ts(w["end"]) if w.get("end") else None
                trunc = {"minute": dict(second=0, microsecond=0), "hour": dict(minute=0, second=0, microsecond=0)}[prec]
                start = start.replace(**trunc)
                end = end.replace(**trunc) if end else None
                s, e = fmt(start), fmt(end)
        except ValueError:
            raise ApiError("invalid", f"when: start and end must be {'days' if prec == 'day' else 'timestamps'} "
                                      f"for precision {prec}", field="when")
        if end is not None and end < start:
            raise ApiError("invalid", "when.end: before the start", field="when.end")
        return (s, e, prec)

    # -- conditions
    def _set_conditions(self, c, num: int, conds: list[dict]) -> None:
        c.execute("DELETE FROM conditions WHERE item = ?", (num,))
        self._add_conditions(c, num, conds)

    def _add_conditions(self, c, num: int, conds: list[dict]) -> None:
        existing = c.execute("SELECT dep, at FROM conditions WHERE item = ?", (num,)).fetchall()
        have = {(r["dep"], r["at"]) for r in existing}
        pos = c.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM conditions WHERE item = ?", (num,)).fetchone()[0]
        new_deps = []
        for i, cond in enumerate(conds):
            if "item" in cond:
                dep = item_num(cond["item"]) if cond["item"].startswith("W-") else None
                if dep is None or not c.execute("SELECT 1 FROM items WHERE num = ?", (dep,)).fetchone():
                    raise ApiError("invalid", f"waits_on.{i}: no such item", field=f"waits_on.{i}")
                key = (dep, None)
                new_deps.append(dep)
            else:
                key = (None, _ts_field(cond["time"], f"waits_on.{i}.time"))
            if key in have:
                continue
            have.add(key)
            c.execute("INSERT INTO conditions (item, pos, dep, at) VALUES (?,?,?,?)", (num, pos, key[0], key[1]))
            pos += 1
        if c.execute("SELECT COUNT(*) FROM conditions WHERE item = ?", (num,)).fetchone()[0] > 20:
            raise ApiError("invalid", "waits_on: at most 20 conditions", field="waits_on")
        cycle = self._find_cycle(c, num)
        if cycle:
            raise ApiError("loop", "the conditions would close a loop", cycle=cycle)

    @staticmethod
    def _find_cycle(c, start: int) -> list[str] | None:
        """A path start -> ... -> start through item conditions, if there is one."""
        deps: dict[int, list[int]] = {}
        for r in c.execute("SELECT item, dep FROM conditions WHERE dep IS NOT NULL ORDER BY item, pos"):
            deps.setdefault(r[0], []).append(r[1])
        stack = [(start, [start])]
        seen = set()
        while stack:
            node, path = stack.pop()
            for d in deps.get(node, []):
                if d == start:
                    return [item_id(n) for n in path + [start]]
                if d not in seen:
                    seen.add(d)
                    stack.append((d, path + [d]))
        return None

    # -- states
    def _initial_state(self, c, num: int, state: str) -> None:
        """A new item starts in its derived state (or running), not with a derived change."""
        if state == "running":
            return
        met = self._all_met(c, num, self.now())
        c.execute("UPDATE items SET state = ? WHERE num = ?", ("next" if met else "waiting", num))

    def _owner_state(self, c, num: int, old: str, new: str, now: str) -> None:
        if new in ("next", "waiting"):
            if old in ("next", "waiting"):
                return
            target = "next" if self._all_met(c, num, self.now()) else "waiting"
            c.execute("UPDATE items SET state = ?, state_since = ?, done_at = NULL WHERE num = ?", (target, now, num))
            return
        if new == old:
            return
        done_at = now if new in CLOSED else None
        c.execute("UPDATE items SET state = ?, state_since = ?, done_at = ? WHERE num = ?", (new, now, done_at, num))

    def _all_met(self, c, num: int, now: datetime) -> bool:
        for cr in c.execute("SELECT dep, at FROM conditions WHERE item = ?", (num,)).fetchall():
            if cr["dep"] is not None:
                st = c.execute("SELECT state FROM items WHERE num = ?", (cr["dep"],)).fetchone()[0]
                if st != "done":
                    return False
            elif parse_ts(cr["at"]) > now:
                return False
        return True

    @staticmethod
    def client_events(c, num: int) -> list[dict]:
        """The item's client events (created, changed), oldest first, each with `owner`: who owned
        the item when it happened (worked out backwards from the current owner and hand-overs)."""
        rows = [dict(r) for r in c.execute(
            "SELECT id, at, by, event, changes FROM history WHERE item = ? AND by IS NOT NULL "
            "AND event IN ('created', 'changed') ORDER BY id", (num,))]
        owner = c.execute("SELECT owner FROM items WHERE num = ?", (num,)).fetchone()[0]
        for r in reversed(rows):
            r["changes"] = json.loads(r["changes"]) if r["changes"] else {}
            if "owner" in r["changes"]:
                owner = r["changes"]["owner"][0]  # before the hand-over
            r["owner"] = owner
        return rows

    def _history(self, c, num: int, by: str | None, event: str, changes: dict | None, note: str | None) -> None:
        self._touched_items.add(num)
        c.execute("INSERT INTO history (item, at, by, event, changes, note) VALUES (?,?,?,?,?,?)",
                  (num, fmt(self.now()), by, event, json.dumps(changes) if changes else None, note))

    # ------------------------------------------------------------------ derivation
    def tick(self) -> list[Transition]:
        """Re-derive what changes with time alone; the scheduler calls this every pass."""
        with self.tx() as c:
            return self.recompute(c)

    def recompute(self, c) -> list[Transition]:
        """Bring conditions, states and flags in line with the data and the clock.

        Runs inside the caller's transaction. Writes only what changed, each change with a
        history event by Waypoint (by = NULL), and returns the transitions worth telling someone.
        """
        now = self.now()
        nows = fmt(now)
        rows = {r["num"]: r for r in c.execute("SELECT num, owner, state, due_at, late, at_risk_from, done_at FROM items")}
        conds = c.execute("SELECT item, pos, dep, at, met_at FROM conditions ORDER BY item, pos").fetchall()
        transitions: list[Transition] = []
        newly_met: dict[int, list[str]] = {}

        # 1. conditions
        by_item: dict[int, list[dict]] = {}
        for cr in conds:
            if cr["dep"] is not None:
                dep = rows[cr["dep"]]
                met_at = dep["done_at"] if dep["state"] == "done" else None
            else:
                met_at = cr["at"] if parse_ts(cr["at"]) <= now else None
            if met_at != cr["met_at"]:
                c.execute("UPDATE conditions SET met_at = ? WHERE item = ? AND pos = ?", (met_at, cr["item"], cr["pos"]))
                what = item_id(cr["dep"]) if cr["dep"] is not None else cr["at"]
                if met_at is not None:
                    self._history(c, cr["item"], None, "condition_met", None, what)
                    newly_met.setdefault(cr["item"], []).append(what)
            by_item.setdefault(cr["item"], []).append({"dep": cr["dep"], "met": met_at is not None})

        # 2. derived states
        state = {n: r["state"] for n, r in rows.items()}
        for n, r in rows.items():
            if r["state"] not in ("next", "waiting"):
                continue
            new = "next" if all(x["met"] for x in by_item.get(n, [])) else "waiting"
            if new != r["state"]:
                state[n] = new
                self._derived(c, n, {"state": [r["state"], new]}, extra="state = ?, state_since = ?",
                              args=(new, nows))
                if new == "next":
                    transitions.append(Transition("unblocked", item_id(n), r["owner"], newly_met.get(n, [])))
                else:
                    transitions.append(Transition("replan", item_id(n), r["owner"]))

        # 3. late
        late = {}
        for n, r in rows.items():
            late[n] = bool(r["due_at"]) and state[n] in OPEN and parse_ts(r["due_at"]) < now
            if late[n] != bool(r["late"]):
                self._derived(c, n, {"late": [bool(r["late"]), late[n]]}, extra="late = ?", args=(int(late[n]),))
                if late[n]:
                    transitions.append(Transition("late", item_id(n), r["owner"]))

        # 4. at risk: open items reached upstream through open item conditions that are late or dropped
        for n, r in rows.items():
            sources = []
            if state[n] in OPEN:
                seen, todo = set(), [n]
                while todo:
                    cur = todo.pop(0)
                    for x in by_item.get(cur, []):
                        if x["dep"] is None or x["met"] or x["dep"] in seen:
                            continue
                        up = x["dep"]
                        seen.add(up)
                        if state[up] == "dropped":
                            sources.append({"item": item_id(up), "why": "dropped", "due_at": None})
                        elif late[up]:
                            sources.append({"item": item_id(up), "why": "late", "due_at": rows[up]["due_at"]})
                        todo.append(up)
                sources.sort(key=lambda s: item_num(s["item"]))
            old = json.loads(r["at_risk_from"])
            if sources != old:
                self._derived(c, n, {"at_risk_from": [old, sources]}, extra="at_risk_from = ?",
                              args=(json.dumps(sources),))
                fresh = {s["item"] for s in sources} - {s["item"] for s in old}
                dropped = [s["item"] for s in sources if s["why"] == "dropped" and s["item"] in fresh]
                if dropped:
                    transitions.append(Transition("replan", item_id(n), r["owner"], dropped))
                elif fresh:
                    transitions.append(Transition("at_risk", item_id(n), r["owner"], sorted(fresh, key=item_num)))
        for fn in self.listeners:
            fn(c, transitions)
        return transitions

    def _derived(self, c, num: int, changes: dict, extra: str, args: tuple) -> None:
        c.execute(f"UPDATE items SET {extra}, version = version + 1, updated_at = ?, updated_by = NULL WHERE num = ?",
                  (*args, fmt(self.now()), num))
        self._history(c, num, None, "derived", changes, None)

    # ------------------------------------------------------------------ views
    def done_cutoff(self) -> datetime:
        return self.now() - timedelta(days=self.settings.done_days)

    def coordinator(self) -> str | None:
        if self.settings.coordinator:
            return self.settings.coordinator
        with self.read() as c:
            row = c.execute("SELECT name FROM clients WHERE role = 'coordinator' AND retired_at IS NULL "
                            "ORDER BY name LIMIT 1").fetchone()
        return row[0] if row else None
