"""The daemon's views agree with the contract's examples.

The invented scenario of examples/make_examples.py is replayed through the store (creating items,
setting conditions and states at their times on a fake clock), and the views the daemon computes
are compared with the committed examples on everything the daemon derives.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from waypoint import views
from waypoint.clock import FakeClock
from waypoint.db import connect
from waypoint.store import Client, Settings, Store
from waypoint.timeutil import parse_ts

ROOT = Path(__file__).resolve().parent.parent


def load_scenario() -> dict:
    spec = importlib.util.spec_from_file_location("make_examples", ROOT / "examples" / "make_examples.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SCENARIO


@pytest.fixture
def replayed(tmp_path) -> Store:
    sc = load_scenario()
    clock = FakeClock(parse_ts("2026-03-01T00:00:00Z"))
    st = Store(connect(tmp_path / "w.db"), clock, Settings(tz=sc["tz"], done_days=sc["done_days"]))
    for name, c in sc["clients"].items():
        st.add_client(name, c["role"], label=c.get("label"))
    who = {name: Client(name, c["role"]) for name, c in sc["clients"].items()}
    for t in sc["tracks"]:
        st.put_track(who["coordinator"], t["id"], {k: t[k] for k in ("title", "lead", "note") if k in t}
                     | {"order": t["order"]})
    raw = sorted(sc["items"], key=lambda r: int(r["id"][2:]))
    # 1. create every item at its creation time, keeping the scenario's ids (in id order, because
    #    AUTOINCREMENT never hands out an id below the highest one used)
    for r in raw:
        clock.set(parse_ts(r["created_at"]))
        seq = int(r["id"][2:]) - 1
        if st.conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'items'", (seq,)).rowcount == 0:
            st.conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('items', ?)", (seq,))
        body = {k: r[k] for k in ("owner", "track", "title", "label", "kind", "question", "answer_at", "when",
                                  "due_at", "wake_at", "overdue", "note", "links") if r.get(k) is not None}
        if "overdue" in body:
            body["overdue"] = [{k: v for k, v in s.items() if v is not None} for s in body["overdue"]]
        creator = who[r.get("created_by", r["owner"])]
        assert st.create_item(creator, body)["id"] == r["id"]
    # 2. conditions, once every item exists
    for r in raw:
        if r.get("waits_on"):
            conds = [{"item": c} if c.startswith("W-") else {"time": c} for c in r["waits_on"]]
            st.patch_item(who["coordinator"], r["id"], {"waits_on": conds})
    # 3. owner-set states at their times
    changes = []
    for r in raw:
        if r.get("state") == "running":
            changes.append((r["state_since"], r, {"state": "running", "running_until": r["running_until"]}))
        elif r.get("state") in ("done", "dropped"):
            changes.append((r["done_at"], r, {"state": r["state"], "note": r.get("note") or "closed"}))
    for at, r, body in sorted(changes, key=lambda x: x[0]):
        clock.set(parse_ts(at))
        st.patch_item(who[r["owner"]], r["id"], body)
    clock.set(parse_ts(sc["now"]))
    st.tick()
    return st


DERIVED = ("id", "owner", "track", "title", "label", "kind", "state", "waits_on", "waited_on_by", "question",
           "answer_at", "running_until", "running_overdue",
           "when", "due_at", "late", "at_risk", "at_risk_from", "done_at")


def compare_items(got: list[dict], want: list[dict]) -> None:
    assert [i["id"] for i in got] == [i["id"] for i in want], "same items in the same order"
    for g, w in zip(got, want):
        for f in DERIVED:
            assert g[f] == w[f], (g["id"], f, g[f], w[f])


def test_timeline_matches_example(replayed):
    want = json.loads((ROOT / "examples" / "timeline.json").read_text(encoding="utf-8"))
    got = views.timeline(replayed)
    assert got["now"] == want["now"] and got["window"] == want["window"] and got["tz"] == want["tz"]
    assert got["tracks"] == want["tracks"]
    assert got["owners"].keys() >= {i["owner"] for i in want["items"]}
    compare_items(got["items"], want["items"])
    assert got["edges"] == want["edges"]


def test_for_view_matches_example(replayed):
    want = json.loads((ROOT / "examples" / "for-alice.json").read_text(encoding="utf-8"))
    got = views.for_human(replayed, "alice")
    assert got["entries"] == want["entries"]
    assert got["tracks"] == want["tracks"]
    compare_items(got["items"], want["items"])
    assert got["edges"] == want["edges"]
