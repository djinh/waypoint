"""/v1/status, /metrics, pruning settled messages, and the backup."""

from __future__ import annotations

import json
import sqlite3
import stat

import pytest

from waypoint import monitor, server as daemon
from waypoint.deliveries import Deliveries
from waypoint.scheduler import Scheduler
from waypoint.schema import validator
from waypoint.store import Client


def valid(name, body):
    errors = list(validator(name).iter_errors(body))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:3]]


@pytest.fixture
def sched(store, tokens, tracks):
    store.settings.outage_seconds = 10**9
    s = Scheduler(store)
    s.pass_hooks.append(Deliveries.of(store).check_silent)
    return s


def test_all_quiet(store, sched):
    sched.run_pass()
    st = monitor.status(store)
    valid("Status", st)
    assert st["ok"] and not st["scheduler"]["behind"]


def test_what_needs_attention(store, sched, mk, who):
    store.route("agent-a", "host-1")
    late = mk("agent-a", "Release notes", due_at="2026-03-10T09:00:00Z")
    mk("agent-b", "No deliverer for me")
    run = mk("agent-c", "Long job", state="running")
    gone = mk("agent-c", "Orphan")
    store.conn.execute("UPDATE items SET owner = 'agent-x' WHERE num = ?", (int(gone["id"][2:]),))
    sched.run_pass()  # a nudge for agent-a, waiting in host-1's queue
    store.clock.advance(days=1, minutes=1)
    sched.run_pass()
    st = monitor.status(store)
    valid("Status", st)
    assert not st["ok"]
    assert st["deliverers"] == [{"name": "host-1", "recipients": 1, "last_seen": None, "silent": True}]
    assert st["recipients_without_deliverer"] == ["agent-b", "agent-c", "coordinator"]
    assert [d["kind"] for d in st["stuck_deliveries"]][0] == "nudge"
    assert [i["id"] for i in st["late_items"]] == [late["id"]]
    assert [i["id"] for i in st["unknown_owners"]] == [gone["id"]]
    assert [i["id"] for i in st["running_stale"]] == [run["id"]]
    store.clock.advance(minutes=2)
    assert monitor.status(store)["scheduler"]["behind"]


def test_status_and_metrics_over_http(server, store, tokens, tracks, mk):
    mk("agent-a", "Late", due_at="2026-03-10T09:00:00Z")
    status, st, _ = server("GET", "/v1/status", tokens["pages"])
    assert status == 200
    valid("Status", st)
    assert server("GET", "/v1/status", tokens["host-1"])[0] == 403
    assert server("GET", "/v1/status")[0] == 401
    assert server("GET", "/metrics")[0] == 401
    import urllib.request
    req = urllib.request.Request(server.base + "/metrics", headers={"Authorization": f"Bearer {tokens['pages']}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        text = resp.read().decode()
        assert resp.headers["Content-Type"].startswith("text/plain")
    assert 'waypoint_items{state="next"} 1' in text
    assert "waypoint_items_late 1" in text
    assert "# TYPE waypoint_deliveries_stuck gauge" in text
    assert "waypoint_scheduler_last_pass_seconds -1" in text


def test_settled_messages_are_pruned_after_30_days(store, sched, mk, who):
    store.route("agent-a", "host-1")
    mk("agent-a", "Remind me", wake_at="2026-03-10T09:30:00Z")
    mk("agent-a", "Remind me too", wake_at="2026-03-10T09:30:00Z")
    sched.run_pass()
    dl = Deliveries.of(store)
    d1, d2 = dl.fetch(Client("host-1", "deliverer"))
    dl.ack(Client("host-1", "deliverer"), d1["id"], {"result": "delivered"})
    store.clock.advance(days=30, seconds=1)
    sched.run_pass()
    left = [r[0] for r in store.conn.execute("SELECT num FROM messages WHERE kind = 'wake'")]
    assert left == [int(d2["id"][2:])]  # the unconfirmed one stays, however old


def test_backup(store, tokens, tracks, mk, tmp_path):
    mk("agent-a", "Something", note="kept")
    db_path, json_path = monitor.backup(store, tmp_path / "backup")
    for p in (db_path, json_path):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    copy = sqlite3.connect(db_path)
    assert copy.execute("SELECT title FROM items").fetchall() == [("Something",)]
    data = json.loads(json_path.read_text())
    assert data["items"][0]["note"] == "kept" and data["items"][0]["history"]
    assert {c["name"] for c in data["clients"]} >= {"agent-a", "alice"}
    text = json_path.read_text()
    for t in tokens.values():
        assert t not in text
    assert "token" not in text
    assert monitor.backup(store, tmp_path / "backup") == (db_path, json_path)  # same day: replaced


def test_backup_command(tmp_path, capsys):
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[store]\ndatabase = "{tmp_path / "w.db"}"\n')
    assert daemon.main(["--config", str(cfgp), "backup", str(tmp_path / "b")]) == 0
    assert "wrote waypoint-" in capsys.readouterr().out
