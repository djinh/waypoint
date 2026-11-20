"""Build the example view responses in examples/ from a small invented scenario.

The scenario below is written the way an owner would enter it (owner-set states, conditions,
times); this script derives everything Waypoint derives (next/waiting, late, at risk, met
conditions, edges, chains) and writes the two read models in exactly the contract's shape:

    python examples/make_examples.py          # rewrites examples/*.json

It is a reference for the contract, not the daemon: the daemon has its own implementation, and
once it exists the examples are checked against it. Standard library only.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

OPEN = ("next", "running", "waiting")
CLOSED = ("done", "dropped")


def ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _is_item_ref(cond: str) -> bool:
    return cond.startswith("W-")


class Scenario:
    """Owner-entered items plus the derivations of the contract (docs/API.md)."""

    def __init__(self, spec: dict):
        self.now = parse(spec["now"])
        self.tz = spec["tz"]
        self.done_days = spec.get("done_days", 3)
        self.tracks = spec["tracks"]
        self.clients = spec["clients"]
        self.recurrences = spec.get("recurrences", [])
        self.raw = {it["id"]: it for it in spec["items"]}
        self.items = {}
        for iid in self.raw:
            self._derive_basic(iid)
        for iid in self.items:
            self._derive_risk(iid)

    # -- derivation ---------------------------------------------------------------------
    def _conditions(self, raw: dict) -> list[dict]:
        out = []
        for c in raw.get("waits_on", []):
            if _is_item_ref(c):
                b = self.raw[c]
                met = b.get("state") == "done"
                out.append({"item": c, "met": met, "met_at": b.get("done_at") if met else None})
            else:
                met = parse(c) <= self.now
                out.append({"time": c, "met": met, "met_at": c if met else None})
        return out

    def _derive_basic(self, iid: str) -> None:
        raw = self.raw[iid]
        conds = self._conditions(raw)
        state = raw.get("state")
        if state not in ("running", "done", "dropped"):
            state = "next" if all(c["met"] for c in conds) else "waiting"
        due = raw.get("due_at")
        late = bool(due) and parse(due) < self.now and state in OPEN
        created_at = raw["created_at"]
        updated_at = raw.get("updated_at", raw.get("done_at") or raw.get("state_since") or created_at)
        self.items[iid] = {
            "id": iid,
            "owner": raw["owner"],
            "track": raw["track"],
            "title": raw["title"],
            "label": raw.get("label"),
            "kind": raw["kind"],
            "state": state,
            "state_since": raw.get("state_since") or raw.get("done_at") or created_at,
            "waits_on": conds,
            "waited_on_by": sorted(
                (o for o, r in self.raw.items() if iid in r.get("waits_on", [])), key=_id_key
            ),
            "question": raw.get("question"),
            "answer_at": self._answer_at(raw),
            "running_until": raw.get("running_until") if state == "running" else None,
            "running_overdue": bool(raw.get("running_overdue")) and state == "running",
            "when": raw.get("when"),
            "due_at": due,
            "wake_at": raw.get("wake_at"),
            "overdue": raw.get("overdue", []),
            "note": raw.get("note"),
            "links": [{"url": l["url"], "label": l.get("label")} for l in raw.get("links", [])],
            "late": late,
            "at_risk": False,
            "at_risk_from": [],
            "overdue_steps": [
                {"action": s["action"], "at": s["at"], "to": s.get("to"), "caught_up": s.get("caught_up", False)}
                for s in raw.get("overdue_steps", [])
            ],
            "version": raw.get("version", 1),
            "created_at": created_at,
            "created_by": raw.get("created_by", raw["owner"]),
            "updated_at": updated_at,
            "updated_by": raw.get("updated_by", raw["owner"]),
            "done_at": raw.get("done_at"),
            "recurrence": None,
            "occurrence_at": None,
        }

    def _answer_at(self, raw: dict) -> dict | None:
        if raw["kind"] not in ("decision", "approval"):
            return None
        if raw.get("answer_at"):
            return {**raw["answer_at"], "default": False}
        creator = raw.get("created_by", raw["owner"])
        if self.clients.get(creator, {}).get("role") in ("agent", "coordinator"):
            return {"session": creator, "default": True}
        return None

    def _derive_risk(self, iid: str) -> None:
        item = self.items[iid]
        if item["state"] not in OPEN:
            return
        sources, seen, todo = [], set(), [iid]
        while todo:
            cur = todo.pop(0)
            for c in self.items[cur]["waits_on"]:
                if "item" not in c or c["met"] or c["item"] in seen:
                    continue
                up = self.items[c["item"]]
                seen.add(up["id"])
                if up["state"] == "dropped":
                    sources.append({"item": up["id"], "why": "dropped", "due_at": None})
                elif up["late"]:
                    sources.append({"item": up["id"], "why": "late", "due_at": up["due_at"]})
                todo.append(up["id"])
        item["at_risk_from"] = sorted(sources, key=lambda s: _id_key(s["item"]))
        item["at_risk"] = bool(sources)

    # -- graph helpers ------------------------------------------------------------------
    def _chain(self, iid: str, downstream: bool) -> list[dict]:
        out, seen, frontier, depth = [], {iid}, [iid], 0
        while frontier:
            depth += 1
            nxt = []
            for cur in frontier:
                if downstream:
                    neigh = self.items[cur]["waited_on_by"]
                else:
                    neigh = [c["item"] for c in self.items[cur]["waits_on"] if "item" in c and not c["met"]]
                for n in neigh:
                    if n in seen or self.items[n]["state"] not in OPEN:
                        continue
                    seen.add(n)
                    nxt.append(n)
                    out.append({"id": n, "depth": depth})
            frontier = sorted(nxt, key=_id_key)
        return out

    def _add_risk_sources(self, ids: set[str]) -> None:
        """A view always holds the items its at-risk flags name, so a page can say why."""
        for i in list(ids):
            ids.update(src["item"] for src in self.items[i]["at_risk_from"])

    def _edges(self, ids: list[str]) -> list[dict]:
        idset = set(ids)
        edges = []
        for iid in ids:
            for c in self.items[iid]["waits_on"]:
                if "item" in c and c["item"] in idset:
                    edges.append({"from": c["item"], "to": iid, "met": c["met"]})
        return edges

    def _order(self, ids: set[str]) -> list[str]:
        """Track order, then dependency order (depth in the graph), then id."""
        track_order = {t["id"]: (t["order"], t["id"]) for t in self.tracks}

        def depth(iid: str, stack=()) -> int:
            ups = [c["item"] for c in self.items[iid]["waits_on"] if "item" in c and c["item"] not in stack]
            return 1 + max((depth(u, stack + (iid,)) for u in ups), default=-1)

        return sorted(ids, key=lambda i: (track_order[self.items[i]["track"]], depth(i), _id_key(i)))

    def _owners(self, items: list[dict], tracks: list[dict]) -> dict:
        names = set()
        for it in items:
            names.update({it["owner"], it["created_by"]})
            if it["updated_by"]:
                names.add(it["updated_by"])
        names.update(t["lead"] for t in tracks if t["lead"])
        out = {}
        for n in sorted(names):
            c = self.clients.get(n)
            out[n] = {"role": c["role"], "label": c.get("label", n)} if c else {"role": "unknown", "label": n}
        return out

    def _tracks(self, track_ids=None) -> list[dict]:
        ts_ = [
            {
                "id": t["id"],
                "title": t["title"],
                "lead": t.get("lead"),
                "note": t.get("note"),
                "order": t["order"],
                "archived": t.get("archived", False),
            }
            for t in self.tracks
        ]
        if track_ids is not None:
            ts_ = [t for t in ts_ if t["id"] in track_ids]
        return sorted(ts_, key=lambda t: (t["order"], t["id"]))

    # -- views --------------------------------------------------------------------------
    def _upcoming(self, to: datetime, live: set) -> list:
        """The coming occurrences of the scenario's daily recurrences (wall-clock time in the zone)."""
        out = []
        zone = ZoneInfo(self.tz)
        for r in self.recurrences:
            if r["track"] not in live:
                continue
            hh, mm = (int(x) for x in r["daily"].split(":"))
            day = self.now.astimezone(zone).date()
            while True:
                t = datetime(day.year, day.month, day.day, hh, mm, tzinfo=zone).astimezone(timezone.utc)
                if t > to:
                    break
                if t > self.now:
                    out.append({"recurrence": r["id"], "at": ts(t), "owner": r["owner"], "track": r["track"],
                                "title": r["title"], "label": r.get("label")})
                day += timedelta(days=1)
        return sorted(out, key=lambda u: (u["at"], u["recurrence"]))

    def timeline(self, window_to_days: int = 9) -> dict:
        frm = self.now - timedelta(days=self.done_days)
        to = self.now + timedelta(days=window_to_days)
        tracks = [t for t in self._tracks() if not t["archived"]]
        live = {t["id"] for t in tracks}
        ids = {
            i
            for i, it in self.items.items()
            if it["track"] in live
            and (it["state"] in OPEN or (it["done_at"] and parse(it["done_at"]) >= frm))
        }
        # every blocker an included item names is included too, so every edge can be drawn
        for i in list(ids):
            ids.update(c["item"] for c in self.items[i]["waits_on"] if "item" in c)
        self._add_risk_sources(ids)
        order = self._order(ids)
        items = [self.items[i] for i in order]
        return {
            "generated_at": ts(self.now),
            "now": ts(self.now),
            "tz": self.tz,
            "window": {"from": ts(frm), "to": ts(to)},
            "done_days": self.done_days,
            "tracks": tracks,
            "owners": self._owners(items, tracks),
            "items": items,
            "edges": self._edges(order),
            "upcoming": self._upcoming(to, live),
        }

    def for_human(self, human: str) -> dict:
        mine = [it for it in self.items.values() if it["owner"] == human and it["state"] in OPEN]
        entries = []
        for it in mine:
            ready = it["state"] in ("next", "running")
            entries.append(
                {
                    "id": it["id"],
                    "ready": ready,
                    "since": it["state_since"] if ready else it["created_at"],
                    "unblocks": self._chain(it["id"], downstream=True),
                    "on_the_way": [] if ready else self._chain(it["id"], downstream=False),
                }
            )
        entries.sort(key=lambda e: (not e["ready"], e["since"], _id_key(e["id"])))
        ids = set()
        for e in entries:
            ids.add(e["id"])
            ids.update(s["id"] for s in e["unblocks"] + e["on_the_way"])
        self._add_risk_sources(ids)
        order = self._order(ids)
        items = [self.items[i] for i in order]
        tracks = self._tracks({it["track"] for it in items})
        return {
            "generated_at": ts(self.now),
            "now": ts(self.now),
            "tz": self.tz,
            "human": human,
            "entries": entries,
            "tracks": tracks,
            "owners": self._owners(items, tracks),
            "items": items,
            "edges": self._edges(order),
            "recurrences": [],
        }


