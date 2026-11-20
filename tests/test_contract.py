"""The API contract: docs/api/openapi.yaml and the examples that follow it.

Checks that the OpenAPI file is well formed, that the example responses validate against their
schemas, that they are internally consistent (derived states, flags, edges and chains agree with
the conditions), that they are what examples/make_examples.py produces, and that they cover the
cases the pages must handle.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "docs" / "api" / "openapi.yaml"
EXAMPLES = ROOT / "examples"
OPEN = {"next", "running", "waiting"}
TS_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


def validator(spec: dict, name: str) -> jsonschema.Draft202012Validator:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/components/schemas/{name}",
        "components": spec["components"],
    }
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# -- the spec itself ------------------------------------------------------------------------


def test_spec_is_openapi_31(spec):
    assert spec["openapi"].startswith("3.1")
    for path in (
        "/v1/items",
        "/v1/items/{id}",
        "/v1/tracks",
        "/v1/tracks/{id}",
        "/v1/views/timeline",
        "/v1/views/for/{human}",
        "/v1/deliveries",
        "/v1/deliveries/{id}/ack",
        "/v1/health",
        "/v1/items/{id}/ask",
        "/v1/clients",
        "/v1/clients/{name}",
        "/v1/clients/{name}/rotate",
    ):
        assert path in spec["paths"], path


def test_every_ref_resolves(spec):
    refs = re.findall(r'\$ref: "#/([^"]+)"', SPEC_PATH.read_text(encoding="utf-8"))
    assert refs
    for ref in refs:
        node = spec
        for part in ref.split("/"):
            assert part in node, ref
            node = node[part]


def test_only_health_is_open(spec):
    assert spec["security"] == [{"bearer": []}]
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if method == "parameters":
                continue
            if path == "/v1/health":
                assert op["security"] == []
            else:
                assert "security" not in op, f"{method} {path} overrides the token requirement"


def test_every_schema_compiles(spec):
    for name in spec["components"]["schemas"]:
        validator(spec, name)


# -- examples validate ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "file,schema", [("timeline.json", "TimelineView"), ("for-alice.json", "ForView")]
)
def test_example_validates(spec, file, schema):
    errors = sorted(validator(spec, schema).iter_errors(load(file)), key=str)
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors[:5])


def test_examples_are_generated_output(tmp_path):
    """The committed examples are exactly what make_examples.py writes."""
    script = EXAMPLES / "make_examples.py"
    copy_dir = tmp_path / "examples"
    copy_dir.mkdir()
    (copy_dir / "make_examples.py").write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run([sys.executable, "-I", str(copy_dir / "make_examples.py")], check=True, timeout=60)
    for name in ("timeline.json", "for-alice.json"):
        assert (copy_dir / name).read_text(encoding="utf-8") == (EXAMPLES / name).read_text(
            encoding="utf-8"
        ), f"{name} is stale: run python examples/make_examples.py"


# -- examples are consistent ----------------------------------------------------------------


def check_items(view: dict) -> dict:
    """Checks every view shares; returns the items by id."""
    now = parse(view["now"])
    items = {it["id"]: it for it in view["items"]}
    assert len(items) == len(view["items"]), "duplicate item"
    track_ids = {t["id"] for t in view["tracks"]}
    for it in items.values():
        assert it["track"] in track_ids, it["id"]
        assert it["owner"] in view["owners"], it["id"]
        # times are UTC with Z; days are plain dates
        for f in ("due_at", "wake_at", "done_at", "created_at", "updated_at", "state_since"):
            assert it[f] is None or TS_Z.match(it[f]), (it["id"], f)
        if it["when"]:
            pat = DAY if it["when"]["precision"] == "day" else TS_Z
            assert pat.match(it["when"]["start"]), it["id"]
            assert it["when"]["end"] is None or pat.match(it["when"]["end"]), it["id"]
        # conditions against the items they name
        for c in it["waits_on"]:
            if "item" in c and c["item"] in items:
                assert c["met"] == (items[c["item"]]["state"] == "done"), (it["id"], c)
            if "time" in c:
                assert c["met"] == (parse(c["time"]) <= now), (it["id"], c)
            assert (c["met_at"] is not None) == c["met"], (it["id"], c)
        # derived state
        if it["state"] in ("next", "waiting"):
            all_met = all(c["met"] for c in it["waits_on"])
            assert it["state"] == ("next" if all_met else "waiting"), it["id"]
        assert (it["done_at"] is not None) == (it["state"] in ("done", "dropped")), it["id"]
        if it["state"] == "dropped":
            assert it["note"], f"{it['id']} dropped without a reason"
        # flags
        late = it["due_at"] is not None and parse(it["due_at"]) < now and it["state"] in OPEN
        assert it["late"] == late, it["id"]
        assert it["at_risk"] == bool(it["at_risk_from"]), it["id"]
        if it["state"] not in OPEN:
            assert not it["at_risk"], it["id"]
        for src in it["at_risk_from"]:
            assert src["item"] in items, f"{it['id']}: the view lacks the item that makes it at risk"
            up = items[src["item"]]
            if src["why"] == "late":
                assert up["late"] and src["due_at"] == up["due_at"], (it["id"], src)
            else:
                assert up["state"] == "dropped", (it["id"], src)
        # questions belong to decisions and approvals
        if it["kind"] == "decision":
            assert it["question"], it["id"]
        if it["kind"] in ("task", "event"):
            assert it["question"] is None, it["id"]
        for step in it["overdue"]:
            assert (step["action"] == "notify") == (step.get("to") is not None), it["id"]
    # edges: both ends present, and exactly the item conditions among the view's items
    expected = {
        (c["item"], it["id"], c["met"])
        for it in items.values()
        for c in it["waits_on"]
        if "item" in c and c["item"] in items
    }
    got = {(e["from"], e["to"], e["met"]) for e in view["edges"]}
    assert got == expected
    assert len(view["edges"]) == len(got), "duplicate edge"
    # waited_on_by mirrors waits_on
    for it in items.values():
        for other in it["waited_on_by"]:
            if other in items:
                assert any(c.get("item") == it["id"] for c in items[other]["waits_on"])
    return items


def upstream_open(items: dict, iid: str) -> set[str]:
    out, todo = set(), [iid]
    while todo:
        for c in items[todo.pop()]["waits_on"]:
            if "item" in c and not c["met"] and c["item"] not in out:
                out.add(c["item"])
                todo.append(c["item"])
    return out


def test_timeline_consistent():
    view = load("timeline.json")
    items = check_items(view)
    frm = parse(view["window"]["from"])
    for it in items.values():
        if it["state"] not in OPEN:
            # closed items are in the window, or named by an open item's condition
            named = any(it["id"] in [c.get("item") for c in o["waits_on"]] for o in items.values())
            assert parse(it["done_at"]) >= frm or named, it["id"]
    # every blocker an item names is in the view
    for it in items.values():
        for c in it["waits_on"]:
            if "item" in c:
                assert c["item"] in items, (it["id"], c["item"])
    # at risk exactly when something upstream (through open conditions) is late or dropped
    for it in items.values():
        if it["state"] in OPEN:
            bad = {u for u in upstream_open(items, it["id"]) if items[u]["late"] or items[u]["state"] == "dropped"}
            assert {s["item"] for s in it["at_risk_from"]} == bad, it["id"]


def test_for_view_consistent():
    view = load("for-alice.json")
    items = check_items(view)
    human = view["human"]
    assert view["owners"][human]["role"] == "human"
    mine = {i for i, it in items.items() if it["owner"] == human and it["state"] in OPEN}
    assert {e["id"] for e in view["entries"]} == mine
    # ready ones first, each group oldest first
    keys = [(not e["ready"], e["since"]) for e in view["entries"]]
    assert keys == sorted(keys)
    for e in view["entries"]:
        it = items[e["id"]]
        assert e["ready"] == (it["state"] in ("next", "running"))
        if e["ready"]:
            assert e["on_the_way"] == []
        # unblocks = open items downstream, with BFS depth
        for step in e["unblocks"] + e["on_the_way"]:
            assert step["id"] in items and items[step["id"]]["state"] in OPEN
        down = {s["id"] for s in e["unblocks"]}
        for s in e["unblocks"]:
            if s["depth"] == 1:
                assert e["id"] in [c.get("item") for c in items[s["id"]]["waits_on"]]
        assert {s["id"] for s in e["on_the_way"]} == (set() if e["ready"] else upstream_open(items, e["id"]))
        assert e["id"] not in down
    # every item in the view is an entry, in a chain, or the reason for an at-risk flag
    named = set()
    for e in view["entries"]:
        named |= {e["id"]} | {s["id"] for s in e["unblocks"] + e["on_the_way"]}
    for i in set(named):
        named |= {src["item"] for src in items[i]["at_risk_from"]}
    assert named == set(items)


def test_examples_cover_the_cases():
    """The cases the pages must draw are all in the examples."""
    tl = load("timeline.json")
    items = tl["items"]
    owners = tl["owners"]
    human = {n for n, o in owners.items() if o["role"] == "human"}
    by_id = {it["id"]: it for it in items}
    assert any(it["kind"] == "event" and it["state"] == "running" and it["when"] and it["when"]["end"] for it in items), "soak"
    assert any(it["kind"] == "decision" and it["owner"] in human and it["question"]["options"] for it in items), "decision"
    assert any(it["kind"] == "approval" and it["owner"] in human for it in items), "approval"
    assert any(
        by_id[e["from"]]["owner"] != by_id[e["to"]]["owner"]
        and owners[by_id[e["from"]]["owner"]]["role"] == "agent"
        and owners[by_id[e["to"]]["owner"]]["role"] == "agent"
        for e in tl["edges"]
    ), "dependency across two agents"
    assert any(it["late"] and it["overdue_steps"] for it in items), "late item with ladder steps"
    assert any(it["at_risk"] for it in items), "at-risk item"
    assert any(it["state"] in OPEN and it["when"] is None and it["due_at"] is None for it in items), "undated item"
    assert any(it["when"] and it["when"]["precision"] == "day" for it in items), "day precision"
    assert any("time" in c for it in items for c in it["waits_on"]), "time condition"
    assert any(it["state"] == "done" for it in items) and any(it["state"] == "dropped" for it in items)
    assert any(it["label"] is None for it in items), "an item without a label"
    fa = load("for-alice.json")
    assert any(e["ready"] for e in fa["entries"]) and any(not e["ready"] for e in fa["entries"])
    assert any(len({s["depth"] for s in e["unblocks"]}) > 1 for e in fa["entries"]), "a chain longer than one"


# -- request bodies -------------------------------------------------------------------------

GOOD_CREATE = {
    "track": "release-2-4",
    "title": "Production release of 2.4",
    "label": "production release",
    "kind": "task",
    "waits_on": [{"item": "W-103"}, {"time": "2026-03-12T09:00:00+01:00"}],
    "when": {"start": "2026-03-12", "end": None, "precision": "day"},
    "due_at": "2026-03-12T17:00:00Z",
    "overdue": [{"after": "0m", "action": "nudge"}, {"after": "16h", "action": "notify", "to": "alice"}],
    "links": [{"url": "https://git.example.org/acme/app/issues/12", "label": "#12"}],
}


def test_create_body(spec):
    v = validator(spec, "ItemCreate")
    assert not list(v.iter_errors(GOOD_CREATE))
    bad = [
        {**GOOD_CREATE, "colour": "red"},  # unknown field
        {**GOOD_CREATE, "title": "x" * 81},
        {**GOOD_CREATE, "label": "x" * 25},
        {**GOOD_CREATE, "state": "next"},  # derived, not settable
        {**GOOD_CREATE, "due_at": "2026-03-12T17:00:00"},  # no zone
        {**GOOD_CREATE, "waits_on": ["W-103"]},  # conditions are objects
        {**GOOD_CREATE, "overdue": [{"after": "1h", "action": "run"}]},  # fixed list of actions
        {**GOOD_CREATE, "links": [{"url": "file:///etc/passwd"}]},
        {k: v for k, v in GOOD_CREATE.items() if k != "kind"},
    ]
    for body in bad:
        assert list(v.iter_errors(body)), body


def test_patch_body(spec):
    v = validator(spec, "ItemPatch")
    for body in (
        {"state": "done", "note": "released 14:02"},
        {"state": "dropped", "note": "folded into 2.4"},
        {"note": None, "due_at": None, "if_version": 3},
        {"add_waits_on": [{"item": "W-111"}]},
        {"label": None},
    ):
        assert not list(v.iter_errors(body)), body
    for body in ({}, {"id": "W-1"}, {"state": "late"}, {"created_at": "2026-03-10T09:30:00Z"}):
        assert list(v.iter_errors(body)), body


def test_delivery_shapes(spec):
    delivery = {
        "id": "D-3051",
        "recipient": "agent-a",
        "kind": "unblocked",
        "item": "W-104",
        "items": ["W-103"],
        "fell_due_at": "2026-03-11T16:20:00Z",
        "attempt": 1,
        "late": {
            "due_at": "2026-03-11T14:00:00Z",
            "reason": "waypoint_down",
            "down_from": "2026-03-11T13:10:00Z",
            "down_until": "2026-03-11T16:40:00Z",
        },
        "text": "W-103 (go for production) is done; W-104 (production release) can start",
        "summary": "Production release of 2.4 (agent-a)",
        "links": [],
    }
    assert not list(validator(spec, "DeliveryBatch").iter_errors({"deliveries": [delivery], "lease_seconds": 120}))
    assert not list(validator(spec, "DeliveryBatch").iter_errors({"deliveries": [], "lease_seconds": 120}))
    broken = copy.deepcopy(delivery)
    broken["kind"] = "run_command"
    assert list(validator(spec, "Delivery").iter_errors(broken))
    v = validator(spec, "AckRequest")
    assert not list(v.iter_errors({"result": "delivered"}))
    assert not list(v.iter_errors({"result": "undeliverable", "detail": "input box busy for 30 minutes"}))
    assert list(v.iter_errors({"result": "maybe"}))


def test_item_detail_has_history(spec):
    item = load("timeline.json")["items"][0]
    v = validator(spec, "ItemDetail")
    assert list(v.iter_errors(item)), "history is required"
    detail = {
        **item,
        "history": [
            {"at": item["created_at"], "by": item["created_by"], "event": "created", "changes": None, "note": None},
            {"at": item["done_at"], "by": item["owner"], "event": "changed",
             "changes": {"state": ["running", "done"]}, "note": item["note"]},
        ],
    }
    assert not list(v.iter_errors(detail))
    assert list(validator(spec, "Item").iter_errors(detail)), "Item has no history"


def test_error_shape(spec):
    v = validator(spec, "Error")
    assert not list(v.iter_errors({"error": {"code": "loop", "message": "would close a loop", "cycle": ["W-1", "W-2", "W-1"]}}))
    assert not list(v.iter_errors({"error": {"code": "content", "message": "looks like a secret", "field": "note"}}))
    assert list(v.iter_errors({"error": {"code": "oops", "message": "x"}}))


def test_packaged_spec_matches_the_yaml(spec):
    """src/waypoint/openapi.json is what the daemon validates with; it must be the YAML's copy."""
    packaged = json.loads((ROOT / "src" / "waypoint" / "openapi.json").read_text(encoding="utf-8"))
    assert packaged == spec, "run python scripts/sync_openapi.py"


def test_contract_version_is_the_package_version(spec):
    import waypoint
    assert spec["info"]["version"] == waypoint.__version__, "run python scripts/sync_openapi.py"
