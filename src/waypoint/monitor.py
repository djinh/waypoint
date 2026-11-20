"""Monitoring: /v1/status (what needs attention), /metrics (counts), and the nightly backup."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

from . import __version__
from .deliveries import delivery_id
from .store import Store, item_id
from .timeutil import fmt, parse_ts

STUCK = timedelta(minutes=30)
STALE_RUNNING = timedelta(days=1)
BEHIND = timedelta(minutes=1)


def status(store: Store) -> dict:
    now = store.now()
    silent = timedelta(seconds=store.settings.silent_seconds)
    with store.read() as c:
        last_pass = c.execute("SELECT MAX(at) FROM passes").fetchone()[0]
        deliverers = []
        for r in c.execute(
                "SELECT cl.name, COUNT(x.name) AS n, d.last_seen FROM clients cl "
                "JOIN clients x ON x.deliverer = cl.name AND x.retired_at IS NULL "
                "LEFT JOIN deliverers d ON d.name = cl.name "
                "WHERE cl.role = 'deliverer' AND cl.retired_at IS NULL GROUP BY cl.name ORDER BY cl.name"):
            quiet = r["last_seen"] is None or now - parse_ts(r["last_seen"]) > silent
            deliverers.append({"name": r["name"], "recipients": r["n"], "last_seen": r["last_seen"], "silent": quiet})
        unreached = [r[0] for r in c.execute(
            "SELECT name FROM clients WHERE retired_at IS NULL AND deliverer IS NULL "
            "AND role IN ('agent','human','coordinator') AND (name IN (SELECT owner FROM items WHERE state IN "
            "('next','running','waiting')) OR name IN (SELECT recipient FROM messages WHERE acked_at IS NULL "
            "AND withdrawn_at IS NULL)) ORDER BY name")]
        stuck = [{"id": delivery_id(r["num"]), "recipient": r["recipient"], "kind": r["kind"],
                  "item": item_id(r["item"]) if r["item"] is not None else None,
                  "fell_due_at": r["fell_due_at"], "attempt": r["attempt"]}
                 for r in c.execute("SELECT * FROM messages WHERE acked_at IS NULL AND withdrawn_at IS NULL "
                                    "AND fell_due_at < ? ORDER BY fell_due_at, num", (fmt(now - STUCK),))]
        late = [_item(r, r["due_at"]) for r in c.execute(
            "SELECT * FROM items WHERE late = 1 ORDER BY due_at, num")]
        unknown = [_item(r, r["created_at"]) for r in c.execute(
            "SELECT * FROM items WHERE state IN ('next','running','waiting') AND owner NOT IN "
            "(SELECT name FROM clients WHERE retired_at IS NULL) ORDER BY num")]
        stale = [_item(r, r["updated_at"]) for r in c.execute(
            "SELECT * FROM items WHERE state = 'running' AND updated_at < ? ORDER BY updated_at, num",
            (fmt(now - STALE_RUNNING),))]
        coordinator = _coordinator(store, c)
    behind = last_pass is None or now - parse_ts(last_pass) > BEHIND
    ok = not (behind or any(d["silent"] for d in deliverers) or unreached or stuck or late or unknown or stale
              or not coordinator["live"])
    return {"generated_at": fmt(now), "version": __version__, "ok": ok,
            "scheduler": {"last_pass": last_pass, "behind": behind}, "deliverers": deliverers,
            "recipients_without_deliverer": unreached, "stuck_deliveries": stuck, "late_items": late,
            "unknown_owners": unknown, "running_stale": stale, "coordinator": coordinator}


def _coordinator(store: Store, c) -> dict:
    """Who hears about escalations: the configured or first live coordinator, whether it is a live
    coordinator, and whether a deliverer reaches it. Without a live one, escalations go nowhere."""
    name = store.coordinator()
    row = c.execute("SELECT role, deliverer FROM clients WHERE name = ? AND retired_at IS NULL", (name,)).fetchone() \
        if name else None
    live = row is not None and row["role"] == "coordinator"
    return {"name": name, "live": live, "routed": bool(live and row["deliverer"])}


def _item(r, since: str) -> dict:
    return {"id": item_id(r["num"]), "owner": r["owner"], "title": r["title"], "since": since}


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def metrics(store: Store) -> str:
    """The Prometheus text exposition format."""
    st = status(store)
    now = store.now()
    lines = []

    def metric(name: str, kind: str, helptext: str, samples: list[tuple[str, float]]):
        lines.append(f"# HELP {name} {helptext}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lines.append(f"{name}{labels} {value:g}")

    with store.read() as c:
        counts = dict(c.execute("SELECT state, COUNT(*) FROM items GROUP BY state").fetchall())
        at_risk = c.execute("SELECT COUNT(*) FROM items WHERE at_risk_from != '[]'").fetchone()[0]
        waiting = c.execute("SELECT COUNT(*) FROM messages WHERE acked_at IS NULL AND withdrawn_at IS NULL").fetchone()[0]
        sent = dict(c.execute("SELECT ack_result, COUNT(*) FROM messages WHERE ack_result IS NOT NULL "
                              "GROUP BY ack_result").fetchall())
    metric("waypoint_up", "gauge", "Waypoint answers.", [("", 1)])
    metric("waypoint_items", "gauge", "Items by state.",
           [(f'{{state="{s}"}}', counts.get(s, 0)) for s in ("next", "running", "waiting", "done", "dropped")])
    metric("waypoint_items_late", "gauge", "Open items past their due time.", [("", len(st["late_items"]))])
    metric("waypoint_items_at_risk", "gauge", "Open items waiting on something late or dropped.", [("", at_risk)])
    metric("waypoint_items_unknown_owner", "gauge", "Open items whose owner is not a client.",
           [("", len(st["unknown_owners"]))])
    metric("waypoint_items_running_stale", "gauge", "Items running for a day without an update.",
           [("", len(st["running_stale"]))])
    metric("waypoint_messages_waiting", "gauge", "Messages not confirmed or withdrawn yet.", [("", waiting)])
    metric("waypoint_messages_confirmed", "counter", "Messages confirmed, by result (until pruned).",
           [(f'{{result="{r}"}}', sent.get(r, 0)) for r in ("delivered", "undeliverable")])
    metric("waypoint_deliveries_stuck", "gauge", "Messages unconfirmed 30 minutes after they fell due.",
           [("", len(st["stuck_deliveries"]))])
    metric("waypoint_recipients_without_deliverer", "gauge", "Recipients nobody can reach.",
           [("", len(st["recipients_without_deliverer"]))])
    metric("waypoint_deliverer_last_seen_seconds", "gauge", "Seconds since each deliverer last fetched.",
           [(f'{{deliverer="{_label(d["name"])}"}}',
             (now - parse_ts(d["last_seen"])).total_seconds() if d["last_seen"] else -1) for d in st["deliverers"]])
    last = st["scheduler"]["last_pass"]
    metric("waypoint_scheduler_last_pass_seconds", "gauge", "Seconds since the last scheduler pass.",
           [("", (now - parse_ts(last)).total_seconds() if last else -1)])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- backup

def backup(store: Store, directory: str | Path) -> tuple[Path, Path]:
    """A consistent copy of the database and a JSON export, named by the day. The database holds
    token hashes only; the export holds no token data at all. Files are readable by their owner only."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    day = store.now().strftime("%Y%m%d")
    db_path, json_path = d / f"waypoint-{day}.db", d / f"waypoint-{day}.json"
    tmp_db = d / f".waypoint-{day}.db.tmp"
    tmp_db.unlink(missing_ok=True)
    fd = os.open(tmp_db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    target = sqlite3.connect(str(tmp_db))
    try:
        with store.read() as c:
            c.backup(target)
    finally:
        target.close()
    os.replace(tmp_db, db_path)
    export = export_json(store)
    tmp_json = d / f".waypoint-{day}.json.tmp"
    tmp_json.unlink(missing_ok=True)
    fd = os.open(tmp_json, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=1, ensure_ascii=False)
    os.replace(tmp_json, json_path)
    return db_path, json_path


def export_json(store: Store) -> dict:
    """Everything but token hashes, from one read transaction: a snapshot, even while the daemon
    writes (the backup command runs beside it)."""
    with store.read() as c:
        c.execute("BEGIN")
        try:
            clients = [{"name": r["name"], "role": r["role"], "label": r["label"], "deliverer": r["deliverer"],
                        "created_at": r["created_at"], "retired_at": r["retired_at"]}
                       for r in c.execute("SELECT * FROM clients ORDER BY name")]
            items = [store.get_item(it["id"], history=True) for it in store.items()]
            tracks = store.tracks(archived=True)
        finally:
            c.execute("COMMIT")
    return {"exported_at": fmt(store.now()), "version": __version__, "clients": clients,
            "tracks": tracks, "items": items}