def _id_key(iid: str) -> int:
    return int(iid.split("-", 1)[1])


def write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------------------
# The invented scenario: a small team shipping release 2.4.
# Now is Tuesday 10 March 2026, 10:30 in Paris (09:30 UTC).
# ---------------------------------------------------------------------------------------
SCENARIO = {
    "now": "2026-03-10T09:30:00Z",
    "tz": "Europe/Paris",
    "done_days": 3,
    "clients": {
        "agent-a": {"role": "agent", "label": "Release agent"},
        "agent-b": {"role": "agent", "label": "Build agent"},
        "agent-c": {"role": "agent", "label": "Docs agent"},
        "coordinator": {"role": "coordinator", "label": "Coordinator"},
        "alice": {"role": "human", "label": "Alice"},
    },
    # a recurrence: an ordinary item for its owner every morning (only its coming occurrences show)
    "recurrences": [
        {"id": "R-1", "owner": "agent-b", "track": "build-infra", "title": "Morning build health check",
         "label": "build check", "daily": "08:30"},
    ],
    "tracks": [
        {"id": "release-2-4", "title": "Release 2.4", "lead": "agent-a", "order": 10},
        {"id": "build-infra", "title": "Build infrastructure", "lead": "agent-b", "order": 20},
        {"id": "docs-site", "title": "Docs site", "lead": "agent-c", "order": 30,
         "note": "no fixed dates"},
    ],
    "items": [
        # -- Release 2.4 -------------------------------------------------------------
        {"id": "W-101", "owner": "agent-a", "track": "release-2-4", "kind": "task",
         "title": "Release candidate 2.4-rc1 built", "label": "rc1 built",
         "state": "done", "created_at": "2026-03-06T10:00:00Z", "done_at": "2026-03-09T15:10:00Z",
         "note": "all checks green", "version": 4,
         "links": [{"url": "https://git.example.org/acme/app/pull/88", "label": "PR #88"}]},
        {"id": "W-102", "owner": "agent-a", "track": "release-2-4", "kind": "event",
         "title": "Staging soak of 2.4-rc1", "label": "staging soak",
         "state": "running", "waits_on": ["W-101"],
         "when": {"start": "2026-03-09T16:00:00Z", "end": "2026-03-11T16:00:00Z", "precision": "minute"},
         "wake_at": "2026-03-11T16:15:00Z", "running_until": "2026-03-11T16:00:00Z",
         "created_at": "2026-03-06T10:05:00Z", "state_since": "2026-03-09T16:00:00Z",
         "note": "no errors after 17 hours", "version": 5},
        {"id": "W-103", "owner": "alice", "track": "release-2-4", "kind": "approval",
         "title": "Go for the production release of 2.4", "label": "go for production",
         "waits_on": ["W-102"],
         "question": {"text": "Release 2.4 to production once the staging soak is clean?",
                      "options": ["go", "hold"], "recommendation": "go"},
         "created_at": "2026-03-06T10:06:00Z", "created_by": "agent-a", "updated_by": "agent-a"},
        {"id": "W-104", "owner": "agent-a", "track": "release-2-4", "kind": "task",
         "title": "Production release of 2.4", "label": "production release",
         "waits_on": ["W-103", "W-110", "W-105"],
         "when": {"start": "2026-03-12", "end": None, "precision": "day"},
         "due_at": "2026-03-12T17:00:00Z",
         "overdue": [{"after": "0m", "action": "nudge"},
                     {"after": "1h", "action": "escalate"},
                     {"after": "1h", "action": "flag"},
                     {"after": "16h", "action": "notify", "to": "alice"}],
         "created_at": "2026-03-06T10:08:00Z", "version": 3},
        {"id": "W-105", "owner": "agent-a", "track": "release-2-4", "kind": "task",
         "title": "Release notes for 2.4", "label": "release notes",
         "due_at": "2026-03-10T08:00:00Z", "note": "draft half done",
         "created_at": "2026-03-07T09:00:00Z", "state_since": "2026-03-07T09:00:00Z",
         "updated_at": "2026-03-09T17:40:00Z", "version": 3,
         "overdue_steps": [{"action": "nudge", "at": "2026-03-10T08:00:10Z"},
                           {"action": "escalate", "at": "2026-03-10T09:00:05Z"},
                           {"action": "flag", "at": "2026-03-10T09:00:05Z"}]},
        {"id": "W-106", "owner": "agent-c", "track": "release-2-4", "kind": "task",
         "title": "Announce 2.4 on the blog and the mailing list", "label": "announce 2.4",
         "waits_on": ["W-104"],
         "created_at": "2026-03-06T10:10:00Z", "created_by": "agent-a", "updated_by": "agent-a"},
        {"id": "W-107", "owner": "agent-a", "track": "release-2-4", "kind": "task",
         "title": "Hotfix release 2.3.5", "label": "hotfix 2.3.5",
         "state": "dropped", "note": "folded into 2.4",
         "created_at": "2026-03-04T08:00:00Z", "done_at": "2026-03-08T11:20:00Z", "version": 2},
        # -- Build infrastructure ----------------------------------------------------
        {"id": "W-110", "owner": "agent-b", "track": "build-infra", "kind": "task",
         "title": "Migration tool supports the 2.4 schema", "label": "migration tool",
         "waits_on": ["W-111"], "due_at": "2026-03-11T12:00:00Z",
         "note": "both strategies prototyped; waiting for the choice",
         "created_at": "2026-03-05T14:00:00Z", "version": 4},
        {"id": "W-111", "owner": "alice", "track": "build-infra", "kind": "decision",
         "title": "Choose the database migration strategy for 2.4", "label": "migration strategy",
         "question": {"text": "Migrate online (slower, no downtime) or in a 30-minute offline window?",
                      "options": ["online", "offline window"],
                      "recommendation": "online: no downtime, and the tool already supports it"},
         "created_at": "2026-03-08T11:00:00Z", "created_by": "agent-b", "updated_by": "agent-b",
         "answer_at": {"issue": "https://git.example.org/acme/app/issues/31"},
         "links": [{"url": "https://docs.example.org/plans/migration-2-4", "label": "plan"},
                   {"url": "https://git.example.org/acme/app/issues/31", "label": "#31"}]},
        {"id": "W-112", "owner": "agent-b", "track": "build-infra", "kind": "task",
         "title": "Rotate the staging deploy credentials",
         "waits_on": ["2026-03-12T08:00:00Z"], "wake_at": "2026-03-12T08:00:00Z",
         "created_at": "2026-03-09T12:00:00Z"},
        {"id": "W-113", "owner": "agent-b", "track": "build-infra", "kind": "event",
         "title": "Upstream database driver 5.0 expected", "label": "driver 5.0 out",
         "waits_on": ["2026-03-12T23:00:00Z"],
         "when": {"start": "2026-03-13", "end": None, "precision": "day"},
         "note": "maintainers said about Friday",
         "created_at": "2026-03-09T12:30:00Z"},
        {"id": "W-114", "owner": "alice", "track": "build-infra", "kind": "task",
         "title": "Renew the example.org domain", "label": "renew domain",
         "due_at": "2026-03-20T17:00:00Z",
         "created_at": "2026-03-05T09:00:00Z", "created_by": "coordinator", "updated_by": "coordinator"},
        # -- Docs site ---------------------------------------------------------------
        {"id": "W-120", "owner": "agent-c", "track": "docs-site", "kind": "task",
         "title": "Migration guide for 2.4", "label": "migration guide",
         "waits_on": ["W-111"],
         "created_at": "2026-03-08T11:05:00Z"},
        {"id": "W-121", "owner": "agent-c", "track": "docs-site", "kind": "task",
         "title": "Move the docs search to the new index", "label": "docs search",
         "state": "running", "note": "half the pages reindexed", "running_until": "2026-03-10T11:00:00Z",
         "created_at": "2026-03-02T10:00:00Z", "state_since": "2026-03-09T08:00:00Z", "version": 6},
    ],
}


def main(argv: list[str]) -> int:
    out = Path(__file__).resolve().parent
    sc = Scenario(SCENARIO)
    write(out / "timeline.json", sc.timeline())
    write(out / "for-alice.json", sc.for_human("alice"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
