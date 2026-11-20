"""The read models for pages (docs/API.md, Views), built from serialized items."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import recur
from .recurrences import Recurrences
from .store import OPEN, Store, item_num
from .timeutil import fmt, parse_ts


def _deps(it: dict) -> list[str]:
    return [c["item"] for c in it["waits_on"] if "item" in c]


def _order(items: dict[str, dict], ids: set[str], track_order: dict[str, tuple]) -> list[str]:
    """Track order, then dependency order (depth in the graph), then id."""
    memo: dict[str, int] = {}

    def depth(start: str) -> int:
        # A depth-first walk with an explicit stack, so a chain of any length can't hit Python's
        # recursion limit. Each node is entered, then finished once everything it waits on is.
        # `path` holds the nodes entered but not finished: exactly the current node's ancestors, so
        # meeting one of them again is a loop (which Waypoint refuses anyway; it counts as 0).
        if start in memo:
            return memo[start]
        stack, path = [(start, False)], set()
        while stack:
            iid, finish = stack.pop()
            ups = [d for d in _deps(items[iid]) if d in items]
            if finish:
                memo[iid] = 1 + max((memo.get(u, 0) for u in ups), default=-1)
                path.discard(iid)
            elif iid not in memo and iid not in path:
                path.add(iid)
                stack.append((iid, True))
                stack.extend((u, False) for u in ups if u not in memo and u not in path)
        return memo[start]

    far = (1 << 30, "")
    return sorted(ids, key=lambda i: (track_order.get(items[i]["track"], far), depth(i), item_num(i)))


def _edges(items: dict[str, dict], order: list[str]) -> list[dict]:
    present = set(order)
    return [
        {"from": c["item"], "to": iid, "met": c["met"]}
        for iid in order
        for c in items[iid]["waits_on"]
        if "item" in c and c["item"] in present
    ]


def _add_risk_sources(items: dict[str, dict], ids: set[str]) -> None:
    for i in list(ids):
        ids.update(s["item"] for s in items[i]["at_risk_from"])


def _owners(store: Store, items: list[dict], tracks: list[dict]) -> dict:
    names = set()
    for it in items:
        names.update({it["owner"], it["created_by"], it["updated_by"]})
    names.update(t["lead"] for t in tracks)
    return store.owners(names)


def _all_items(store: Store) -> dict[str, dict]:
    return {it["id"]: it for it in store.items()}


def _upcoming(store: Store, now: datetime, frm: datetime, to: datetime, shown: set[str]) -> list[dict]:
    """The coming occurrences of active recurrences in the shown tracks, within the window: computed,
    never stored (an occurrence becomes an item only when it falls due)."""
    out = []
    with store.read() as c:
        rows = c.execute("SELECT * FROM recurrences WHERE state = 'active'").fetchall()
    for r in rows:
        if r["track"] not in shown:
            continue
        after = max(now, frm) - timedelta(microseconds=1)  # the next one may be due right now
        for t in recur.between(json.loads(r["schedule"]), after, to, parse_ts(r["created_at"]), store.settings.tz,
                               limit=50):
            out.append({"recurrence": f"R-{r['num']}", "at": fmt(t), "owner": r["owner"], "track": r["track"],
                        "title": r["title"], "label": r["label"]})
    return sorted(out, key=lambda u: (u["at"], u["recurrence"]))


def timeline(store: Store, frm: datetime | None = None, to: datetime | None = None,
             track_ids: list[str] | None = None) -> dict:
    now = store.now()
    frm = frm or store.done_cutoff()
    to = to or now + timedelta(days=9)
    tracks = store.tracks(archived=track_ids is not None, ids=set(track_ids) if track_ids else None)
    shown = {t["id"] for t in tracks}
    items = _all_items(store)
    frm_s = fmt(frm)
    ids = {
        i for i, it in items.items()
        if it["track"] in shown and (it["state"] in OPEN or (it["done_at"] and it["done_at"] >= frm_s))
    }
    for i in list(ids):
        ids.update(_deps(items[i]))
    _add_risk_sources(items, ids)
    track_order = {t["id"]: (t["order"], t["id"]) for t in store.tracks(archived=True)}
    order = _order(items, ids, track_order)
    out_items = [items[i] for i in order]
    # tracks of items pulled in from other tracks (blockers) are listed too
    extra = {it["track"] for it in out_items} - shown
    if extra:
        tracks = tracks + store.tracks(archived=True, ids=extra)
        tracks.sort(key=lambda t: (t["order"], t["id"]))
    return {
        "generated_at": fmt(now),
        "now": fmt(now),
        "tz": store.settings.tz,
        "window": {"from": fmt(frm), "to": fmt(to)},
        "done_days": store.settings.done_days,
        "tracks": tracks,
        "owners": _owners(store, out_items, tracks),
        "items": out_items,
        "edges": _edges(items, order),
        "upcoming": _upcoming(store, now, frm, to, shown),
    }


def _chain(items: dict[str, dict], start: str, downstream: bool) -> list[dict]:
    out, seen, frontier, depth = [], {start}, [start], 0
    while frontier:
        depth += 1
        nxt = []
        for cur in frontier:
            if downstream:
                neigh = items[cur]["waited_on_by"]
            else:
                neigh = [c["item"] for c in items[cur]["waits_on"] if "item" in c and not c["met"]]
            for n in neigh:
                if n in seen or n not in items or items[n]["state"] not in OPEN:
                    continue
                seen.add(n)
                nxt.append(n)
                out.append({"id": n, "depth": depth})
        frontier = sorted(nxt, key=item_num)
    return out


def for_human(store: Store, human: str) -> dict:
    now = store.now()
    items = _all_items(store)
    entries = []
    for it in items.values():
        if it["owner"] != human or it["state"] not in OPEN:
            continue
        ready = it["state"] in ("next", "running")
        entries.append({
            "id": it["id"],
            "ready": ready,
            "since": it["state_since"] if ready else it["created_at"],
            "unblocks": _chain(items, it["id"], downstream=True),
            "on_the_way": [] if ready else _chain(items, it["id"], downstream=False),
        })
    entries.sort(key=lambda e: (not e["ready"], parse_ts(e["since"]), item_num(e["id"])))
    ids = set()
    for e in entries:
        ids.add(e["id"])
        ids.update(s["id"] for s in e["unblocks"] + e["on_the_way"])
    _add_risk_sources(items, ids)
    all_tracks = store.tracks(archived=True)
    track_order = {t["id"]: (t["order"], t["id"]) for t in all_tracks}
    order = _order(items, ids, track_order)
    out_items = [items[i] for i in order]
    tracks = [t for t in all_tracks if t["id"] in {it["track"] for it in out_items}]
    return {
        "generated_at": fmt(now),
        "now": fmt(now),
        "tz": store.settings.tz,
        "human": human,
        "entries": entries,
        "tracks": tracks,
        "owners": _owners(store, out_items, tracks),
        "items": out_items,
        "edges": _edges(items, order),
        "recurrences": Recurrences.of(store).list(owner=human),
    }
