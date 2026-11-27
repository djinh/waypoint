"""/v1/status, /metrics, pruning settled messages, and the backup."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import date, datetime, timedelta, timezone

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


# ---------------------------------------------------------------- keeping N days of backups
def backups(d, *days, ext=(".db", ".json")):
    d.mkdir(exist_ok=True)
    for day in days:
        for e in ext:
            (d / f"waypoint-{day}{e}").write_text("x")


def test_prune_removes_only_backups_dated_before_the_cutoff(tmp_path):
    d = tmp_path / "b"
    backups(d, "20261026", "20261027", "20261028", "20261127", "20261201")
    for odd in ("waypoint-20261001.db.gz", "notes-20261001.db", ".waypoint-20261001.db.tmp", "waypoint-2026101.db",
                "waypoint-20261301.db", "waypoint-20260230.json", "Waypoint-20261001.db", "waypoint-20261001.DB"):
        (d / odd).write_text("keep")
    (d / "waypoint-20261002.db").mkdir()  # a folder with a backup's name
    os.utime(d / "waypoint-20261127.db", (0, 0))  # an old file time doesn't matter: the name's date does
    removed, failed = monitor.prune_backups(d, date(2026, 11, 27), 30)
    assert removed == ["waypoint-20261026.db", "waypoint-20261026.json", "waypoint-20261027.db", "waypoint-20261027.json"]
    assert failed == []
    left = sorted(p.name for p in d.iterdir())
    assert "waypoint-20261028.db" in left and "waypoint-20261127.db" in left and "waypoint-20261201.json" in left
    assert len(left) == 6 + 8 + 1
    assert monitor.prune_backups(d, date(2026, 11, 27), 30) == ([], [])  # nothing more to do


def test_prune_leaves_a_symlink_and_its_target_alone(tmp_path):
    d, elsewhere = tmp_path / "b", tmp_path / "precious.db"
    d.mkdir()
    elsewhere.write_text("keep")
    (d / "waypoint-20200101.db").symlink_to(elsewhere)
    assert monitor.prune_backups(d, date(2026, 11, 27), 1) == ([], [])
    assert elsewhere.read_text() == "keep" and (d / "waypoint-20200101.db").is_symlink()


def test_prune_keep_days_one_keeps_today_and_yesterday(tmp_path):
    d = tmp_path / "b"
    backups(d, "20261125", "20261126", "20261127")
    removed, _ = monitor.prune_backups(d, date(2026, 11, 27), 1)
    assert removed == ["waypoint-20261125.db", "waypoint-20261125.json"]
    with pytest.raises(ValueError):
        monitor.prune_backups(d, date(2026, 11, 27), 0)


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes files from a read-only folder")
def test_prune_reports_what_it_could_not_remove(tmp_path):
    d = tmp_path / "b"
    backups(d, "20200101")
    d.chmod(0o500)
    try:
        removed, failed = monitor.prune_backups(d, date(2026, 11, 27), 30)
    finally:
        d.chmod(0o700)
    assert removed == [] and [n for n, _ in failed] == ["waypoint-20200101.db", "waypoint-20200101.json"]


def days_ago(n):
    return (datetime.now(timezone.utc).date() - timedelta(days=n)).strftime("%Y%m%d")


def test_backup_command_keeps_everything_by_default(tmp_path, capsys):
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[store]\ndatabase = "{tmp_path / "w.db"}"\n')
    backups(tmp_path / "b", days_ago(400))
    assert daemon.main(["--config", str(cfgp), "backup", str(tmp_path / "b")]) == 0
    assert "removed" not in capsys.readouterr().out
    assert (tmp_path / "b" / f"waypoint-{days_ago(400)}.db").exists()


def test_backup_command_with_keep_days_from_the_config_and_the_option(tmp_path, capsys):
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[store]\ndatabase = "{tmp_path / "w.db"}"\n[backup]\nkeep_days = 30\n')
    b = tmp_path / "b"
    backups(b, days_ago(45), days_ago(10))
    assert daemon.main(["--config", str(cfgp), "backup", str(b)]) == 0
    out = capsys.readouterr().out
    assert f"removed 2 backup files older than 30 days: waypoint-{days_ago(45)}.db, waypoint-{days_ago(45)}.json" in out
    assert {p.name for p in b.iterdir()} == {f"waypoint-{days_ago(n)}{e}" for n in (10, 0) for e in (".db", ".json")}
    assert daemon.main(["--config", str(cfgp), "backup", str(b), "--keep-days", "5"]) == 0  # the option wins
    assert {p.name for p in b.iterdir()} == {f"waypoint-{days_ago(0)}{e}" for e in (".db", ".json")}
    for bad in ("0", "3651"):
        assert daemon.main(["--config", str(cfgp), "backup", str(b), "--keep-days", bad]) == 2
    assert "--keep-days: a whole number" in capsys.readouterr().err


def test_a_failed_backup_removes_nothing(tmp_path, monkeypatch, capsys):
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f'[store]\ndatabase = "{tmp_path / "w.db"}"\n[backup]\nkeep_days = 1\n')
    b = tmp_path / "b"
    backups(b, days_ago(45))

    def broken(store, directory):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(monitor, "backup", broken)
    assert daemon.main(["--config", str(cfgp), "backup", str(b)]) == 1
    assert "No space left" in capsys.readouterr().err
    assert len(list(b.iterdir())) == 2


@pytest.mark.parametrize("value", ["0", "3651", '"30"', "true", "30.0"])
def test_bad_keep_days_are_refused(tmp_path, value):
    from waypoint.config import ConfigError, load
    cfgp = tmp_path / "c.toml"
    cfgp.write_text(f"[backup]\nkeep_days = {value}\n")
    with pytest.raises(ConfigError, match="backup.keep_days"):
        load(cfgp)
    cfgp.write_text("[backup]\nkeep = 30\n")
    with pytest.raises(ConfigError, match="unknown key: backup.keep"):
        load(cfgp)
